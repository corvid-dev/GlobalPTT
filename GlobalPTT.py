"""
GlobalPTT v2.3 — Push-to-Talk for Windows 11
pip install pynput pycaw comtypes
"""
import os, sys, json, uuid, queue, threading
from dataclasses import dataclass, field
from functools import lru_cache
import tkinter as tk
from tkinter import ttk
from pynput import keyboard, mouse
from pycaw.pycaw import IAudioEndpointVolume, IMMDeviceEnumerator
from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize, CoCreateInstance, GUID

VERSION = "2.3"

# theme
APP_BG=("#1e1e1e"); PANEL_BG="#2a2a2a"; COL_BG="#252525"; DIVIDER="#333333"
ACCENT="#00b894"; ACCENT_OFF="#e17055"; TEXT="#ececec"; MUTED="#888888"

# layout
COL_W=350; ADD_BAR_W=50; MIN_H=320

# COM
_CLSID_ENUM = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_PKEY_NAME  = GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}")

PREFS_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GlobalPTT", "prefs.json")

MOUSE_LABELS = {
    mouse.Button.left:"Mouse-Left", mouse.Button.right:"Mouse-Right",
    mouse.Button.middle:"Mouse-Middle", mouse.Button.x1:"Mouse-X1", mouse.Button.x2:"Mouse-X2",
}
DISPLAY_NAMES = {
    "ctrl_l":"L-Ctrl","ctrl_r":"R-Ctrl","shift_l":"L-Shift","shift_r":"R-Shift","shift":"Shift",
    "alt_l":"L-Alt","alt_r":"R-Alt","Mouse-Left":"Mouse L","Mouse-Right":"Mouse R",
    "Mouse-Middle":"Mouse M","Mouse-X1":"Mouse 4","Mouse-X2":"Mouse 5",
}

def key_label(k):
    if isinstance(k, str): return k
    try: return k.char or str(k).replace("Key.","")
    except: return str(k).replace("Key.","")

@lru_cache(maxsize=256)
def disp(label):
    return DISPLAY_NAMES.get(label, label.upper() if len(label)==1 else label.title())

def load_prefs():
    try:
        with open(PREFS_PATH, encoding="utf-8") as f: return json.load(f)
    except: return {}

def save_prefs(data):
    try:
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with open(PREFS_PATH,"w",encoding="utf-8") as f: json.dump(data,f,indent=2)
    except: pass


# ── data ───────────────────────────────────────────────────────────────────────

@dataclass
class ChannelState:
    uid:      str       = field(default_factory=lambda: str(uuid.uuid4()))  # runtime only
    ep_id:    str       = ""
    keybinds: list[str] = field(default_factory=list)
    delay_ms: int       = 250

    @staticmethod
    def from_dict(d): return ChannelState(ep_id=d.get("ep_id",""), keybinds=list(d.get("keybinds",[])), delay_ms=d.get("delay_ms",250))
    def to_dict(self): return {"ep_id":self.ep_id,"keybinds":list(self.keybinds),"delay_ms":self.delay_ms}


# ── COM audio ──────────────────────────────────────────────────────────────────

class MicGate:
    def __init__(self):
        self._vol=None; self._orig_mute=False; self._orig_vol=1.0; self._use_vol=False

    def activate(self, enum, ep_id):
        self.deactivate()
        try:
            vol = enum.GetDevice(ep_id).Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
            self._orig_mute=bool(vol.GetMute()); self._orig_vol=vol.GetMasterVolumeLevelScalar()
            vol.SetMute(1,None); self._use_vol=not bool(vol.GetMute()); vol.SetMute(0,None)
            self._vol=vol; return True
        except: self.deactivate(); return False

    def set_mute(self, muted):
        if not self._vol: return
        try:
            if self._use_vol: self._vol.SetMasterVolumeLevelScalar(0.0 if muted else self._orig_vol, None)
            else: self._vol.SetMute(int(muted), None)
        except: pass

    def deactivate(self):
        if not self._vol: return
        try:
            if self._use_vol: self._vol.SetMasterVolumeLevelScalar(self._orig_vol, None)
            else: self._vol.SetMute(int(self._orig_mute), None)
        except: pass
        self._vol=None


