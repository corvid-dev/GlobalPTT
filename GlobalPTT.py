"""
GlobalPTT — Push-to-Talk for Windows 11
Requirements: pip install pynput pycaw comtypes
"""

import tkinter as tk
from tkinter import ttk
import threading
import queue
import json
import sys
import os
import uuid
from dataclasses import dataclass, field
from pynput import keyboard, mouse
from pycaw.pycaw import IAudioEndpointVolume, IMMDeviceEnumerator
from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize, CoCreateInstance, GUID

VERSION = "1.44"

# ── constants ─────────────────────────────────────────────────────────────────
APP_BG     = "#1e1e1e"
PANEL_BG   = "#2a2a2a"
COL_BG     = "#252525"
DIVIDER    = "#333333"
ACCENT     = "#00b894"
ACCENT_OFF = "#e17055"
TEXT       = "#ececec"
MUTED      = "#888888"

NO_DEVICE  = "— None —"
COL_WIDTH  = 200
COL_MIN_W  = 340
COL_MIN_H  = 320
ADD_BAR_W  = 50

PREFS_PATH = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "GlobalPTT", "prefs.json")

MOUSE_MAP = {
    mouse.Button.left:   "Mouse-Left",
    mouse.Button.right:  "Mouse-Right",
    mouse.Button.middle: "Mouse-Middle",
    mouse.Button.x1:     "Mouse-X1",
    mouse.Button.x2:     "Mouse-X2",
}

KEY_DISPLAY = {
    "ctrl_l": "L-Ctrl",   "ctrl_r": "R-Ctrl",
    "shift":  "Shift",    "shift_l": "L-Shift", "shift_r": "R-Shift",
    "alt_l":  "L-Alt",    "alt_r":   "R-Alt",
    "Mouse-Left": "Mouse L", "Mouse-Right": "Mouse R",
    "Mouse-Middle": "Mouse M", "Mouse-X1": "Mouse 4", "Mouse-X2": "Mouse 5",
}

_DISPLAY_CACHE: dict[str, str] = {}

_CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_PKEY_Device_FriendlyName = GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}")


# ── channel state ─────────────────────────────────────────────────────────────

@dataclass
class ChannelState:
    uid:              str       = field(default_factory=lambda: str(uuid.uuid4()))
    device_name:      str       = NO_DEVICE   # stores the COM friendly name verbatim
    keybinds:         list[str] = field(default_factory=list)
    release_delay_ms: int       = 250

    @staticmethod
    def from_prefs(p: dict) -> "ChannelState":
        return ChannelState(
            device_name      = p.get("device_name", NO_DEVICE),
            keybinds         = list(p.get("keybinds", [])),
            release_delay_ms = p.get("release_delay_ms", 250),
        )

    def to_prefs(self) -> dict:
        return {"device_name": self.device_name,
                "keybinds":    list(self.keybinds),
                "release_delay_ms": self.release_delay_ms}


# ── helpers ───────────────────────────────────────────────────────────────────

def key_to_label(k) -> str:
    if isinstance(k, str):
        return k
    try:
        return k.char or str(k).replace("Key.", "")
    except AttributeError:
        return str(k).replace("Key.", "")

def label_to_display(label: str) -> str:
    if label not in _DISPLAY_CACHE:
        _DISPLAY_CACHE[label] = KEY_DISPLAY.get(
            label, label.upper() if len(label) == 1 else label.title())
    return _DISPLAY_CACHE[label]

def mk_label(parent, text, fg=MUTED, font=("Segoe UI", 8), **kw) -> tk.Label:
    w = tk.Label(parent, text=text, bg=parent["bg"], fg=fg, font=font, **kw)
    w.pack(fill="x")
    return w

def mk_divider(parent):
    tk.Frame(parent, bg=DIVIDER, height=1).pack(fill="x", pady=(0, 8))

def mk_btn(parent, text, cmd, fg=TEXT, afg=TEXT, abg="#444", **kw) -> tk.Button:
    kw.setdefault("font", ("Segoe UI", 9))
    return tk.Button(parent, text=text, command=cmd,
                     bg=PANEL_BG, fg=fg, relief="flat", cursor="hand2",
                     activebackground=abg, activeforeground=afg, **kw)

