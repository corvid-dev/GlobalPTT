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

NO_DEVICE = "— None —"

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

NUM_CHANNELS = 2

# Windows COM GUIDs — stable, from Windows SDK
_CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_PKEY_Device_FriendlyName = GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}")

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
    Wraps IAudioEndpointVolume for one capture device.
    Must be constructed and used on a single COM thread.
    Falls back to volume scalar when SetMute has no effect (e.g. Elgato Wave Link).
    """

    def __init__(self):
        self._volume: IAudioEndpointVolume | None = None
        self._original_mute: bool | None          = None
        self._original_volume: float | None       = None
        self._use_volume_fallback                 = False

    @staticmethod
    def _friendly_name(ep) -> str:
        """Read device friendly name from raw COM property store."""
        try:
            store = ep.OpenPropertyStore(0)  # STGM_READ = 0
            for i in range(store.GetCount()):
                pk = store.GetAt(i)
                if pk.fmtid == _PKEY_Device_FriendlyName and pk.pid == 14:
                    return str(store.GetValue(pk).GetValue())
        except Exception:
            pass
        return ""

    @staticmethod
    def _enum_capture_endpoints():
        """Yield active capture IMMDevice endpoints via COM enumerator."""
        from pycaw.pycaw import IMMDeviceEnumerator
        enumerator = CoCreateInstance(
            _CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        collection = enumerator.EnumAudioEndpoints(2, 1)  # eCapture, DEVICE_STATE_ACTIVE
        for i in range(collection.GetCount()):
            yield collection.Item(i)

    def attach(self, device_name: str) -> str:
        """
        Attach to the named capture device.
        Returns NO_DEVICE if intentionally empty, friendly name on success, '' on failure.
        """
        if not device_name or device_name == NO_DEVICE:
            return NO_DEVICE

        self._volume = None
        needle = device_name.lower().strip()

        # Check Windows default microphone first
        try:
            mic = AudioUtilities.GetMicrophone()
            if mic:
                friendly = self._friendly_name(mic).lower().strip()
                if friendly and (friendly.startswith(needle) or needle.startswith(friendly)):
                    iface = mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    self._volume = iface.QueryInterface(IAudioEndpointVolume)
                    self._save_state_and_probe()
                    return friendly
        except Exception:
            pass

        # Fall through to full device enumeration
        try:
            for ep in self._enum_capture_endpoints():
                friendly = self._friendly_name(ep).lower().strip()
                if friendly and (friendly.startswith(needle) or needle.startswith(friendly)):
                    iface = ep.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    self._volume = iface.QueryInterface(IAudioEndpointVolume)
                    self._save_state_and_probe()
                    return friendly
        except Exception:
            pass

        return ""

    def detach(self):
        self._volume = None

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
                level = 0.0 if muted else (self._original_volume or 1.0)
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


# ── gate worker ───────────────────────────────────────────────────────────────

class GateWorker:
    """
    Single COM thread owning all WinMicGate instances.
    Commands: ("attach", slot, device_name)
              ("mute",   slot, bool)
              ("close",  None, None)
    """

    def __init__(self, q: queue.Queue, on_attach_result):
        self._q = q
        self._on_attach_result = on_attach_result
        self._gates = [WinMicGate() for _ in range(NUM_CHANNELS)]

    def run(self):
        CoInitialize()
        try:
            while True:
                try:
                    cmd, slot, arg = self._q.get(timeout=0.2)
                except queue.Empty:
                    continue

                if cmd == "attach":
                    g = self._gates[slot]
                    g.restore()
                    g.detach()
                    name = g.attach(arg)
                    if name and name != NO_DEVICE:
                        g.set_mute(True)
                    self._on_attach_result(slot, name)

                elif cmd == "mute":
                    self._gates[slot].set_mute(arg)

                elif cmd == "close":
                    for g in self._gates:
                        g.close()
                    break
        finally:
            CoUninitialize()


# ── per-channel PTT controller ───────────────────────────────────────────────

class PTTChannel:
    """
    One column: device, keybinds, delay, talking state.
    Input events arrive from listener threads; UI updates go via root.after().
    """

    def __init__(self, slot: int, root: tk.Tk, gate_q: queue.Queue,
                 prefs: dict, capturing_slot: list):
        self.slot            = slot
        self.root            = root
        self.gate_q          = gate_q
        self.prefs           = prefs
        self._capturing_slot = capturing_slot

        ch_prefs = prefs.get(f"ch{slot}", {})

        self._lock         = threading.RLock()
        self._active_keys: set            = set()
        self._keybinds: list[str]         = list(ch_prefs.get("keybinds", []))
        self._talking                     = False
        self._release_timer: threading.Timer | None = None
        self._device_name                 = ch_prefs.get("device_name", NO_DEVICE)
        self._device_index: int | None    = None
        self._device_indices: list[int]   = []
        self._delay_var: tk.IntVar | None = None

        self.frame        = None
        self._status_var  = None
        self._status_lbl  = None
        self._device_cb   = None
        self._bind_frame  = None
        self._bind_rows   = []
        self._add_btn     = None
        self._capture_lbl = None

    def _save(self):
        self.prefs.setdefault(f"ch{self.slot}", {}).update({
            "keybinds":         self._keybinds,
            "device_name":      self._device_name,
            "release_delay_ms": self._delay_var.get() if self._delay_var else 250,
        })
        save_prefs(self.prefs)

    def build_column(self, parent: tk.Frame,
                     device_names: list[str], device_indices: list[int]):
        self._device_indices = device_indices
        self.frame = tk.Frame(parent, bg=COL_BG)

        # header
        header = tk.Frame(self.frame, bg=PANEL_BG)
        header.pack(fill="x")
        tk.Label(header, text=f"Channel {self.slot + 1}",
                 bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(side="left", padx=10, pady=6)
        self._status_var = tk.StringVar(value="○ INACTIVE")
        self._status_lbl = tk.Label(header, textvariable=self._status_var,
                                    bg=PANEL_BG, fg=MUTED,
                                    font=("Segoe UI", 9, "bold"), anchor="e")
        self._status_lbl.pack(side="right", padx=10, pady=6)

        tk.Frame(self.frame, bg=DIVIDER, height=1).pack(fill="x")

        body = tk.Frame(self.frame, bg=COL_BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)

        # device — NO_DEVICE always at index 0
        tk.Label(body, text="Input Device", bg=COL_BG, fg=MUTED,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x")
        self._device_cb = ttk.Combobox(body, state="readonly", font=("Segoe UI", 9))
        self._device_cb["values"] = [NO_DEVICE] + device_names
        self._device_cb.pack(fill="x", pady=(2, 8))
        self._device_cb.bind("<<ComboboxSelected>>", self._on_device_change)

        saved = self._device_name
        sel = next((i + 1 for i, n in enumerate(device_names)
                    if n.lower() == saved.lower()), 0)
        self._device_cb.current(sel)
        if sel > 0:
            self._device_index = device_indices[sel - 1]
            self._device_name  = device_names[sel - 1]
        else:
            self._device_name = NO_DEVICE

        tk.Frame(body, bg=DIVIDER, height=1).pack(fill="x", pady=(0, 8))

        # delay
        ch_prefs = self.prefs.get(f"ch{self.slot}", {})
        self._delay_var = tk.IntVar(value=ch_prefs.get("release_delay_ms", 250))
        tk.Label(body, text="Release Delay (ms)", bg=COL_BG, fg=MUTED,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x")
        tk.Scale(body, variable=self._delay_var,
                 from_=0, to=2000, orient="horizontal",
                 bg=COL_BG, fg=TEXT, troughcolor=PANEL_BG,
                 highlightthickness=0, bd=0,
                 activebackground=ACCENT, font=("Segoe UI", 8),
                 sliderlength=14).pack(fill="x", pady=(2, 8))

        tk.Frame(body, bg=DIVIDER, height=1).pack(fill="x", pady=(0, 8))

        # keybinds
        tk.Label(body, text="Keybinds", bg=COL_BG, fg=MUTED,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x")
        self._bind_frame = tk.Frame(body, bg=PANEL_BG)
        self._bind_frame.pack(fill="x", pady=(2, 6))

        btn_row = tk.Frame(body, bg=COL_BG)
        btn_row.pack(fill="x")
        self._add_btn = tk.Button(btn_row, text="+ Add Key",
                                   bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 9),
                                   relief="flat", cursor="hand2",
                                   activebackground="#444", activeforeground=TEXT,
                                   command=self._start_capture)
        self._add_btn.pack(side="left")
        self._capture_lbl = tk.Label(btn_row, text="",
                                      bg=COL_BG, fg=ACCENT, font=("Segoe UI", 8))
        self._capture_lbl.pack(side="left", padx=(8, 0))

        self._rebuild_bind_list()
        return self.frame

    def _on_device_change(self, _event=None):
        sel = self._device_cb.current()
        if sel == 0:
            self._device_name  = NO_DEVICE
            self._device_index = None
            self._save()
            self.gate_q.put(("attach", self.slot, NO_DEVICE))
        elif sel > 0:
            self._device_index = self._device_indices[sel - 1]
            self._device_name  = self._device_cb["values"][sel]
            self._save()
            self.gate_q.put(("attach", self.slot, self._device_name))

    def attach_initial(self):
        if self._device_name and self._device_name != NO_DEVICE:
            self.gate_q.put(("attach", self.slot, self._device_name))

    def on_attach_ok(self):
        self.root.after(0, self._set_muted_ui)

    def on_attach_none(self):
        self.root.after(0, self._set_inactive_ui)

    def on_attach_fail(self):
        self.root.after(0, lambda: self._set_error_ui("Mic not found"))

    def _rebuild_bind_list(self):
        for row in self._bind_rows:
            row.destroy()
        self._bind_rows.clear()
        for label in self._keybinds:
            row = tk.Frame(self._bind_frame, bg=PANEL_BG)
            row.pack(fill="x", padx=4, pady=2)
            tk.Label(row, text=label_to_display(label),
                     bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 9),
                     anchor="w").pack(side="left", padx=(6, 0))
            tk.Button(row, text="✕", bg=PANEL_BG, fg=MUTED,
                      font=("Segoe UI", 8), relief="flat", cursor="hand2",
                      activebackground=PANEL_BG, activeforeground=ACCENT_OFF,
                      command=lambda l=label: self._remove_bind(l)).pack(
                          side="right", padx=4)
            self._bind_rows.append(row)

    def _remove_bind(self, label):
        with self._lock:
            if label in self._keybinds:
                self._keybinds.remove(label)
        self._save()
        self._rebuild_bind_list()

    def _start_capture(self):
        if self._capturing_slot[0] == self.slot:
            return
        self._capturing_slot[0] = self.slot
        self._add_btn.config(state="disabled")
        self._capture_lbl.config(text="Press a key…")

    def _finish_capture(self, label):
        self._capturing_slot[0] = -1
        if label and label not in self._keybinds:
            self._keybinds.append(label)
            self._save()
            self._rebuild_bind_list()
        self.root.after(0, lambda: self._capture_lbl.config(text=""))
        self.root.after(0, lambda: self._add_btn.config(state="normal"))

    def handle_key_press(self, label: str) -> bool:
        if self._capturing_slot[0] == self.slot:
            self._finish_capture(label)
            return True
        with self._lock:
            if label in self._keybinds:
                self._active_keys.add(label)
                self._ptt_activate()
        return False

    def handle_key_release(self, label: str):
        with self._lock:
            self._active_keys.discard(label)
            any_held = any(k in self._active_keys for k in self._keybinds)
        if not any_held:
            self._ptt_deactivate()

    def handle_mouse_press(self, label: str) -> bool:
        if self._capturing_slot[0] == self.slot:
            self._finish_capture(label)
            return True
        with self._lock:
            if label in self._keybinds:
                self._active_keys.add(label)
                self._ptt_activate()
        return False

    def handle_mouse_release(self, label: str):
        with self._lock:
            self._active_keys.discard(label)
            any_held = any(k in self._active_keys for k in self._keybinds)
        if not any_held:
            self._ptt_deactivate()

    def _ptt_activate(self):
        if self._device_name == NO_DEVICE:
            return
        with self._lock:
            if self._release_timer:
                self._release_timer.cancel()
                self._release_timer = None
            if self._talking:
                return
            self._talking = True
        self.gate_q.put(("mute", self.slot, False))
        self.root.after(0, self._set_talking_ui)

    def _ptt_deactivate(self):
        with self._lock:
            if not self._talking:
                return
            delay_ms = self._delay_var.get() if self._delay_var else 0
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
        self.gate_q.put(("mute", self.slot, True))
        self.root.after(0, self._set_muted_ui)

    def cancel_timers(self):
        with self._lock:
            if self._release_timer:
                self._release_timer.cancel()
                self._release_timer = None

    def _set_talking_ui(self):
        self._status_var.set("● LIVE")
        self._status_lbl.config(fg=ACCENT)

    def _set_muted_ui(self):
        self._status_var.set("● MUTED")
        self._status_lbl.config(fg=ACCENT_OFF)

    def _set_inactive_ui(self):
        self._status_var.set("○ INACTIVE")
        self._status_lbl.config(fg=MUTED)

    def _set_error_ui(self, msg: str):
        self._status_var.set(f"⚠ {msg[:24]}")
        self._status_lbl.config(fg=ACCENT_OFF)


# ── main application ──────────────────────────────────────────────────────────

class PushToTalkApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Push to Talk")
        self.root.configure(bg=APP_BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._prefs  = load_prefs()
        self._gate_q: queue.Queue = queue.Queue()

        capturing_slot = [-1]
        self._channels: list[PTTChannel] = [
            PTTChannel(i, root, self._gate_q, self._prefs, capturing_slot)
            for i in range(NUM_CHANNELS)
        ]

        self._worker = GateWorker(self._gate_q, self._on_attach_result)
        self._gate_thread = threading.Thread(target=self._worker.run, daemon=True)
        self._gate_thread.start()

        self._build_ui()
        self._start_listeners()

        self.root.update_idletasks()
        self._set_icon()

    def _set_icon(self):
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            self.root.wm_iconbitmap(os.path.join(base, "GlobalPTTIcon.ico"))
        except Exception:
            pass

    def _on_attach_result(self, slot: int, name: str):
        ch = self._channels[slot]
        if name == NO_DEVICE:
            ch.on_attach_none()
        elif name:
            ch.on_attach_ok()
        else:
            ch.on_attach_fail()

    def _build_ui(self):
        devices = sd.query_devices()
        names   = [d["name"] for d in devices if d["max_input_channels"] > 0]
        indices = [i for i, d in enumerate(devices) if d["max_input_channels"] > 0]

        style = ttk.Style()
        style.theme_use("default")
        style.configure("TCombobox",
                        fieldbackground=PANEL_BG, background=PANEL_BG,
                        foreground=TEXT, selectbackground=PANEL_BG,
                        selectforeground=TEXT, bordercolor="#444", arrowcolor=TEXT)
        style.map("TCombobox",
                  fieldbackground=[("readonly", PANEL_BG)],
                  foreground=[("readonly", TEXT)])

        container = tk.Frame(self.root, bg=APP_BG)
        container.pack(fill="both", expand=True)

        for i, ch in enumerate(self._channels):
            col = ch.build_column(container, names, indices)
            col.pack(side="left", fill="both", expand=True)
            if i < NUM_CHANNELS - 1:
                tk.Frame(container, bg=DIVIDER, width=1).pack(side="left", fill="y")
            ch.attach_initial()

    def _start_listeners(self):
        self._kb_listener = keyboard.Listener(
            on_press=self._on_key_press, on_release=self._on_key_release)
        self._kb_listener.start()
        self._ms_listener = mouse.Listener(on_click=self._on_mouse_click)
        self._ms_listener.start()

    def _on_key_press(self, key):
        label = key_to_label(key)
        for ch in self._channels:
            if ch.handle_key_press(label):
                break

    def _on_key_release(self, key):
        label = key_to_label(key)
        for ch in self._channels:
            ch.handle_key_release(label)

    def _on_mouse_click(self, x, y, button, pressed):
        label = MOUSE_MAP.get(button, str(button))
        if pressed:
            for ch in self._channels:
                if ch.handle_mouse_press(label):
                    break
        else:
            for ch in self._channels:
                ch.handle_mouse_release(label)

    def _on_close(self):
        for ch in self._channels:
            ch.cancel_timers()
            ch._save()
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
    root.minsize(480, 200)
    PushToTalkApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()