class AudioThread:
    def __init__(self, root):
        self._root=root; self._q=queue.Queue(); self._gates={}

    def send(self, cmd, uid, arg): self._q.put((cmd,uid,arg))

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        CoInitialize()
        try:
            enum = CoCreateInstance(_CLSID_ENUM, IMMDeviceEnumerator, CLSCTX_ALL)
            while True:
                try: cmd,uid,arg = self._q.get(timeout=0.2)
                except queue.Empty: continue

                if cmd=="enumerate":
                    self._root.after(0, arg, self._enumerate(enum))
                elif cmd=="attach":
                    ep_id,cb = arg
                    g = self._gates.setdefault(uid, MicGate())
                    g.deactivate()
                    if ep_id:
                        ok = g.activate(enum, ep_id)
                        if ok: g.set_mute(True)
                        self._root.after(0, cb, ok)
                    else:
                        self._root.after(0, cb, None)
                elif cmd=="mute":
                    if uid in self._gates: self._gates[uid].set_mute(arg)
                elif cmd=="remove":
                    if uid in self._gates: self._gates.pop(uid).deactivate()
                elif cmd=="quit":
                    for g in self._gates.values(): g.deactivate()
                    break
        finally: CoUninitialize()

    def _enumerate(self, enum):
        result={}; seen_ids=set(); name_counts={}
        def name(ep):
            try:
                store=ep.OpenPropertyStore(0)
                for i in range(store.GetCount()):
                    pk=store.GetAt(i)
                    if pk.fmtid==_PKEY_NAME and pk.pid==14:
                        return str(store.GetValue(pk).GetValue())
            except: pass
            return ""
        try:
            col=enum.EnumAudioEndpoints(2,1)
            for i in range(col.GetCount()):
                try:
                    ep=col.Item(i); eid=ep.GetId()
                    if eid in seen_ids: continue
                    seen_ids.add(eid); n=name(ep)
                    if not n: continue
                    name_counts[n]=name_counts.get(n,0)+1
                    result[eid]=f"[{name_counts[n]}] {n}" if name_counts[n]>1 else n
                except: pass
        except: pass
        return result


# ── bridge ─────────────────────────────────────────────────────────────────────

@dataclass
class Bridge:
    audio:         object
    root:          tk.Tk
    on_save:       callable
    on_remove:     callable
    on_move:       callable
    on_kb_add:     callable
    on_kb_remove:  callable
    on_layout:     callable
    is_capturing:  callable
    start_capture: callable
    end_capture:   callable


# ── channel (logic only — no tkinter) ─────────────────────────────────────────

class Channel:
    def __init__(self, state, index, bridge):
        self.state=state; self.index=index; self._b=bridge
        self._lock=threading.RLock(); self._active=set()
        self._talking=False; self._timer=None
        self.on_status_change=None

    def set_device(self, ep_id):
        self.state.ep_id=ep_id
        self._b.audio.send("attach", self.state.uid, (ep_id, self._on_attach))
        self._b.on_save()

    def set_delay(self, ms):
        self.state.delay_ms=ms

    def attach_saved(self):
        if self.state.ep_id:
            self._b.audio.send("attach", self.state.uid, (self.state.ep_id, self._on_attach))

    def _on_attach(self, ok):
        if ok is None:  self._notify("● INACTIVE", MUTED)
        elif ok:        self._notify("● MUTED",    ACCENT_OFF)
        else:           self._notify("● Mic error",ACCENT_OFF)

    def accept_capture(self, label):
        with self._lock:
            if label and label not in self.state.keybinds:
                self.state.keybinds.append(label)
                self._b.on_kb_add(label, self)
                self._b.on_save()
                return True
        return False

    def remove_bind(self, label):
        with self._lock: self.state.keybinds.remove(label)
        self._b.on_kb_remove(label, self); self._b.on_save()

    def handle_press(self, label):
        with self._lock:
            if label not in self.state.keybinds: return
            self._active.add(label)
            if not self._talking and self.state.ep_id: self._arm()

    def handle_release(self, label):
        with self._lock: self._active.discard(label)
        if not self._active.intersection(self.state.keybinds): self._disarm()

    def _arm(self):
        if self._timer: self._timer.cancel(); self._timer=None
        self._talking=True
        self._b.audio.send("mute", self.state.uid, False)
        self._b.root.after(0, self._notify, "● LIVE", ACCENT)

    def _disarm(self):
        with self._lock:
            if not self._talking: return
            if self._timer: self._timer.cancel(); self._timer=None
            delay=self.state.delay_ms
        if delay==0: self._silence(None)
        else:
            t=threading.Timer(delay/1000.0, lambda: self._silence(t)); t.daemon=True
            with self._lock: self._timer=t
            t.start()

    def _silence(self, timer):
        with self._lock:
            if timer is not None and timer is not self._timer: return
            self._timer=None
            if self._active.intersection(self.state.keybinds): return
            if not self._talking: return
            self._talking=False
        self._b.audio.send("mute", self.state.uid, True)
        self._b.root.after(0, self._notify, "● MUTED", ACCENT_OFF)

    def _notify(self, text, color):
        if self.on_status_change: self.on_status_change(text, color)

    def cancel(self):
        with self._lock:
            if self._timer: self._timer.cancel(); self._timer=None
            self._talking=False

    def destroy(self):
        self.cancel()
        self._b.audio.send("mute",   self.state.uid, False)
        self._b.audio.send("remove", self.state.uid, None)


