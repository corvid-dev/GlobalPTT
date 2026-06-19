"""
GlobalPTT — Push-to-Talk for Windows 11
Requirements: pip install sounddevice pynput pycaw comtypes
"""

import tkinter as tk
from tkinter import ttk
import threading
import queue
import json
import os
import sounddevice as sd
from pynput import keyboard, mouse
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize

# ── constants ─────────────────────────────────────────────────────────────────
APP_BG     = "#1e1e1e"
PANEL_BG   = "#2a2a2a"
ACCENT     = "#00b894"
ACCENT_OFF = "#e17055"
TEXT       = "#ececec"
MUTED      = "#888888"
FONT       = ("Segoe UI", 9)
FONT_SM    = ("Segoe UI", 8)

MOUSE_MAP = {
    mouse.Button.left:   "Mouse-Left",
    mouse.Button.right:  "Mouse-Right",
    mouse.Button.middle: "Mouse-Middle",
    mouse.Button.x1:     "Mouse-X1",
    mouse.Button.x2:     "Mouse-X2",
}

KEY_DISPLAY = {
    "ctrl_l": "L-Ctrl", "ctrl_r": "R-Ctrl",
    "shift": "Shift", "shift_l": "L-Shift", "shift_r": "R-Shift",
    "alt_l": "L-Alt", "alt_r": "R-Alt",
    "Mouse-Left": "Mouse L", "Mouse-Right": "Mouse R",
    "Mouse-Middle": "Mouse M", "Mouse-X1": "Mouse 4",
    "Mouse-X2": "Mouse 5",
}

# ── preferences ───────────────────────────────────────────────────────────────

PREFS_PATH = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "GlobalPTT", "prefs.json")

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

# ── key helpers ───────────────────────────────────────────────────────────────

def key_to_label(k) -> str:
    if isinstance(k, str):
        return k
    try:
        return k.char or str(k).replace("Key.", "")
    except AttributeError:
        return str(k).replace("Key.", "")

def label_to_display(label: str) -> str:
    return KEY_DISPLAY.get(label, label.upper() if len(label) == 1 else label.title())

# ── Windows mic gate ──────────────────────────────────────────────────────────

class WinMicGate:
    """
    Wraps IAudioEndpointVolume for a capture device.
    Must be constructed and used entirely on the same thread (COM requirement).
    Falls back to volume scalar if SetMute has no effect (e.g. Elgato Wave Link).
    """

    def __init__(self):
        CoInitialize()
        self._volume: IAudioEndpointVolume | None = None
        self._original_mute: bool | None          = None
        self._original_volume: float | None       = None
        self._use_volume_fallback                 = False

    @staticmethod
    def _friendly_name(ep) -> str:
        try:
            if hasattr(ep, "FriendlyName") and ep.FriendlyName:
                return ep.FriendlyName
        except Exception:
            pass
        try:
            from pycaw.pycaw import IPropertyStore, STGM_READ
            store = ep.OpenPropertyStore(STGM_READ)
            store = store.QueryInterface(IPropertyStore)
            for i in range(store.GetCount()):
                pk = store.GetAt(i)
                if str(pk.fmtid) == "{a45c254e-df1c-4efd-8020-67d146a850e0}" and pk.pid == 14:
                    return str(store.GetValue(pk).GetValue())
        except Exception:
            pass
        return ""

    def attach(self, device_name: str) -> str:
        self._volume = None
        needle = device_name.lower().strip()
        try:
            mic = AudioUtilities.GetMicrophone()
            if mic:
                friendly = self._friendly_name(mic).lower().strip()
                if not needle or friendly.startswith(needle) or needle.startswith(friendly):
                    iface = mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    self._volume = iface.QueryInterface(IAudioEndpointVolume)
                    self._save_state_and_probe()
                    return friendly or device_name

            for ep in AudioUtilities.GetAllDevices():
                friendly = self._friendly_name(ep).lower().strip()
                if friendly and (friendly.startswith(needle) or needle.startswith(friendly)):
                    iface = ep.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    self._volume = iface.QueryInterface(IAudioEndpointVolume)
                    self._save_state_and_probe()
                    return friendly
        except Exception:
            pass
        return ""

    def _save_state_and_probe(self):
        self._original_mute   = bool(self._volume.GetMute())
        self._original_volume = self._volume.GetMasterVolumeLevelScalar()
        self._use_volume_fallback = False
        try:
            self._volume.SetMute(1, None)
            after = bool(self._volume.GetMute())
            self._volume.SetMute(0, None)
            if not after:
                self._use_volume_fallback = True
        except Exception:
            self._use_volume_fallback = True

    def set_mute(self, muted: bool):
        if not self._volume:
            return
        try:
            if self._use_volume_fallback:
                level = 0.0 if muted else (self._original_volume if self._original_volume is not None else 1.0)
                self._volume.SetMasterVolumeLevelScalar(level, None)
            else:
                self._volume.SetMute(int(muted), None)
        except Exception:
            pass

    def restore(self):
        if not self._volume:
            return
        try:
            if self._use_volume_fallback and self._original_volume is not None:
                self._volume.SetMasterVolumeLevelScalar(self._original_volume, None)
            elif self._original_mute is not None:
                self._volume.SetMute(int(self._original_mute), None)
        except Exception:
            pass

    def close(self):
        self.restore()
        CoUninitialize()


