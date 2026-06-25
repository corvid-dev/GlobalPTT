"""
GlobalPTT — Push-to-Talk for Windows 11
Requirements: pip install sounddevice pynput pycaw comtypes
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
import sounddevice as sd
from pynput import keyboard, mouse
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize, CoCreateInstance, GUID

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
    device_name:      str       = NO_DEVICE
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
    def _friendly_name(ep) -> str:
        try:
            store = ep.OpenPropertyStore(0)
            for i in range(store.GetCount()):
                pk = store.GetAt(i)
                if pk.fmtid == _PKEY_Device_FriendlyName and pk.pid == 14:
                    return str(store.GetValue(pk).GetValue())
        except Exception:
            pass
        return ""

    @staticmethod
    def _capture_endpoints():
        from pycaw.pycaw import IMMDeviceEnumerator
        col = CoCreateInstance(
            _CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL
        ).EnumAudioEndpoints(2, 1)
        for i in range(col.GetCount()):
            yield col.Item(i)

    def _activate(self, ep):
        iface = ep.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self._vol = iface.QueryInterface(IAudioEndpointVolume)
        self._orig_mute   = bool(self._vol.GetMute())
        self._orig_volume = self._vol.GetMasterVolumeLevelScalar()
        # probe whether SetMute actually works
        try:
            self._vol.SetMute(1, None)
            self._fallback = not bool(self._vol.GetMute())
            self._vol.SetMute(0, None)
        except Exception:
            self._fallback = True

    def attach(self, name: str) -> str:
        if not name or name == NO_DEVICE:
            return NO_DEVICE
        self._vol = None
        needle = name.lower().strip()
        candidates = []
        try:
            mic = AudioUtilities.GetMicrophone()
            if mic:
                candidates.append(mic)
        except Exception:
            pass
        try:
            candidates.extend(self._capture_endpoints())
        except Exception:
            pass
        for ep in candidates:
            friendly = self._friendly_name(ep).lower().strip()
            if friendly and (friendly.startswith(needle) or needle.startswith(friendly)):
                self._activate(ep)
                return friendly
        return ""

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
    """Single COM thread. All gate dict keys are stable channel UIDs."""

    def __init__(self, q: queue.Queue, on_attach):
        self._q        = q
        self._on_attach = on_attach
        self._gates: dict[str, WinMicGate] = {}

    def run(self):
        CoInitialize()
        try:
            while True:
                try:
                    cmd, uid, arg = self._q.get(timeout=0.2)
                except queue.Empty:
                    continue

                if cmd == "attach":
                    g = self._gates.setdefault(uid, WinMicGate())
                    g.restore(); g._vol = None
                    name = g.attach(arg)
                    if name and name != NO_DEVICE:
                        g.set_mute(True)
                    self._on_attach(uid, name)

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
                 root: tk.Tk, gate_q: queue.Queue,
                 capturing_ref: list, keybind_index: dict,
                 on_remove, on_change):
        self.state          = state
        self.display_index  = display_index
        self.root           = root
        self.gate_q         = gate_q
        self._capturing_ref = capturing_ref
        self._keybind_index = keybind_index
        self._on_remove     = on_remove
        self._on_change     = on_change

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

    def build_column(self, parent, dev_names, dev_indices) -> tk.Frame:
        self._dev_names   = dev_names
        self._dev_indices = dev_indices

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

        # device picker
        mk_label(body, "Input Device", anchor="w")
        self._device_cb = ttk.Combobox(body, state="readonly", font=("Segoe UI", 9))
        self._device_cb["values"] = [NO_DEVICE] + dev_names
        self._device_cb.pack(fill="x", pady=(2, 8))
        self._device_cb.bind("<<ComboboxSelected>>", self._on_device_change)
        sel = next((i + 1 for i, n in enumerate(dev_names)
                    if n.lower() == self.state.device_name.lower()), 0)
        self._device_cb.current(sel)
        if sel == 0:
            self.state.device_name = NO_DEVICE

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

    # ── device ────────────────────────────────────────────────────────────────

    def _on_device_change(self, _=None):
        sel = self._device_cb.current()
        self.state.device_name = NO_DEVICE if sel == 0 else self._device_cb["values"][sel]
        self.gate_q.put(("attach", self.state.uid, self.state.device_name))
        self._on_change()

    def attach_initial(self):
        if self.state.device_name != NO_DEVICE:
            self.gate_q.put(("attach", self.state.uid, self.state.device_name))

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
        bucket = self._keybind_index.get(label, [])
        try:    bucket.remove(self)
        except ValueError: pass
        if not bucket:
            self._keybind_index.pop(label, None)
        self._rebuild_bind_list()
        self._on_change()

    def _start_capture(self):
        if self._capturing_ref[0] == self.state.uid:
            return
        self._capturing_ref[0] = self.state.uid
        self._add_btn.config(state="disabled")
        self._capture_lbl.config(text="Press a key…")

    def _finish_capture(self, label: str):
        self._capturing_ref[0] = None
        with self._lock:
            if label and label not in self.state.keybinds:
                self.state.keybinds.append(label)
                self._keybind_index.setdefault(label, []).append(self)
                self._rebuild_bind_list()
                self._on_change()
        self.root.after(0, self._capture_lbl.config, {"text": ""})
        self.root.after(0, self._add_btn.config, {"state": "normal"})

    # ── input events ──────────────────────────────────────────────────────────

    def handle_press(self, label: str) -> bool:
        if self._capturing_ref[0] == self.state.uid:
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
                self.gate_q.put(("mute", self.state.uid, False))
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
        self.gate_q.put(("mute", self.state.uid, True))
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
        self.gate_q.put(("mute",   self.state.uid, False))
        self.gate_q.put(("remove", self.state.uid, None))
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
        self.root.title("Push to Talk")
        self.root.configure(bg=APP_BG)
        self.root.resizable(True, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._prefs          = load_prefs()
        self._gate_q         = queue.Queue()
        self._capturing_ref  = [None]
        self._keybind_index: dict[str, list[PTTChannel]] = {}
        self._channels:      list[PTTChannel]            = []

        self._worker     = GateWorker(self._gate_q, self._on_attach_result)
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
        devs = sd.query_devices()
        self._dev_names   = [d["name"] for d in devs if d["max_input_channels"] > 0]
        self._dev_indices = [i for i, d in enumerate(devs) if d["max_input_channels"] > 0]

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

        # vertical add-channel bar
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
                      fill=MUTED, font=("Segoe UI", 8), angle=90, anchor="center")

    def _update_geometry(self, snap=False):
        w = COL_WIDTH * max(len(self._channels), 1) + ADD_BAR_W
        self.root.minsize(w, COL_MIN_H)
        if snap:
            self.root.geometry(f"{w}x{self.root.winfo_height() or COL_MIN_H}")

    # ── channel management ────────────────────────────────────────────────────

    def _restore_channels(self):
        count = self._prefs.get("channel_count", 2)
        for i in range(count):
            self._add_channel(ChannelState.from_prefs(self._prefs.get(f"ch{i}", {})),
                              attach=False)
        for ch in self._channels:
            ch.attach_initial()

    def _add_channel(self, state: ChannelState | None = None, attach: bool = True):
        state = state or ChannelState()
        idx   = len(self._channels)
        ch    = PTTChannel(state, idx, self.root, self._gate_q,
                           self._capturing_ref, self._keybind_index,
                           self._remove_channel, self._save_all)
        for label in state.keybinds:
            self._keybind_index.setdefault(label, []).append(ch)
        self._channels.append(ch)

        if idx > 0:
            div = tk.Frame(self._inner, bg=DIVIDER, width=1)
            div.pack(side="left", fill="y", before=self._add_bar)
            ch._divider_frame = div

        ch.build_column(self._inner, self._dev_names, self._dev_indices
                        ).pack(side="left", fill="y", before=self._add_bar)

        if attach:
            ch.attach_initial()
            self._save_all()
        self._update_geometry(snap=True)

    def _remove_channel(self, ch: PTTChannel):
        if len(self._channels) <= 1:
            return
        for label in ch.state.keybinds:
            bucket = self._keybind_index.get(label, [])
            try:    bucket.remove(ch)
            except ValueError: pass
            if not bucket:
                self._keybind_index.pop(label, None)
        if self._capturing_ref[0] == ch.state.uid:
            self._capturing_ref[0] = None
        self._channels.pop(self._channels.index(ch))
        ch.destroy()
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

    # ── gate callback ─────────────────────────────────────────────────────────

    def _on_attach_result(self, uid: str, name: str):
        for ch in self._channels:
            if ch.state.uid == uid:
                ch.on_attach_result(name)
                break

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
        uid = self._capturing_ref[0]
        if pressed and uid is not None:
            for ch in self._channels:
                if ch.state.uid == uid:
                    ch.handle_press(label)
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
    root.minsize(COL_WIDTH + ADD_BAR_W, COL_MIN_H)
    PushToTalkApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()