# ── channel widget (UI only — no PTT logic) ───────────────────────────────────

class ChannelWidget:
    def __init__(self, ch, bridge):
        self._ch=ch; self._b=bridge
        self._ep_map={}
        self.frame=self._hdr=self._svar=self._slbl=self._combo=None
        self._btn_l=self._btn_r=None
        self._dvar=self._bframe=self._addbtn=self._clbl=None; self._brows=[]

    def build(self, parent):
        self.frame=tk.Frame(parent, bg=COL_BG, width=COL_W)
        self.frame.pack_propagate(False)
        self._body=tk.Frame(self.frame, bg=COL_BG)
        self._body.pack(fill="both", expand=True)

        # header
        hdr=tk.Frame(self._body, bg=PANEL_BG); hdr.pack(fill="x")
        self._hdr=tk.Label(hdr, text=self._title(), bg=PANEL_BG, fg=MUTED, font=("Segoe UI",11,"bold"), anchor="w")
        self._hdr.pack(side="left", padx=(10,4), pady=6)
        self._btn_l=tk.Button(hdr, text="◀", command=lambda:self._b.on_move(self._ch,-1),
                  bg=PANEL_BG, fg=MUTED, relief="flat", cursor="hand2",
                  activebackground=PANEL_BG, activeforeground=TEXT, font=("Segoe UI",20))
        self._btn_l.pack(side="left", pady=4)
        self._btn_r=tk.Button(hdr, text="▶", command=lambda:self._b.on_move(self._ch,+1),
                  bg=PANEL_BG, fg=MUTED, relief="flat", cursor="hand2",
                  activebackground=PANEL_BG, activeforeground=TEXT, font=("Segoe UI",20))
        self._btn_r.pack(side="left", pady=4)
        tk.Button(hdr, text="✕", command=lambda:self._b.on_remove(self._ch),
                  bg=PANEL_BG, fg=TEXT, relief="flat", cursor="hand2",
                  activebackground=PANEL_BG, activeforeground=ACCENT_OFF, font=("Segoe UI",16)
                  ).pack(side="right", padx=(0,4), pady=4)
        self._svar=tk.StringVar(value="● INACTIVE")
        self._slbl=tk.Label(hdr, textvariable=self._svar, bg=PANEL_BG, fg=MUTED, font=("Segoe UI",13,"bold"), anchor="e")
        self._slbl.pack(side="right", padx=(10,16), pady=6)
        tk.Frame(self._body, bg=DIVIDER, height=1).pack(fill="x")

        # body
        b=tk.Frame(self._body, bg=COL_BG); b.pack(fill="both", expand=True, padx=10, pady=8)
        self._lbl(b,"Input Device")
        self._combo=ttk.Combobox(b, state="readonly", font=("Segoe UI",10), height=20)
        self._combo["values"]=["— None —"]; self._combo.current(0)
        self._combo.pack(fill="x", pady=(2,8))
        self._combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        self._div(b)
        self._lbl(b,"Release Delay (ms)")
        self._dvar=tk.IntVar(value=self._ch.state.delay_ms)
        self._dvar.trace_add("write", lambda *_: self._ch.set_delay(self._dvar.get()))
        scale=tk.Scale(b, variable=self._dvar, from_=0, to=2000, orient="horizontal",
                 bg=COL_BG, fg=TEXT, troughcolor=PANEL_BG, highlightthickness=0,
                 bd=0, activebackground=ACCENT, font=("Segoe UI",9), sliderlength=40, width=20)
        scale.pack(fill="x", pady=(2,8))
        scale.bind("<ButtonRelease-1>", lambda _: self._b.on_save())
        scale.bind("<Button-2>", lambda e: "break")
        scale.bind("<Button-3>", lambda e: "break")
        self._div(b)
        krow=tk.Frame(b, bg=COL_BG); krow.pack(fill="x")
        tk.Label(krow, text="Keybinds", bg=COL_BG, fg=MUTED, font=("Segoe UI",11), anchor="w").pack(side="left")
        self._addbtn=tk.Button(krow, text="+ Add Key", command=self._on_add,
                               bg="#3a3a3a", fg=TEXT, relief="flat", cursor="hand2",
                               activebackground="#444", activeforeground=TEXT, font=("Segoe UI",10))
        self._addbtn.pack(side="left", padx=(8,0))
        self._clbl=tk.Label(b, text="", bg=COL_BG, fg=ACCENT, font=("Segoe UI",9))
        self._clbl.pack(fill="x")
        self._bframe=tk.Frame(b, bg=COL_BG); self._bframe.pack(fill="x", pady=(2,0))

        # wire observer
        self._ch.on_status_change=self.set_status
        self._rebuild_binds()
        return self.frame

    def update_arrows(self, is_first, is_last):
        if self._btn_l: self._btn_l.config(state="disabled" if is_first else "normal")
        if self._btn_r: self._btn_r.config(state="disabled" if is_last  else "normal")

    def _lbl(self, p, t): tk.Label(p, text=t, bg=COL_BG, fg=MUTED, font=("Segoe UI",11), anchor="w").pack(fill="x")
    def _div(self, p):    tk.Frame(p, bg=DIVIDER, height=1).pack(fill="x", pady=(0,8))
    def _title(self):     return f"Channel {self._ch.index+1}"

    def set_index(self, i):
        self._ch.index=i
        if self._hdr: self._hdr.config(text=self._title())

    def set_status(self, text, color):
        if self._svar: self._svar.set(text)
        if self._slbl: self._slbl.config(fg=color)

    def refresh_devices(self, ep_map):
        self._ep_map=ep_map
        ids=list(ep_map.keys()); names=["— None —"]+list(ep_map.values())
        self._combo["values"]=names
        if self._ch.state.ep_id in ids:
            self._combo.current(ids.index(self._ch.state.ep_id)+1)
        else:
            self._combo.current(0)
            if self._ch.state.ep_id:
                self._ch.set_device(""); self.set_status("⚠ Device not found", ACCENT_OFF)

    def _on_device_selected(self, _=None):
        sel=self._combo.current()
        self._ch.set_device("" if sel==0 else list(self._ep_map.keys())[sel-1])

    def _rebuild_binds(self):
        for r in self._brows: r.destroy()
        self._brows.clear()
        for label in self._ch.state.keybinds:
            r=tk.Frame(self._bframe, bg=PANEL_BG); r.pack(fill="x", pady=2)
            tk.Label(r, text=disp(label), bg=PANEL_BG, fg=TEXT, font=("Segoe UI",10), anchor="w").pack(side="left", padx=(6,0))
            tk.Button(r, text="✕", command=lambda l=label:self._rm_bind(l),
                      bg=PANEL_BG, fg=MUTED, relief="flat", cursor="hand2",
                      activebackground=PANEL_BG, activeforeground=ACCENT_OFF, font=("Segoe UI",10)
                      ).pack(side="right", padx=4)
            self._brows.append(r)
        if self._b: self._b.on_layout()

    def _rm_bind(self, label):
        self._ch.remove_bind(label); self._rebuild_binds()

    def _on_add(self):
        if not self._b.is_capturing(self._ch):
            self._b.start_capture(self._ch)
            self._addbtn.config(state="disabled"); self._clbl.config(text="Press a key…")

    def accept_capture(self, label):
        if self._ch.accept_capture(label): self._rebuild_binds()
        self._clbl.config(text=""); self._addbtn.config(state="normal")

    def destroy(self):
        self._ch.on_status_change=None
        if self.frame: self.frame.destroy(); self.frame=None