# ── main application ──────────────────────────────────────────────────────────

class PushToTalkApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Push to Talk")
        self.root.configure(bg=APP_BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._lock             = threading.RLock()
        self._prefs            = load_prefs()
        self._active_keys: set = set()
        self._keybinds: list[str] = self._prefs.get("keybinds", [])
        self._talking          = False
        self._release_timer: threading.Timer | None = None
        self._running          = True
        self._capturing        = False
        self._device_name      = ""
        self._device_index: int | None = None

        self._gate_q: queue.Queue = queue.Queue()
        self._gate_thread = threading.Thread(target=self._gate_worker, daemon=True)
        self._gate_thread.start()

        self._build_ui()
        self._rebuild_bind_list()
        self._populate_devices()
        self._start_listeners()
        self._gate_q.put(("mute", True))

    # ── gate worker ───────────────────────────────────────────────────────────

    def _gate_worker(self):
        gate = WinMicGate()
        while self._running:
            try:
                cmd, arg = self._gate_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if cmd == "attach":
                name = gate.attach(arg)
                if not name:
                    self.root.after(0, lambda: self._set_error_ui("Mic not found — check device"))
                else:
                    with self._lock:
                        talking = self._talking
                    gate.set_mute(not talking)
                    self.root.after(0, self._set_muted_ui)
            elif cmd == "mute":
                gate.set_mute(arg)
            elif cmd == "close":
                gate.close()
                break

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = self.root
        pad  = dict(padx=10, pady=6)

        self._status_var = tk.StringVar(value="● MUTED")
        self._status_lbl = tk.Label(root, textvariable=self._status_var,
                                     bg=APP_BG, fg=ACCENT_OFF,
                                     font=("Segoe UI", 10, "bold"), anchor="w")
        self._status_lbl.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)

        tk.Frame(root, bg="#333", height=1).grid(row=1, column=0, columnspan=2, sticky="ew", padx=10)

        tk.Label(root, text="Input Device", bg=APP_BG, fg=MUTED,
                 font=FONT_SM, anchor="w").grid(row=2, column=0, sticky="w", padx=10, pady=(8, 0))

        self._device_var = tk.StringVar()
        self._device_cb  = ttk.Combobox(root, textvariable=self._device_var,
                                         state="readonly", width=30, font=FONT)
        self._device_cb.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(2, 6))
        self._device_cb.bind("<<ComboboxSelected>>", self._on_device_change)

        tk.Frame(root, bg="#333", height=1).grid(row=4, column=0, columnspan=2, sticky="ew", padx=10)

        tk.Label(root, text="Keybinds (any triggers PTT)",
                 bg=APP_BG, fg=MUTED, font=FONT_SM, anchor="w").grid(
            row=5, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 2))

        self._bind_frame = tk.Frame(root, bg=PANEL_BG, bd=0)
        self._bind_frame.grid(row=6, column=0, columnspan=2, sticky="ew", padx=10)
        self._bind_rows: list[tk.Frame] = []

        btn_frame = tk.Frame(root, bg=APP_BG)
        btn_frame.grid(row=7, column=0, columnspan=2, sticky="ew", padx=10, pady=4)

        self._add_btn = tk.Button(btn_frame, text="+ Add Key",
                                   bg=PANEL_BG, fg=TEXT, font=FONT,
                                   relief="flat", cursor="hand2",
                                   activebackground="#444", activeforeground=TEXT,
                                   command=self._start_capture)
        self._add_btn.pack(side="left")

        self._capture_lbl = tk.Label(btn_frame, text="", bg=APP_BG, fg=ACCENT, font=FONT_SM)
        self._capture_lbl.pack(side="left", padx=(8, 0))

        tk.Frame(root, bg="#333", height=1).grid(row=8, column=0, columnspan=2, sticky="ew", padx=10)

        tk.Label(root, text="Release Delay (ms)",
                 bg=APP_BG, fg=MUTED, font=FONT_SM, anchor="w").grid(
            row=9, column=0, sticky="w", padx=10, pady=(8, 2))

        self._delay_var = tk.IntVar(value=self._prefs.get("release_delay_ms", 250))
        tk.Scale(root, variable=self._delay_var,
                 from_=0, to=2000, orient="horizontal",
                 bg=APP_BG, fg=TEXT, troughcolor=PANEL_BG,
                 highlightthickness=0, bd=0,
                 activebackground=ACCENT, font=FONT_SM,
                 sliderlength=14, length=220).grid(
            row=10, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        root.columnconfigure(0, weight=1)
        self._style_combobox()

    def _style_combobox(self):
        s = ttk.Style()
        s.theme_use("default")
        s.configure("TCombobox",
                    fieldbackground=PANEL_BG, background=PANEL_BG,
                    foreground=TEXT, selectbackground=PANEL_BG,
                    selectforeground=TEXT, bordercolor="#444", arrowcolor=TEXT)
        s.map("TCombobox",
              fieldbackground=[("readonly", PANEL_BG)],
              foreground=[("readonly", TEXT)])

    # ── device ────────────────────────────────────────────────────────────────

    def _populate_devices(self):
        devices = sd.query_devices()
        names, indices = [], []
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0:
                names.append(d["name"])
                indices.append(i)
        self._device_indices = indices
        self._device_names   = names
        self._device_cb["values"] = names
        if not names:
            return
        saved = self._prefs.get("device_name", "")
        sel   = next((i for i, n in enumerate(names) if n.lower() == saved.lower()), None)
        if sel is None:
            try:
                default = sd.default.device[0]
                sel = indices.index(default) if default in indices else 0
            except Exception:
                sel = 0
        self._device_cb.current(sel)
        self._device_index = indices[sel]
        self._device_name  = names[sel]
        self._gate_q.put(("attach", self._device_name))

    def _on_device_change(self, _event=None):
        sel = self._device_cb.current()
        if sel >= 0:
            self._device_index         = self._device_indices[sel]
            self._device_name          = self._device_names[sel]
            self._prefs["device_name"] = self._device_name
            save_prefs(self._prefs)
            self._gate_q.put(("attach", self._device_name))

    # ── keybind UI ────────────────────────────────────────────────────────────

    def _rebuild_bind_list(self):
        for row in self._bind_rows:
            row.destroy()
        self._bind_rows.clear()
        for label in self._keybinds:
            row = tk.Frame(self._bind_frame, bg=PANEL_BG)
            row.pack(fill="x", padx=4, pady=2)
            tk.Label(row, text=label_to_display(label),
                     bg=PANEL_BG, fg=TEXT, font=FONT,
                     width=14, anchor="w").pack(side="left", padx=(6, 0))
            tk.Button(row, text="✕", bg=PANEL_BG, fg=MUTED,
                      font=FONT_SM, relief="flat", cursor="hand2",
                      activebackground=PANEL_BG, activeforeground=ACCENT_OFF,
                      command=lambda l=label: self._remove_bind(l)).pack(side="right", padx=4)
            self._bind_rows.append(row)

    def _save_keybinds(self):
        self._prefs["keybinds"] = self._keybinds
        save_prefs(self._prefs)

    def _remove_bind(self, label):
        with self._lock:
            if label in self._keybinds:
                self._keybinds.remove(label)
        self._save_keybinds()
        self._rebuild_bind_list()

    def _start_capture(self):
        if self._capturing:
            return
        self._capturing = True
        self._add_btn.config(state="disabled")
        self._capture_lbl.config(text="Press a key or mouse button…")

    def _finish_capture(self, label):
        if not self._capturing:
            return
        self._capturing = False
        if label and label not in self._keybinds:
            self._keybinds.append(label)
            self._save_keybinds()
            self._rebuild_bind_list()
        self.root.after(0, lambda: self._capture_lbl.config(text=""))
        self.root.after(0, lambda: self._add_btn.config(state="normal"))

    # ── listeners ─────────────────────────────────────────────────────────────

    def _start_listeners(self):
        self._kb_listener = keyboard.Listener(
            on_press=self._on_key_press, on_release=self._on_key_release)
        self._kb_listener.start()
        self._ms_listener = mouse.Listener(on_click=self._on_mouse_click)
        self._ms_listener.start()

    def _on_key_press(self, key):
        label = key_to_label(key)
        if self._capturing:
            self._finish_capture(label)
            return
        with self._lock:
            if label in self._keybinds:
                self._active_keys.add(label)
                activate = True
            else:
                activate = False
        if activate:
            self._ptt_activate()

    def _on_key_release(self, key):
        label = key_to_label(key)
        with self._lock:
            self._active_keys.discard(label)
            any_held = any(k in self._active_keys for k in self._keybinds)
        if not any_held:
            self._ptt_deactivate()

    def _on_mouse_click(self, x, y, button, pressed):
        label = MOUSE_MAP.get(button, str(button))
        if self._capturing and pressed:
            self._finish_capture(label)
            return
        with self._lock:
            if label not in self._keybinds:
                return
            if pressed:
                self._active_keys.add(label)
            else:
                self._active_keys.discard(label)
            any_held = any(k in self._active_keys for k in self._keybinds)
        if pressed:
            self._ptt_activate()
        elif not any_held:
            self._ptt_deactivate()

    # ── PTT logic ─────────────────────────────────────────────────────────────

    def _ptt_activate(self):
        with self._lock:
            if self._release_timer:
                self._release_timer.cancel()
                self._release_timer = None
            if self._talking:
                return
            self._talking = True
        self._gate_q.put(("mute", False))
        self.root.after(0, self._set_talking_ui)

    def _ptt_deactivate(self):
        with self._lock:
            if not self._talking:
                return
            delay_ms = self._delay_var.get()
            if self._release_timer:
                self._release_timer.cancel()
            if delay_ms == 0:
                self._release_timer = None
                go_now = True
            else:
                t = threading.Timer(delay_ms / 1000.0, self._do_deactivate)
                t.daemon = True
                self._release_timer = t
                go_now = False
        if go_now:
            self._do_deactivate()
        else:
            t.start()

    def _do_deactivate(self):
        with self._lock:
            self._release_timer = None
            if any(k in self._active_keys for k in self._keybinds):
                return
            if not self._talking:
                return
            self._talking = False
        self._gate_q.put(("mute", True))
        self.root.after(0, self._set_muted_ui)

    # ── UI state ──────────────────────────────────────────────────────────────

    def _set_talking_ui(self):
        self._status_var.set("● LIVE")
        self._status_lbl.config(fg=ACCENT)

    def _set_muted_ui(self):
        self._status_var.set("● MUTED")
        self._status_lbl.config(fg=ACCENT_OFF)

    def _set_error_ui(self, msg):
        self._status_var.set(f"⚠ {msg[:40]}")
        self._status_lbl.config(fg=ACCENT_OFF)

    # ── shutdown ──────────────────────────────────────────────────────────────

    def _on_close(self):
        self._running = False
        self._prefs["release_delay_ms"] = self._delay_var.get()
        save_prefs(self._prefs)
        with self._lock:
            if self._release_timer:
                self._release_timer.cancel()
                self._release_timer = None
        try:
            self._kb_listener.stop()
            self._ms_listener.stop()
        except Exception:
            pass
        self._gate_q.put(("close", None))
        self._gate_thread.join(timeout=1.0)
        self.root.destroy()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    root.minsize(280, 340)
    PushToTalkApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
