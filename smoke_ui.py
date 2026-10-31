"""Headless end-to-end check of the timer: real Tk UI + fake OBS + real clock.

Run on a machine with a display (or under Xvfb):
    xvfb-run -a python smoke_ui.py

It drives the actual app object -- building the widgets, starting a session with
a 1s start offset and 3s stop offset, and asserting the fake OBS server saw
StartRecord and StopRecord at roughly the right moments.  That covers the timer
engine, the WebSocket path and the UI message pump in one go.
"""

from __future__ import annotations

import json
import sys
import time
import tkinter as tk

import Timersv2
from fake_obs_server import PASSWORD, FakeOBS

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + ((" -- " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def pump(root: tk.Tk, condition, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if condition():
            return True
        time.sleep(interval)
    root.update()
    return condition()


def log_text(app) -> str:
    return app.txt_log.get("1.0", "end")


def log_count(app, needle: str) -> int:
    return log_text(app).count(needle)


def main() -> int:
    server = FakeOBS(password=PASSWORD).start()
    root = tk.Tk()
    app = Timersv2.SessionTimerApp(root)

    # -- wiring ------------------------------------------------------------
    app.var_mode.set("websocket")
    app.var_host.set("127.0.0.1")
    app.var_port.set(str(server.port))
    app.var_password.set(PASSWORD)
    app.var_start.set("00:00:01")
    app.var_stop.set("00:00:03")
    check("UI built with all sections", hasattr(app, "btn_start") and hasattr(app, "cbo_monitor"))
    check("schedule length label computed", "00:00:02" in app.lbl_length.cget("text"),
          app.lbl_length.cget("text"))

    # -- test connection ---------------------------------------------------
    app.test_connection()
    ok = pump(root, lambda: "OBS 30.1.2" in app.lbl_conn.cget("text"), 5)
    check("WebSocket test connection reports real version", ok, app.lbl_conn.cget("text"))

    # -- full session ------------------------------------------------------
    t0 = time.monotonic()
    app.start_session()
    check("session started", app.session is not None)
    started = pump(root, lambda: server.recording, 6)
    start_at = time.monotonic() - t0
    check("recording started near the 1s mark", started and 0.8 <= start_at <= 2.5,
          "%.2fs after session start" % start_at)

    stopped = pump(root, lambda: not server.recording, 10)
    check("recording stopped", stopped)
    if server.record_started_at and server.record_stopped_at:
        duration = server.record_stopped_at - server.record_started_at
        check("recording lasted ~2s (the 1s->3s window)", 1.4 <= duration <= 3.2,
              "%.2fs" % duration)
    else:
        check("server timestamps captured", False)

    pump(root, lambda: "Recording stopped via WebSocket" in log_text(app), 4)
    check("session cleared after stop", app.session is None)
    check("start button re-enabled", str(app.btn_start.cget("state")) == "normal")
    text = log_text(app)
    check("log records the WebSocket start", "Recording started via WebSocket" in text)
    check("log records the stop", "Recording stopped via WebSocket" in text)
    check("no unexpected errors in log", "unexpected error" not in text)

    # -- cancel path -------------------------------------------------------
    server.requests.clear()
    app.var_start.set("00:00:00")
    app.var_stop.set("00:01:00")
    app.start_session()
    before = log_count(app, "Recording started via WebSocket")
    started = pump(root, lambda: log_count(app, "Recording started via WebSocket") > before, 6)
    check("second session starts recording immediately (0s offset)",
          started and server.recording)
    app.cancel_session()
    cancelled = pump(root, lambda: not server.recording, 6)
    check("cancel stops the recording this timer started", cancelled)
    check("cancel logged", "Session cancelled" in log_text(app))
    check("cancel did not double-start", server.requests.count("StartRecord") == 1,
          str(server.requests))

    # -- already-recording guard -------------------------------------------
    server.requests.clear()
    server.recording = True  # user started recording by hand
    app.var_start.set("00:00:00")
    app.var_stop.set("00:00:02")
    app.start_session()
    pump(root, lambda: "already recording" in log_text(app), 6)
    check("does not restart a manual recording",
          "already recording" in log_text(app) and server.requests.count("StartRecord") == 0,
          str(server.requests))
    app.cancel_session()
    time.sleep(0.4)
    pump(root, lambda: False, 0.5)
    check("cancelling leaves a manual recording alone", server.recording is True)
    server.recording = False

    # -- settings persistence ---------------------------------------------
    app._save_settings()
    data = json.loads(Timersv2.SETTINGS_PATH.read_text(encoding="utf-8"))
    check("settings written to disk", data["ws_port"] == server.port and data["mode"] == "websocket",
          json.dumps({k: data[k] for k in ("mode", "ws_port", "start_at", "stop_at")}))

    app._on_close()
    server.stop()
    print()
    if failures:
        print("FAILED: %d check(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