# ── app ────────────────────────────────────────────────────────────────────────

class App:
    def __init__(self, root):
        self.root=root
        root.title(f"GlobalPTT - Push to Talk - v{VERSION}")
        root.resizable(True,True)
        root.protocol("WM_DELETE_WINDOW", self._quit)
        root.bind("<Configure>", self._on_root_configure)
        self._was_zoomed=False

        self._prefs=load_prefs(); self._channels=[]; self._widgets=[]; self._dividers=[]
        self._kb_index={}; self._capturing=None

        self._audio=AudioThread(root); self._audio.start()

        self._bridge=Bridge(
            audio=self._audio, root=root,
            on_save=self._save, on_remove=self._remove, on_move=self._move,
            on_kb_add=self._kb_add, on_kb_remove=self._kb_rm,
            on_layout=self._fit,
            is_capturing=lambda ch:self._capturing is ch,
            start_capture=self._set_capture,
            end_capture=self._end_capture,
        )
        self._build_ui(); self._restore()
        self._start_input(); root.update_idletasks(); self._fit()
        self._audio.send("enumerate", None, self._apply_ep_map)
        try:
            base=getattr(sys,"_MEIPASS",os.path.dirname(os.path.abspath(__file__)))
            root.wm_iconbitmap(os.path.join(base,"GlobalPTTIcon.ico"))
        except: pass

    def _build_ui(self):
        style=ttk.Style(); style.theme_use("default")
        style.configure("TCombobox", fieldbackground=PANEL_BG, background=PANEL_BG,
                        foreground=TEXT, selectbackground=PANEL_BG, selectforeground=TEXT,
                        bordercolor="#444", arrowcolor=TEXT)
        style.map("TCombobox", fieldbackground=[("readonly",PANEL_BG)], foreground=[("readonly",TEXT)])
        outer=tk.Frame(self.root, bg=APP_BG); outer.pack(fill="both", expand=True)
        self._canvas=tk.Canvas(outer, bg=APP_BG, highlightthickness=0)
        bottom=tk.Frame(outer, bg=APP_BG); bottom.pack(side="bottom", fill="x")
        ttk.Sizegrip(bottom).pack(side="right")
        self._canvas.pack(side="top", fill="both", expand=True)
        self._inner=tk.Frame(self._canvas, bg=APP_BG)
        cwin=self._canvas.create_window((0,0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda _:self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda _:self._canvas.itemconfig(cwin, height=self._canvas.winfo_height()))
        bar=tk.Canvas(self._inner, bg="#3a3a3a", width=ADD_BAR_W, highlightthickness=0, cursor="hand2")
        bar.pack(side="left", fill="y")
        def draw_bar(_=None):
            bar.delete("all"); w,h=bar.winfo_width(),bar.winfo_height()
            bar.create_line(0,0,0,h,fill=DIVIDER,width=1)
            bar.create_text(w//2,h//2,text="+ Add Channel",fill="#aaaaaa",font=("Trebuchet MS",14,"bold"),angle=90,anchor="center")
        bar.bind("<Configure>",draw_bar)
        bar.bind("<Button-1>", lambda _:self._add())
        bar.bind("<Enter>",    lambda _:bar.config(bg="#484848"))
        bar.bind("<Leave>",    lambda _:bar.config(bg="#3a3a3a"))
        self._bar=bar

    def _apply_ep_map(self, ep_map):
        for w in self._widgets: w.refresh_devices(ep_map)

    def _enumerate_all(self):
        self._audio.send("enumerate", None, self._apply_ep_map)

    def _fit(self):
        self.root.update_idletasks()
        min_w = COL_W * len(self._channels) + ADD_BAR_W
        content_h = max((w._body.winfo_reqheight() for w in self._widgets if w._body), default=0)
        min_h = max(content_h + 40, MIN_H)
        self.root.minsize(min_w, min_h)
        self.root.geometry(f"{min_w}x{min_h}")
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _move(self, ch, direction):
        idx=self._channels.index(ch)
        new_idx=idx+direction
        if new_idx < 0 or new_idx >= len(self._channels): return
        # swap in lists
        self._channels[idx], self._channels[new_idx] = self._channels[new_idx], self._channels[idx]
        self._widgets[idx],  self._widgets[new_idx]  = self._widgets[new_idx],  self._widgets[idx]
        # update indices and headers
        for i,(c,w) in enumerate(zip(self._channels, self._widgets)):
            c.index=i; w.set_index(i)
        # re-pack all frames in new order
        for w in self._widgets: w.frame.pack_forget()
        for i,w in enumerate(self._widgets):
            if i>0: self._dividers[i-1].pack_forget()
            if i>0: self._dividers[i-1].pack(side="left", fill="y", before=self._bar)
            w.frame.pack(side="left", fill="y", before=self._bar)
        self._refresh_arrows()
        self._save()

    def _refresh_arrows(self):
        n=len(self._widgets)
        for i,w in enumerate(self._widgets):
            w.update_arrows(is_first=i==0, is_last=i==n-1)

    def _on_root_configure(self, _=None):
        zoomed=self.root.state()=="zoomed"
        if self._was_zoomed and not zoomed:
            self._fit()
        self._was_zoomed=zoomed

    def _restore(self):
        n=self._prefs.get("channel_count",2)
        for i in range(n):
            s=ChannelState.from_dict(self._prefs.get(f"ch{i}",{}))
            self._add(s, restored=True)

    def _add(self, state=None, restored=False):
        state=state or ChannelState()
        idx=len(self._channels)
        ch=Channel(state, idx, self._bridge)
        w=ChannelWidget(ch, self._bridge)
        self._channels.append(ch); self._widgets.append(w)
        for label in state.keybinds: self._kb_index.setdefault(label,[]).append(ch)
        if idx>0:
            div=tk.Frame(self._inner, bg=DIVIDER, width=1)
            div.pack(side="left", fill="y", before=self._bar)
            self._dividers.append(div)
        w.build(self._inner).pack(side="left", fill="y", before=self._bar)
        self._refresh_arrows()
        if restored: ch.attach_saved()
        else: self._save(); self._enumerate_all()
        self._fit()

    def _remove(self, ch):
        if self._capturing is ch: self._capturing=None
        idx=self._channels.index(ch)
        self._channels.pop(idx); w=self._widgets.pop(idx)
        if self._dividers: self._dividers.pop(max(idx-1,0)).destroy()
        for label in list(ch.state.keybinds): self._kb_rm(label,ch)
        ch.destroy(); w.destroy()
        for i,(c,ww) in enumerate(zip(self._channels,self._widgets)):
            c.index=i; ww.set_index(i)
        self._save(); self._enumerate_all(); self._refresh_arrows(); self._fit()

    def _kb_add(self, label, ch): self._kb_index.setdefault(label,[]).append(ch)
    def _kb_rm(self, label, ch):
        b=self._kb_index.get(label)
        if b:
            try: b.remove(ch)
            except ValueError: pass
            if not b: del self._kb_index[label]

    def _set_capture(self, ch): self._capturing=ch
    def _end_capture(self, ch, label):
        self._capturing=None
        w=self._widgets[self._channels.index(ch)]
        w.accept_capture(label)

    def _start_input(self):
        self._kb=keyboard.Listener(
            on_press=  lambda k:self._dispatch(key_label(k),True),
            on_release=lambda k:self._dispatch(key_label(k),False))
        self._kb.start()
        self._ms=mouse.Listener(
            on_click=lambda x,y,b,p:self._dispatch(MOUSE_LABELS.get(b,str(b)),p))
        self._ms.start()

    def _dispatch(self, label, pressed):
        if pressed and self._capturing:
            self._end_capture(self._capturing, label); return
        for ch in list(self._kb_index.get(label,[])):
            ch.handle_press(label) if pressed else ch.handle_release(label)

    def _save(self):
        data={"channel_count":len(self._channels)}
        data.update({f"ch{i}":ch.state.to_dict() for i,ch in enumerate(self._channels)})
        self._prefs=data; save_prefs(data)

    def _quit(self):
        self._save()
        for ch in self._channels: ch.cancel()
        try: self._kb.stop(); self._ms.stop()
        except: pass
        self._audio.send("quit",None,None); self.root.destroy()


def main():
    try:
        from ctypes import windll; windll.shcore.SetProcessDpiAwareness(1)
    except: pass
    root=tk.Tk(); root.minsize(COL_W+ADD_BAR_W, MIN_H); App(root); root.mainloop()

if __name__=="__main__": main()