def load_prefs() -> dict:
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_prefs(data: dict):
    try:
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with open(PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ── Windows mic gate ──────────────────────────────────────────────────────────

class WinMicGate:
    def __init__(self):
        self._vol: IAudioEndpointVolume | None = None
        self._orig_mute:   bool | None         = None
        self._orig_volume: float | None        = None
        self._fallback                         = False

    @staticmethod
    def enumerate_devices() -> dict[str, str]:
        """Return {friendly_name: ep_id} for all active endpoints. COM thread only."""
        def _friendly(ep) -> str:
            try:
                store = ep.OpenPropertyStore(0)
                for i in range(store.GetCount()):
                    pk = store.GetAt(i)
                    if pk.fmtid == _PKEY_Device_FriendlyName and pk.pid == 14:
                        return str(store.GetValue(pk).GetValue())
            except Exception:
                pass
            return ""

        result: dict[str, str] = {}
        seen:   set[str]       = set()
        try:
            enumerator = CoCreateInstance(
                _CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
            for data_flow in (2, 0):   # eCapture first, then eRender
                col = enumerator.EnumAudioEndpoints(data_flow, 1)
                for i in range(col.GetCount()):
                    ep = col.Item(i)
                    try:
                        ep_id = ep.GetId()
                        if ep_id in seen:
                            continue
                        seen.add(ep_id)
                        name = _friendly(ep)
                        if name:
                            result[name] = ep_id
                    except Exception:
                        pass
        except Exception:
            pass
        return result

    def _activate(self, ep):
        iface = ep.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self._vol = iface.QueryInterface(IAudioEndpointVolume)
        self._orig_mute   = bool(self._vol.GetMute())
        self._orig_volume = self._vol.GetMasterVolumeLevelScalar()
        try:
            self._vol.SetMute(1, None)
            self._fallback = not bool(self._vol.GetMute())
            self._vol.SetMute(0, None)
        except Exception:
            self._fallback = True

    def set_mute(self, muted: bool):
        if not self._vol:
            return
        try:
            if self._fallback:
                self._vol.SetMasterVolumeLevelScalar(
                    0.0 if muted else (self._orig_volume or 1.0), None)
            else:
                self._vol.SetMute(int(muted), None)
        except Exception:
            pass

    def restore(self):
        if not self._vol:
            return
        try:
            if self._fallback and self._orig_volume is not None:
                self._vol.SetMasterVolumeLevelScalar(self._orig_volume, None)
            elif self._orig_mute is not None:
                self._vol.SetMute(int(self._orig_mute), None)
        except Exception:
            pass

    def close(self):
        self.set_mute(False)
        self.restore()
        self._vol = None


# ── gate worker ───────────────────────────────────────────────────────────────

class GateWorker:
    """Single COM thread. Messages on _q are (cmd, uid, arg)."""

    def __init__(self, q: queue.Queue, on_names_ready):
        self._q              = q
        self._on_names_ready = on_names_ready
        self._gates: dict[str, WinMicGate] = {}

    def run(self):
        CoInitialize()
        try:
            name_map = WinMicGate.enumerate_devices()   # {name: ep_id}
            self._on_names_ready(list(name_map.keys()))

            enumerator = CoCreateInstance(
                _CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)

            while True:
                try:
                    cmd, uid, arg = self._q.get(timeout=0.2)
                except queue.Empty:
                    continue

                if cmd == "attach":
                    # arg = (device_name, on_attach_cb)
                    name, on_attach = arg
                    g = self._gates.setdefault(uid, WinMicGate())
                    g.restore()
                    g._vol = None
                    if name and name != NO_DEVICE:
                        ep_id = name_map.get(name)
                        try:
                            if ep_id:
                                g._activate(enumerator.GetDevice(ep_id))
                                g.set_mute(True)
                                on_attach(name)
                            else:
                                on_attach("")
                        except Exception:
                            on_attach("")
                    else:
                        on_attach(NO_DEVICE)

                elif cmd == "mute":
                    if uid in self._gates:
                        self._gates[uid].set_mute(arg)

                elif cmd == "remove":
                    if uid in self._gates:
                        self._gates.pop(uid).close()

                elif cmd == "close":
                    for g in self._gates.values():
                        g.close()
                    break
        finally:
            CoUninitialize()


# ── PTT channel ───────────────────────────────────────────────────────────────

class PTTChannel:
    """One UI column. state.uid is stable; display_index is cosmetic only."""

    def __init__(self, state: ChannelState, display_index: int,
                 root: tk.Tk,
                 on_set_mute,         # (bool) -> None
                 on_attach,           # (device_name) -> None
                 on_remove_gate,      # () -> None
                 is_capturing,        # () -> bool
                 start_capture,       # () -> None
                 finish_capture,      # (label) -> None
                 on_keybinds_changed, # () -> None
                 on_remove,           # (self) -> None
                 on_change):          # () -> None
        self.state                = state
        self.display_index        = display_index
        self.root                 = root
        self._on_set_mute         = on_set_mute
        self._on_attach           = on_attach
        self._on_remove_gate      = on_remove_gate
        self._is_capturing        = is_capturing
        self._start_capture_cb    = start_capture
        self._finish_capture      = finish_capture
        self._on_keybinds_changed = on_keybinds_changed
        self._on_remove           = on_remove
        self._on_change           = on_change

        self._lock          = threading.RLock()
        self._active_keys: set[str]               = set()
        self._talking                             = False
        self._timer: threading.Timer | None       = None

        self.frame          = None
        self._divider_frame = None
        self._header_lbl    = None
        self._status_var    = None
        self._status_lbl    = None
        self._device_cb     = None
        self._delay_var     = None
        self._bind_frame    = None
        self._bind_rows: list[tk.Frame]           = []
        self._add_btn       = None
        self._capture_lbl   = None

    # ── build ─────────────────────────────────────────────────────────────────

    def build_column(self, parent) -> tk.Frame:
        self.frame = tk.Frame(parent, bg=COL_BG, width=COL_WIDTH)
        self.frame.pack_propagate(False)

        # header bar
        hdr = tk.Frame(self.frame, bg=PANEL_BG)
        hdr.pack(fill="x")
        self._header_lbl = tk.Label(hdr, text=f"Channel {self.display_index + 1}",
                                    bg=PANEL_BG, fg=MUTED,
                                    font=("Segoe UI", 8, "bold"), anchor="w")
        self._header_lbl.pack(side="left", padx=10, pady=6)
        mk_btn(hdr, "✕", lambda: self._on_remove(self),
               fg=MUTED, afg=ACCENT_OFF, abg=PANEL_BG
               ).pack(side="right", padx=(0, 4), pady=4)
        self._status_var = tk.StringVar(value="○ INACTIVE")
        self._status_lbl = tk.Label(hdr, textvariable=self._status_var,
                                    bg=PANEL_BG, fg=MUTED,
                                    font=("Segoe UI", 9, "bold"), anchor="e")
        self._status_lbl.pack(side="right", padx=(10, 4), pady=6)
        tk.Frame(self.frame, bg=DIVIDER, height=1).pack(fill="x")

        body = tk.Frame(self.frame, bg=COL_BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)

        # device picker — values filled in by update_device_list()
        mk_label(body, "Input Device", anchor="w")
        self._device_cb = ttk.Combobox(body, state="readonly", font=("Segoe UI", 9))
        self._device_cb["values"] = [NO_DEVICE]
        self._device_cb.current(0)
        self._device_cb.pack(fill="x", pady=(2, 8))
        self._device_cb.bind("<<ComboboxSelected>>", self._on_device_change)

        mk_divider(body)

        # release delay
        self._delay_var = tk.IntVar(value=self.state.release_delay_ms)
        self._delay_var.trace_add("write", self._on_delay_change)
        mk_label(body, "Release Delay (ms)", anchor="w")
        tk.Scale(body, variable=self._delay_var, from_=0, to=2000,
                 orient="horizontal", bg=COL_BG, fg=TEXT, troughcolor=PANEL_BG,
                 highlightthickness=0, bd=0, activebackground=ACCENT,
                 font=("Segoe UI", 8), sliderlength=14
                 ).pack(fill="x", pady=(2, 8))

        mk_divider(body)

        # keybinds
        mk_label(body, "Keybinds", anchor="w")
        self._bind_frame = tk.Frame(body, bg=PANEL_BG)
        self._bind_frame.pack(fill="x", pady=(2, 6))

        btn_row = tk.Frame(body, bg=COL_BG)
        btn_row.pack(fill="x")
        self._add_btn = mk_btn(btn_row, "+ Add Key", self._start_capture)
        self._add_btn.pack(side="left")
        self._capture_lbl = tk.Label(btn_row, text="", bg=COL_BG,
                                      fg=ACCENT, font=("Segoe UI", 8))
        self._capture_lbl.pack(side="left", padx=(8, 0))

        self._rebuild_bind_list()
        return self.frame

    def update_display_index(self, idx: int):
        self.display_index = idx
        if self._header_lbl:
            self._header_lbl.config(text=f"Channel {idx + 1}")

    def update_device_list(self, com_names: list[str]):
        if not self._device_cb:
            return
        self._device_cb["values"] = [NO_DEVICE] + com_names
        saved = self.state.device_name
        if saved and saved != NO_DEVICE and saved in com_names:
            self._device_cb.current(com_names.index(saved) + 1)
        else:
            self._device_cb.current(0)
            if saved != NO_DEVICE:
                self.state.device_name = NO_DEVICE

    # ── device ────────────────────────────────────────────────────────────────

    def _on_device_change(self, _=None):
        sel = self._device_cb.current()
        self.state.device_name = NO_DEVICE if sel == 0 else self._device_cb["values"][sel]
        self._on_attach(self.state.device_name)
        self._on_change()

    def attach_initial(self):
        if self.state.device_name != NO_DEVICE:
            self._on_attach(self.state.device_name)

    def on_attach_result(self, name: str):
        if   name == NO_DEVICE: self.root.after(0, self._set_status, "○ INACTIVE", MUTED)
        elif name:               self.root.after(0, self._set_status, "● MUTED",    ACCENT_OFF)
        else:                    self.root.after(0, self._set_status, "⚠ Mic not found", ACCENT_OFF)

    def _on_delay_change(self, *_):
        self.state.release_delay_ms = self._delay_var.get()
        self._on_change()

    # ── keybinds ──────────────────────────────────────────────────────────────

    def _rebuild_bind_list(self):
        for row in self._bind_rows:
            row.destroy()
        self._bind_rows.clear()
        for label in self.state.keybinds:
            row = tk.Frame(self._bind_frame, bg=PANEL_BG)
            row.pack(fill="x", padx=4, pady=2)
            tk.Label(row, text=label_to_display(label), bg=PANEL_BG, fg=TEXT,
                     font=("Segoe UI", 9), anchor="w").pack(side="left", padx=(6, 0))
            mk_btn(row, "✕", lambda l=label: self._remove_bind(l),
                   fg=MUTED, afg=ACCENT_OFF, abg=PANEL_BG,
                   font=("Segoe UI", 8)).pack(side="right", padx=4)
            self._bind_rows.append(row)

    def _remove_bind(self, label: str):
        with self._lock:
            self.state.keybinds.remove(label)
        self._rebuild_bind_list()
        self._on_keybinds_changed()
        self._on_change()

    def _start_capture(self):
        if self._is_capturing():
            return
        self._start_capture_cb()
        self._add_btn.config(state="disabled")
        self._capture_lbl.config(text="Press a key…")

    def _do_finish_capture(self, label: str):
        with self._lock:
            if label and label not in self.state.keybinds:
                self.state.keybinds.append(label)
                self._rebuild_bind_list()
                self._on_keybinds_changed()
                self._on_change()
        self.root.after(0, self._capture_lbl.config, {"text": ""})
        self.root.after(0, self._add_btn.config, {"state": "normal"})

    # ── input events ──────────────────────────────────────────────────────────

    def handle_press(self, label: str) -> bool:
        if self._is_capturing():
            self._finish_capture(label)
            return True
        with self._lock:
            if label in self.state.keybinds:
                self._active_keys.add(label)
                self._set_talking(True)
        return False

    def handle_release(self, label: str):
        with self._lock:
            self._active_keys.discard(label)
            if any(k in self._active_keys for k in self.state.keybinds):
                return
        self._set_talking(False)

    # ── PTT state machine ─────────────────────────────────────────────────────

    def _set_talking(self, talking: bool):
        with self._lock:
            if talking:
                if self.state.device_name == NO_DEVICE or self._talking:
                    return
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
                self._talking = True
                self._on_set_mute(False)
                self.root.after(0, self._set_status, "● LIVE", ACCENT)
            else:
                if not self._talking:
                    return
                delay = self.state.release_delay_ms
                if self._timer:
                    self._timer.cancel()
                if delay == 0:
                    self._timer = None
                    do_now = True
                else:
                    self._timer = threading.Timer(delay / 1000.0, self._deactivate_now)
                    self._timer.daemon = True
                    do_now = False
        if talking:
            return
        if do_now:
            self._deactivate_now()
        else:
            self._timer.start()

    def _deactivate_now(self):
        with self._lock:
            self._timer = None
            if any(k in self._active_keys for k in self.state.keybinds):
                return
            if not self._talking:
                return
            self._talking = False
        self._on_set_mute(True)
        self.root.after(0, self._set_status, "● MUTED", ACCENT_OFF)

    def cancel_timers(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._talking = False

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _set_status(self, text: str, color: str):
        if self._status_var:  self._status_var.set(text)
        if self._status_lbl:  self._status_lbl.config(fg=color)

    def destroy(self):
        self.cancel_timers()
        self._on_set_mute(False)
        self._on_remove_gate()
        if self._divider_frame:
            self._divider_frame.destroy()
            self._divider_frame = None
        if self.frame:
            self.frame.destroy()
            self.frame = None


# ── application ───────────────────────────────────────────────────────────────

class PushToTalkApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"GlobalPTT - Push to Talk - v{VERSION}")
        self.root.configure(bg=APP_BG)
        self.root.resizable(True, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._prefs          = load_prefs()
        self._gate_q         = queue.Queue()
        self._capturing:     PTTChannel | None           = None
        self._keybind_index: dict[str, list[PTTChannel]] = {}
        self._channels:      list[PTTChannel]            = []
        self._com_names:     list[str]                   = []

        self._worker     = GateWorker(self._gate_q, self._on_names_ready)
        self._gate_thread = threading.Thread(target=self._worker.run, daemon=True)
        self._gate_thread.start()

        self._build_ui()
        self._restore_channels()
        self._start_listeners()
        self.root.update_idletasks()
        self._update_geometry(snap=True)
        self._set_icon()

    def _set_icon(self):
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            self.root.wm_iconbitmap(os.path.join(base, "GlobalPTTIcon.ico"))
        except Exception:
            pass

    # ── UI scaffold ───────────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TCombobox", fieldbackground=PANEL_BG, background=PANEL_BG,
                        foreground=TEXT, selectbackground=PANEL_BG,
                        selectforeground=TEXT, bordercolor="#444", arrowcolor=TEXT)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL_BG)],
                               foreground=[("readonly", TEXT)])

        outer = tk.Frame(self.root, bg=APP_BG)
        outer.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(outer, bg=APP_BG, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(outer, orient="horizontal",
                                         command=self._canvas.xview)
        self._canvas.configure(xscrollcommand=self._scrollbar.set)
        self._scrollbar.pack(side="bottom", fill="x")
        self._canvas.pack(side="top", fill="both", expand=True)

        self._inner   = tk.Frame(self._canvas, bg=APP_BG)
        self._cwin    = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>",
                          lambda _: self._canvas.itemconfig(
                              self._cwin, height=self._canvas.winfo_height()))

        self._add_bar = tk.Canvas(self._inner, bg=PANEL_BG, width=ADD_BAR_W,
                                   highlightthickness=0, cursor="hand2")
        self._add_bar.pack(side="left", fill="y")
        self._add_bar.bind("<Configure>", self._draw_add_bar)
        self._add_bar.bind("<Button-1>",  lambda _: self._add_channel())
        self._add_bar.bind("<Enter>",     lambda _: self._add_bar.config(bg="#3a3a3a"))
        self._add_bar.bind("<Leave>",     lambda _: self._add_bar.config(bg=PANEL_BG))

    def _on_inner_configure(self, _=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._update_geometry()

    def _draw_add_bar(self, _=None):
        c = self._add_bar
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        c.create_line(0, 0, 0, h, fill=DIVIDER, width=1)
        c.create_text(w // 2, h // 2, text="+ Add Channel",
                      fill=MUTED, font=("Segoe UI", 9), angle=90, anchor="center")

    def _update_geometry(self, snap=False):
        w = max(COL_WIDTH * len(self._channels) + ADD_BAR_W, COL_MIN_W)
        self.root.minsize(w, COL_MIN_H)
        if snap:
            self.root.geometry(f"{w}x{self.root.winfo_height() or COL_MIN_H}")

    # ── COM names callback (called from gate thread, marshalled to main) ──────

    def _on_names_ready(self, names: list[str]):
        """Called from the COM thread; marshal device list to the main thread."""
        self.root.after(0, self._apply_com_names, names)

    def _apply_com_names(self, names: list[str]):
        self._com_names = names
        for ch in self._channels:
            ch.update_device_list(names)
            ch.attach_initial()

    # ── channel management ────────────────────────────────────────────────────

    def _restore_channels(self):
        """
        Build channel UI from prefs. Device lists are populated later when
        _apply_com_names() arrives from the COM thread.
        """
        count = self._prefs.get("channel_count", 2)
        for i in range(count):
            self._add_channel(ChannelState.from_prefs(self._prefs.get(f"ch{i}", {})),
                              defer_attach=True)

    def _add_channel(self, state: ChannelState | None = None, defer_attach: bool = False):
        state = state or ChannelState()
        idx   = len(self._channels)
        uid   = state.uid

        def on_set_mute(muted: bool):
            self._gate_q.put(("mute", uid, muted))

        def on_attach(name: str):
            def cb(result):
                self.root.after(0, ch.on_attach_result, result)
            self._gate_q.put(("attach", uid, (name, cb)))

        def on_remove_gate():
            self._gate_q.put(("remove", uid, None))

        def is_capturing() -> bool:
            return self._capturing is ch

        def start_capture():
            self._capturing = ch

        def finish_capture(label: str):
            self._capturing = None
            ch._do_finish_capture(label)

        def on_keybinds_changed():
            self._rebuild_keybind_index()

        ch = PTTChannel(state, idx, self.root,
                        on_set_mute, on_attach, on_remove_gate,
                        is_capturing, start_capture, finish_capture,
                        on_keybinds_changed,
                        self._remove_channel, self._save_all)

        self._channels.append(ch)
        self._rebuild_keybind_index()

        if idx > 0:
            div = tk.Frame(self._inner, bg=DIVIDER, width=1)
            div.pack(side="left", fill="y", before=self._add_bar)
            ch._divider_frame = div

        ch.build_column(self._inner).pack(side="left", fill="y", before=self._add_bar)

        if self._com_names:
            ch.update_device_list(self._com_names)
            if not defer_attach:
                ch.attach_initial()

        if not defer_attach:
            self._save_all()
        self._update_geometry(snap=True)

    def _rebuild_keybind_index(self):
        self._keybind_index.clear()
        for ch in self._channels:
            for label in ch.state.keybinds:
                self._keybind_index.setdefault(label, []).append(ch)

    def _remove_channel(self, ch: PTTChannel):
        if self._capturing is ch:
            self._capturing = None
        self._channels.pop(self._channels.index(ch))
        ch.destroy()
        self._rebuild_keybind_index()
        for i, c in enumerate(self._channels):
            c.update_display_index(i)
        self._save_all()
        self._update_geometry(snap=True)

    # ── prefs ─────────────────────────────────────────────────────────────────

    def _save_all(self):
        data = {"channel_count": len(self._channels)}
        data.update({f"ch{i}": ch.state.to_prefs()
                     for i, ch in enumerate(self._channels)})
        self._prefs = data
        save_prefs(data)

    # ── input listeners ───────────────────────────────────────────────────────

    def _start_listeners(self):
        self._kb_listener = keyboard.Listener(
            on_press  = lambda k: self._dispatch(key_to_label(k), True),
            on_release= lambda k: self._dispatch(key_to_label(k), False))
        self._kb_listener.start()
        self._ms_listener = mouse.Listener(
            on_click=lambda x, y, b, p: self._dispatch(
                MOUSE_MAP.get(b, str(b)), p))
        self._ms_listener.start()

    def _dispatch(self, label: str, pressed: bool):
        if pressed and self._capturing is not None:
            self._capturing.handle_press(label)
            return
        fn = "handle_press" if pressed else "handle_release"
        for ch in list(self._keybind_index.get(label, [])):
            getattr(ch, fn)(label)

    # ── shutdown ──────────────────────────────────────────────────────────────

    def _on_close(self):
        self._save_all()
        for ch in self._channels:
            ch.cancel_timers()
        try:
            self._kb_listener.stop()
            self._ms_listener.stop()
        except Exception:
            pass
        self._gate_q.put(("close", None, None))
        self._gate_thread.join(timeout=1.0)
        self.root.destroy()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.minsize(COL_MIN_W, COL_MIN_H)
    PushToTalkApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()