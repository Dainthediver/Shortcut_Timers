#!/usr/bin/env python3
"""OBS Session Timer v2 -- dual-monitor safe.

What changed from v1 (Timersv1.py)
---------------------------------
v1 focused the OBS window with pygetwindow and pressed your hotkeys.  On a
two-monitor desk that breaks in ways that look like "the timer doesn't work":

* ``SetForegroundWindow`` is refused when the timer is not already foreground,
  so the keystroke went to whatever window *was* focused (your video player).
* ``win.restore()`` / ``activate()`` can re-place or un-maximise OBS, so it
  wandered between monitors.
* pygetwindow reports virtualised coordinates, so a 150%-scaled second monitor
  makes monitor maths wrong.

v2 removes the whole problem by defaulting to OBS's own WebSocket API
(Tools -> WebSocket Server Settings in OBS 28+).  No focus change, no
keystrokes, nothing moves, both monitors keep doing what they were doing.
Hotkeys are still available as a fallback -- and *that* path is now hardened:
per-monitor DPI awareness, AttachThreadInput + ALT-tap foregrounding, and the
window's original rect/monitor restored afterwards.

Run:      python Timersv2.py     (or pythonw Timersv2.py for no console)
Requires: Python 3.8+, tkinter.  No third-party packages.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import win_layout as wl
from obs_ws import OBSAuthError, OBSRequestError, ObsClient, WSError

APP_VERSION = "2.0.0"
APP_TITLE = "OBS Session Timer v%s" % APP_VERSION
SETTINGS_PATH = Path(__file__).with_name("timer_settings.json")

DEFAULTS = {
    "window_prefix": "OBS",
    "mode": "auto",              # auto | websocket | hotkey
    "ws_host": "127.0.0.1",
    "ws_port": 4455,
    "ws_password": "",
    "start_at": "00:00:05",
    "stop_at": "00:15:00",
    "start_shortcut": "ctrl+alt+b",
    "stop_shortcut": "ctrl+alt+n",
    "keep_obs_placement": True,
    "always_on_top": False,
    "ui_monitor": 1,             # 1-based monitor for this window
}

# Dark palette (teal / amber)
BG = "#031A1A"
BG_FIELD = "#07292A"
FG = "#F8F1E3"
FG_MUTED = "#9CB2AE"
ACCENT = "#FFAC02"
GOOD = "#5BD68A"
BAD = "#FF6B6B"
WARN = "#FFB347"


def hms_to_seconds(text: str) -> int:
    """Parse a time offset.

    Accepted forms:        '1:02:03' -> H:M:S
                           '02:03'   -> M:S
                           '900'     -> 900 seconds (bare number = seconds)
    Raises ValueError on anything else.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty time")
    parts = raw.split(":")
    if len(parts) > 3:
        raise ValueError("use H:M:S")
    try:
        values = [int(p or 0) for p in parts]
    except ValueError:
        raise ValueError("time must be numeric (H:M:S)") from None
    if min(values) < 0:
        raise ValueError("time cannot be negative")
    if len(values) == 1:
        if values[0] > 86399:
            raise ValueError("bare number is seconds - use H:M:S for longer times")
        return values[0]
    if len(values) == 2:
        m, s = values
        if s > 59:
            raise ValueError("seconds must be 0-59")
        return m * 60 + s
    h, m, s = values
    if m > 59 or s > 59:
        raise ValueError("minutes and seconds must be 0-59")
    return h * 3600 + m * 60 + s


def seconds_to_hms(total: float) -> str:
    total = max(0, int(total))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%02d" % (h, m, s)


class SessionTimerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = self._load_settings()
        self.q: queue.Queue = queue.Queue()
        self.client: ObsClient | None = None
        self.session: dict | None = None
        self.started_by_us = False
        # Set the moment a start is *requested* (not when the reply lands), so a
        # cancel in the same instant still knows it may have to stop recording.
        self.start_requested = False
        self._action_lock = threading.Lock()

        self._build_ui()
        self._apply_settings()
        self._startup_report()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._pump)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = self.root
        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.minsize(640, 700)

        def label(parent, text, fg=FG, font=None, **kw):
            return tk.Label(parent, text=text, bg=parent["bg"], fg=fg,
                            font=font or ("Segoe UI", 9), **kw)

        outer = tk.Frame(root, bg=BG, padx=14, pady=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        # ---- OBS target --------------------------------------------------
        target = tk.LabelFrame(outer, text=" OBS target ", bg=BG, fg=ACCENT,
                               font=("Segoe UI", 9, "bold"), bd=1, relief="solid",
                               padx=10, pady=8)
        target.grid(row=0, column=0, sticky="ew")
        target.columnconfigure(2, weight=1)

        label(target, "Window title starts with").grid(row=0, column=0, sticky="w")
        self.var_prefix = tk.StringVar()
        self.ent_prefix = self._entry(target, self.var_prefix, width=18)
        self.ent_prefix.grid(row=0, column=1, padx=6, sticky="w")
        self.btn_find = self._button(target, "Find OBS", self.find_obs_window)
        self.btn_find.grid(row=0, column=2, padx=4, sticky="w")

        self.lbl_window = label(target, "(not checked yet)", fg=FG_MUTED, anchor="w")
        self.lbl_window.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        opts = tk.Frame(target, bg=BG)
        opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.var_keep = tk.BooleanVar()
        self._check(opts, "Keep OBS on its monitor", self.var_keep).pack(side="left")
        self.var_ontop = tk.BooleanVar()
        self._check(opts, "Timer always on top", self.var_ontop,
                    command=self._apply_ontop).pack(side="left", padx=(16, 0))

        place = tk.Frame(target, bg=BG)
        place.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        label(place, "Put this timer on").pack(side="left")
        self.var_monitor = tk.StringVar()
        self.cbo_monitor = ttk.Combobox(place, textvariable=self.var_monitor,
                                        state="readonly", width=26)
        self.cbo_monitor.pack(side="left", padx=6)
        self._button(place, "Move window", self.move_to_monitor).pack(side="left")
        self._button(place, "Refresh", self.refresh_monitors).pack(side="left", padx=6)

        # ---- control mode ------------------------------------------------
        ctrl = tk.LabelFrame(outer, text=" How to control OBS ", bg=BG, fg=ACCENT,
                             font=("Segoe UI", 9, "bold"), bd=1, relief="solid",
                             padx=10, pady=8)
        ctrl.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ctrl.columnconfigure(3, weight=1)

        self.var_mode = tk.StringVar()
        modes = tk.Frame(ctrl, bg=BG)
        modes.grid(row=0, column=0, columnspan=4, sticky="ew")
        for value, text in (("auto", "Auto (WebSocket, fall back to hotkeys)"),
                            ("websocket", "WebSocket only"),
                            ("hotkey", "Hotkeys only")):
            tk.Radiobutton(modes, text=text, value=value, variable=self.var_mode,
                           bg=BG, fg=FG, selectcolor=BG_FIELD, activebackground=BG,
                           activeforeground=ACCENT, highlightthickness=0,
                           font=("Segoe UI", 9)).pack(anchor="w")

        label(ctrl, "Host").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.var_host = tk.StringVar()
        self._entry(ctrl, self.var_host, width=14).grid(row=1, column=1, padx=6, pady=(8, 0), sticky="w")
        label(ctrl, "Port").grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.var_port = tk.StringVar()
        self._entry(ctrl, self.var_port, width=7).grid(row=1, column=3, padx=6, pady=(8, 0), sticky="w")

        label(ctrl, "Password").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.var_password = tk.StringVar()
        self._entry(ctrl, self.var_password, width=24, show="*").grid(
            row=2, column=1, columnspan=2, padx=6, pady=(6, 0), sticky="w")

        self.btn_test = self._button(ctrl, "Test connection", self.test_connection)
        self.btn_test.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.lbl_conn = label(ctrl, "not tested", fg=FG_MUTED, anchor="w")
        self.lbl_conn.grid(row=3, column=1, columnspan=3, sticky="ew", padx=6, pady=(8, 0))

        # ---- schedule ----------------------------------------------------
        sched = tk.LabelFrame(outer, text=" Schedule ", bg=BG, fg=ACCENT,
                              font=("Segoe UI", 9, "bold"), bd=1, relief="solid",
                              padx=10, pady=8)
        sched.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        for col in range(6):
            sched.columnconfigure(col, weight=1)

        label(sched, "Recording starts at").grid(row=0, column=0, sticky="w")
        self.var_start = tk.StringVar()
        self._entry(sched, self.var_start, width=10).grid(row=0, column=1, padx=6, sticky="w")
        label(sched, "(H:MM:SS after Start)", fg=FG_MUTED).grid(row=0, column=2, columnspan=2, sticky="w")

        label(sched, "Recording stops at").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.var_stop = tk.StringVar()
        self._entry(sched, self.var_stop, width=10).grid(row=1, column=1, padx=6, pady=(6, 0), sticky="w")
        self.lbl_length = label(sched, "", fg=FG_MUTED)
        self.lbl_length.grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))
        self.var_start.trace_add("write", lambda *_: self._update_length())
        self.var_stop.trace_add("write", lambda *_: self._update_length())

        label(sched, "Start hotkey").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.var_start_key = tk.StringVar()
        self._entry(sched, self.var_start_key, width=16).grid(row=2, column=1, padx=6, pady=(10, 0), sticky="w")
        label(sched, "Stop hotkey").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.var_stop_key = tk.StringVar()
        self._entry(sched, self.var_stop_key, width=16).grid(row=3, column=1, padx=6, pady=(6, 0), sticky="w")
        label(sched, "used only in hotkey mode", fg=FG_MUTED).grid(
            row=3, column=2, columnspan=2, sticky="w", pady=(6, 0))

        # ---- actions -----------------------------------------------------
        actions = tk.Frame(outer, bg=BG)
        actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.btn_start = self._button(actions, "Start Session", self.start_session,
                                      bg=ACCENT, fg="#031A1A", bold=True)
        self.btn_start.pack(side="left")
        self.btn_stop = self._button(actions, "Stop recording now",
                                     lambda: self.manual_action("stop"))
        self.btn_stop.pack(side="left", padx=6)
        self.btn_cancel = self._button(actions, "Cancel session", self.cancel_session)
        self.btn_cancel.pack(side="left")
        self.btn_test_start = self._button(actions, "Test start", lambda: self.manual_action("start"))
        self.btn_test_start.pack(side="right")
        self.btn_test_stop = self._button(actions, "Test stop", lambda: self.manual_action("stop"))
        self.btn_test_stop.pack(side="right", padx=6)

        # ---- clock -------------------------------------------------------
        clock = tk.Frame(outer, bg=BG)
        clock.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.lbl_clock = tk.Label(clock, text="--:--:--", bg=BG, fg=ACCENT,
                                  font=("Consolas", 30, "bold"))
        self.lbl_clock.pack()
        self.lbl_phase = tk.Label(clock, text="Ready", bg=BG, fg=FG_MUTED,
                                  font=("Segoe UI", 10))
        self.lbl_phase.pack()

        # ---- log ---------------------------------------------------------
        logframe = tk.Frame(outer, bg=BG)
        logframe.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        outer.rowconfigure(5, weight=1)
        logframe.rowconfigure(0, weight=1)
        logframe.columnconfigure(0, weight=1)
        self.txt_log = tk.Text(logframe, height=9, bg=BG_FIELD, fg=FG_MUTED,
                               insertbackground=FG, relief="flat", wrap="word",
                               font=("Consolas", 9))
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        scroll = tk.Scrollbar(logframe, command=self.txt_log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=scroll.set, state="disabled")
        for tag, color in (("info", FG_MUTED), ("good", GOOD), ("bad", BAD), ("warn", WARN)):
            self.txt_log.tag_configure(tag, foreground=color)

    # -- widget factories --------------------------------------------------
    def _entry(self, parent, var, width=12, show=None):
        return tk.Entry(parent, textvariable=var, width=width, show=show or "",
                        bg=BG_FIELD, fg=FG, insertbackground=FG, relief="flat",
                        highlightthickness=1, highlightbackground="#0C3B3B",
                        highlightcolor=ACCENT, font=("Segoe UI", 9))

    def _check(self, parent, text, var, command=None):
        kwargs = {"command": command} if command else {}
        return tk.Checkbutton(parent, text=text, variable=var, **kwargs,
                              bg=BG, fg=FG, selectcolor=BG_FIELD, activebackground=BG,
                              activeforeground=ACCENT, highlightthickness=0,
                              font=("Segoe UI", 9))

    def _button(self, parent, text, command, bg=BG_FIELD, fg=FG, bold=False):
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=ACCENT, activeforeground="#031A1A",
                         relief="flat", bd=0, padx=12, pady=5, cursor="hand2",
                         font=("Segoe UI", 9, "bold" if bold else "normal"))

    # ----------------------------------------------------------- settings
    def _load_settings(self) -> dict:
        data = dict(DEFAULTS)
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
        return data

    def _apply_settings(self) -> None:
        s = self.settings
        self.var_prefix.set(s["window_prefix"])
        self.var_mode.set(s["mode"])
        self.var_host.set(s["ws_host"])
        self.var_port.set(str(s["ws_port"]))
        self.var_password.set(s["ws_password"])
        self.var_start.set(s["start_at"])
        self.var_stop.set(s["stop_at"])
        self.var_start_key.set(s["start_shortcut"])
        self.var_stop_key.set(s["stop_shortcut"])
        self.var_keep.set(bool(s["keep_obs_placement"]))
        self.var_ontop.set(bool(s["always_on_top"]))
        self.refresh_monitors(select=int(s.get("ui_monitor", 1)))
        self._apply_ontop()
        self._update_length()

    def _collect_settings(self) -> dict:
        return {
            "window_prefix": self.var_prefix.get().strip() or "OBS",
            "mode": self.var_mode.get(),
            "ws_host": self.var_host.get().strip() or "127.0.0.1",
            "ws_port": _safe_int(self.var_port.get(), 4455),
            "ws_password": self.var_password.get(),
            "start_at": self.var_start.get().strip(),
            "stop_at": self.var_stop.get().strip(),
            "start_shortcut": self.var_start_key.get().strip(),
            "stop_shortcut": self.var_stop_key.get().strip(),
            "keep_obs_placement": bool(self.var_keep.get()),
            "always_on_top": bool(self.var_ontop.get()),
            "ui_monitor": _monitor_number(self.var_monitor.get()),
        }

    def _save_settings(self) -> None:
        self.settings = self._collect_settings()
        try:
            SETTINGS_PATH.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except Exception as exc:
            self._log("Could not save settings: %s" % exc, "warn")

    # -------------------------------------------------------------- logging
    def _log(self, message: str, tag: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        line = "[%s] %s\n" % (stamp, message)
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line, tag)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")
        print(line, end="")

    def _log_from_thread(self, message: str, tag: str = "info") -> None:
        self.q.put(("log", (message, tag), None))

    # ------------------------------------------------------- window helpers
    def _monitors(self) -> list:
        try:
            return wl.list_monitors()
        except Exception:
            return []

    def refresh_monitors(self, select: int | None = None) -> None:
        monitors = self._monitors()
        values = [m.label for m in monitors] or ["(no monitors detected)"]
        self.cbo_monitor.configure(values=values)
        index = (select or 1) - 1
        index = max(0, min(index, len(values) - 1))
        self.var_monitor.set(values[index])

    def move_to_monitor(self) -> None:
        monitors = self._monitors()
        if not monitors:
            messagebox.showerror(APP_TITLE, "No monitors detected.")
            return
        index = max(0, min(_monitor_number(self.var_monitor.get()) - 1, len(monitors) - 1))
        mon = monitors[index]
        width = self.root.winfo_width() or 660
        height = self.root.winfo_height() or 760
        x = mon.work.left + max(20, (mon.work.width - width) // 2)
        y = mon.work.top + max(20, (mon.work.height - height) // 2)
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self._log("Timer window moved to %s (work area %s)" % (mon.label, mon.work))

    def _apply_ontop(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.var_ontop.get()))
        except Exception:
            pass

    def find_obs_window(self):
        prefix = self.var_prefix.get().strip() or "OBS"
        if not wl.IS_WINDOWS:
            self._log("Window discovery needs Windows (this is %s)" % sys.platform, "warn")
            return None
        try:
            windows = wl.list_windows()
            window = wl.find_window(prefix, windows)
        except Exception as exc:
            self._log("Window scan failed: %s" % exc, "bad")
            return None
        if not window:
            self.lbl_window.configure(text="No visible window title starts with %r" % prefix,
                                      fg=BAD)
            self._log("No window starts with %r (saw %d windows)" % (prefix, len(windows)), "bad")
            return None
        monitor = wl.monitor_for_rect(window.rect, self._monitors())
        where = monitor.label if monitor else "off-screen / unknown"
        self.lbl_window.configure(
            text='%r  ->  %s   [%d x %d at %d,%d]'
                 % (window.title, where, window.rect.width, window.rect.height,
                    window.rect.left, window.rect.top),
            fg=GOOD)
        self._log("Found %r on %s" % (window.title, where), "good")
        return window

    # --------------------------------------------------------- OBS control
    def _snapshot(self) -> dict:
        """Read every Tk variable once, on the UI thread.

        tkinter raises "main thread is not in main loop" if a worker thread
        touches a StringVar, so workers never see Tk objects -- only this dict.
        """
        return {
            "mode": self.var_mode.get(),
            "host": self.var_host.get().strip() or "127.0.0.1",
            "port": _safe_int(self.var_port.get(), 4455),
            "password": self.var_password.get(),
            "prefix": self.var_prefix.get().strip() or "OBS",
            "keep_placement": bool(self.var_keep.get()),
            "start_key": self.var_start_key.get().strip(),
            "stop_key": self.var_stop_key.get().strip(),
        }

    def _ws_connect(self, cfg: dict) -> ObsClient:
        client = ObsClient(cfg["host"], cfg["port"], cfg["password"], timeout=4.0)
        client.connect()
        self.client = client
        return client

    def _ws_call(self, cfg: dict, action: str) -> tuple[bool, str]:
        """'start' | 'stop' | 'status' via WebSocket. Raises on failure."""
        client = self.client
        if not client or not client.identified:
            client = self._ws_connect(cfg)
        if action == "status":
            return True, client.probe()
        if action == "start":
            if client.is_recording():
                return False, "OBS was already recording - left untouched"
            client.start_record()
            return True, "Recording started via WebSocket (%s)" % client.probe()
        if action == "stop":
            if not client.is_recording():
                return False, "OBS is not recording - nothing to stop"
            client.stop_record()
            return True, "Recording stopped via WebSocket"
        raise ValueError(action)

    def _hotkey_call(self, cfg: dict, action: str) -> tuple[bool, str]:
        shortcut = cfg["start_key"] if action == "start" else cfg["stop_key"]
        keys = wl.parse_hotkey(shortcut)  # validate before touching focus
        window = wl.find_window(cfg["prefix"])
        if not window:
            raise RuntimeError("no visible window starting with %r" % cfg["prefix"])
        monitors = self._monitors()
        before_mon = wl.monitor_for_rect(window.rect, monitors)
        ok, notes = wl.force_foreground(window.hwnd, keep_placement=cfg["keep_placement"])
        if not ok:
            raise RuntimeError("Windows refused foreground for %r" % window.title)
        time.sleep(0.35)  # let OBS take keyboard focus
        wl.send_hotkey(keys)
        after_mon = wl.monitor_for_rect(wl.window_rect(window.hwnd), monitors)
        detail = "%s sent to %r on %s" % (
            shortcut, window.title,
            before_mon.label if before_mon else "?")
        if after_mon and before_mon and after_mon.index != before_mon.index:
            detail += " (WARNING: window ended up on %s)" % after_mon.label
        if notes:
            detail += " [%s]" % notes
        return True, detail

    def _do_action(self, cfg: dict, action: str) -> tuple[bool, str]:
        mode = cfg["mode"]
        errors = []
        if mode in ("auto", "websocket"):
            try:
                return self._ws_call(cfg, action)
            except (OBSAuthError, OBSRequestError, WSError, OSError) as exc:
                errors.append("WebSocket: %s" % exc)
                self.client = None
                if mode == "websocket":
                    return False, "; ".join(errors)
        if wl.IS_WINDOWS:
            try:
                return self._hotkey_call(cfg, action)
            except Exception as exc:
                errors.append("Hotkey: %s" % exc)
        else:
            errors.append("Hotkey mode needs Windows")
        return False, "; ".join(errors)

    def _run_async(self, label: str, action: str) -> None:
        """Run an OBS action off the UI thread.

        Actions are serialised by a lock rather than dropped, so a stop that
        lands while a start is still in flight (short test sessions) still runs.
        """
        cfg = self._snapshot()  # must happen here, on the UI thread
        self._log_from_thread("%s ..." % label.capitalize(), "info")

        def work():
            try:
                with self._action_lock:
                    ok, message = self._do_action(cfg, action)
            except Exception as exc:  # never lose a worker exception
                ok, message = False, "unexpected error: %s" % exc
            self.q.put(("action_done", (action, ok, message), None))

        threading.Thread(target=work, daemon=True).start()

    def test_connection(self) -> None:
        cfg = self._snapshot()
        if cfg["mode"] == "hotkey":
            window = self.find_obs_window()
            self.lbl_conn.configure(
                text="hotkey mode: window %s" % ("found" if window else "missing"),
                fg=GOOD if window else BAD)
            return
        self.lbl_conn.configure(text="connecting...", fg=FG_MUTED)

        def work():
            client = None
            try:
                client = self._ws_connect(cfg)
                message = client.probe()
                self.q.put(("conn_ok", message, None))
            except Exception as exc:
                if client:
                    client.close()
                self.client = None
                self.q.put(("conn_fail", str(exc), None))

        threading.Thread(target=work, daemon=True).start()

    def manual_action(self, action: str) -> None:
        self.start_requested = action == "start"
        self._run_async("manual %s" % action, action)

    # -------------------------------------------------------------- session
    def start_session(self) -> None:
        if self.session:
            self._log("Session already running", "warn")
            return
        try:
            start_at = hms_to_seconds(self.var_start.get())
            stop_at = hms_to_seconds(self.var_stop.get())
            if stop_at <= start_at:
                raise ValueError("'stops at' must be later than 'starts at'")
            if self.var_mode.get() in ("auto", "hotkey") and wl.IS_WINDOWS:
                wl.parse_hotkey(self.var_start_key.get())
                wl.parse_hotkey(self.var_stop_key.get())
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, "Invalid schedule: %s" % exc)
            self._log("Invalid schedule: %s" % exc, "bad")
            return

        self._save_settings()
        self.session = {
            "t0": time.monotonic(),
            "start_at": start_at,
            "stop_at": stop_at,
            "started": False,
            "stopped": False,
        }
        self.started_by_us = False
        self.btn_start.configure(state="disabled", text="Session running")
        self.lbl_phase.configure(text="Waiting to start recording...", fg=WARN)
        self._log("Session started. Recording %s -> %s (%s long)"
                  % (seconds_to_hms(start_at), seconds_to_hms(stop_at),
                     seconds_to_hms(stop_at - start_at)), "good")

    def cancel_session(self) -> None:
        if not self.session:
            self._log("No session running", "info")
            return
        cancelled = self.session
        self.session = None
        self._reset_session_ui("Cancelled", BAD)
        self._log("Session cancelled at %s" % seconds_to_hms(
            time.monotonic() - cancelled["t0"]), "warn")
        if self.start_requested:
            self._log("Stopping the recording this timer started", "warn")
            self._run_async("cancel stop", "stop")
        self.start_requested = False
        self.started_by_us = False

    def _finish_session(self) -> None:
        self.session = None
        self._reset_session_ui("Finished - recording saved by OBS", GOOD)

    def _reset_session_ui(self, phase: str, color: str) -> None:
        self.btn_start.configure(state="normal", text="Start Session")
        self.lbl_phase.configure(text=phase, fg=color)
        self.lbl_clock.configure(text="--:--:--", fg=ACCENT)

    def _update_length(self) -> None:
        try:
            length = hms_to_seconds(self.var_stop.get()) - hms_to_seconds(self.var_start.get())
        except ValueError:
            self.lbl_length.configure(text="invalid time", fg=BAD)
            return
        if length <= 0:
            self.lbl_length.configure(text="stop must be later than start", fg=BAD)
        else:
            self.lbl_length.configure(text="= %s of recording" % seconds_to_hms(length), fg=FG_MUTED)

    # ------------------------------------------------------------- main loop
    def _pump(self) -> None:
        try:
            while True:
                kind, payload, _ = self.q.get_nowait()
                self._handle_message(kind, payload)
        except queue.Empty:
            pass
        self._tick_session()
        self.root.after(100, self._pump)

    def _handle_message(self, kind: str, payload) -> None:
        if kind == "log":
            message, tag = payload
            self._log(message, tag)
        elif kind == "conn_ok":
            self.lbl_conn.configure(text=payload, fg=GOOD)
            self._log(payload, "good")
        elif kind == "conn_fail":
            self.lbl_conn.configure(text="failed: %s" % payload, fg=BAD)
            self._log("WebSocket test failed: %s" % payload, "bad")
        elif kind == "action_done":
            action, ok, message = payload
            self._log(message, "good" if ok else "bad")
            if action == "start":
                if ok:
                    self.started_by_us = True
                    self.lbl_phase.configure(text="RECORDING", fg=GOOD)
                else:
                    # Start refused (usually "already recording"): never let a
                    # later stop/cancel touch a recording this timer didn't make.
                    self.start_requested = False
                    if self.session:
                        self.lbl_phase.configure(text="start failed - see log", fg=BAD)
            elif action == "stop" and ok:
                self.started_by_us = False
                self.lbl_phase.configure(text="Recording stopped", fg=FG_MUTED)

    def _tick_session(self) -> None:
        session = self.session
        if not session:
            return
        elapsed = time.monotonic() - session["t0"]

        if not session["started"] and elapsed >= session["start_at"]:
            session["started"] = True
            self.start_requested = True
            self._run_async("start recording", "start")
        if not session["stopped"] and elapsed >= session["stop_at"]:
            session["stopped"] = True
            self.start_requested = False
            self._run_async("stop recording", "stop")
            self._finish_session()
            return

        if session["started"]:
            remaining = session["stop_at"] - elapsed
            self.lbl_clock.configure(text=seconds_to_hms(remaining), fg=GOOD)
            self.lbl_phase.configure(text="RECORDING - %s left" % seconds_to_hms(remaining), fg=GOOD)
        else:
            until = session["start_at"] - elapsed
            self.lbl_clock.configure(text=seconds_to_hms(until), fg=WARN)
            self.lbl_phase.configure(text="Recording begins in %s" % seconds_to_hms(until), fg=WARN)

    # -------------------------------------------------------------- startup
    def _startup_report(self) -> None:
        dpi = "n/a"
        if wl.IS_WINDOWS:
            try:
                dpi = wl.enable_dpi_awareness()
            except Exception as exc:
                dpi = "failed: %s" % exc
        self._log("%s on %s - DPI awareness: %s" % (APP_TITLE, sys.platform, dpi))
        monitors = self._monitors()
        if monitors:
            for mon in monitors:
                self._log("  %s work=%s device=%s" % (mon.label, mon.work, mon.device))
        else:
            self._log("  monitor enumeration unavailable", "warn")
        if wl.IS_WINDOWS:
            self.find_obs_window()
            if self.var_mode.get() != "hotkey":
                self.test_connection()

    def _on_close(self) -> None:
        self._save_settings()
        if self.client:
            self.client.close()
        self.root.destroy()


def _safe_int(text: str, fallback: int) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return fallback


def _monitor_number(label: str) -> int:
    try:
        return int(str(label).split("Monitor")[1].split("(")[0].strip())
    except Exception:
        return 1


def main() -> None:
    if wl.IS_WINDOWS:
        try:
            wl.enable_dpi_awareness()  # must happen before Tk creates windows
        except Exception:
            pass
    root = tk.Tk()
    SessionTimerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
