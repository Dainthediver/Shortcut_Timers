# Shortcut_Timers — OBS Session Timer

Start OBS recording at a set offset, stop it at another offset, without touching
the keyboard. Built for a **two-monitor desk**: video on monitor 2, OBS on
monitor 1, timer wherever you want it.

| File | What it is |
| --- | --- |
| `Timersv2.py` | **The app.** Dual-monitor safe session timer (tkinter GUI). |
| `obs_ws.py` | Dependency-free OBS WebSocket v5 client (used by the app). |
| `win_layout.py` | Windows window/monitor layer: DPI awareness, monitor maths, safe foregrounding. |
| `fake_obs_server.py` | Fake obs-websocket server used by the tests. |
| `test_obs_ws.py` | Protocol tests over a real TCP WebSocket (auth, start/stop, errors, big frames). |
| `test_timers.py` | Time parsing, hotkey parsing, monitor-selection maths. |
| `test_win32_stub.py` | Runs the Windows code path on any OS with a stubbed user32. |
| `smoke_ui.py` | Headless end-to-end: real Tk UI + fake OBS + real clock, asserts a 1s→3s session records for ~2s. |
| `Timersv1.py` | Original pygetwindow version, kept for reference. |
| `pytoexe_guide.md` | How to build the `.exe` in VS Code (PyInstaller walkthrough). |

## Why v2 exists

v1 focused the OBS window with `pygetwindow` and pressed your hotkeys. On two
monitors that fails in ways that look like the timer is broken:

* `SetForegroundWindow` is **refused** unless the calling process is already
  foreground, so the keystroke went to whatever *was* focused — usually the
  video player on the other monitor, never OBS.
* `restore()` / `activate()` can re-place or un-maximise a window, so OBS
  wandered between monitors mid-session.
* pygetwindow reports DPI-virtualised coordinates, so any monitor with a
  different scale factor (125%/150%) breaks the monitor maths.

v2 stops needing focus at all: it drives OBS through **obs-websocket**, which is
built into OBS 28+. No keystrokes, no focus change, no window movement, nothing
on either monitor is disturbed. Hotkeys remain available as a fallback, and that
path is hardened (per-monitor DPI awareness, `AttachThreadInput` + ALT-tap
foregrounding, and the window's original rect restored afterwards).

## Setup

1. **OBS 28 or newer.** Tools → *WebSocket Server Settings* →
   *Enable WebSocket Server*. Note the port (default `4455`) and password.
2. **Python 3.8+** with tkinter (bundled with the python.org Windows installer).
3. Run it:

   ```
   python Timersv2.py        :: with a console, useful while setting up
   pythonw Timersv2.py       :: no console window
   ```

4. In the app: paste the port + password → **Test connection**. It should say
   something like `OBS 30.1.2 (obs-websocket 5.5.2) at 127.0.0.1:4455`.
5. Set **Recording starts at** / **Recording stops at** (offsets from pressing
   *Start Session*, `H:MM:SS`), pick the monitor this timer window should live
   on, then *Start Session*.

Settings are saved to `timer_settings.json` next to the script — or next to the
`.exe` in a packaged build (git-ignored — it holds the WebSocket password in plain
text).

### Control modes

| Mode | Behaviour |
| --- | --- |
| **Auto** (default) | WebSocket if it answers, otherwise fall back to hotkeys. |
| **WebSocket only** | Never touches focus. Fails loudly if OBS isn't reachable. |
| **Hotkeys only** | Foregrounds OBS, sends your start/stop shortcut. Needs OBS hotkeys bound in *Settings → Hotkeys*. |

## Dual-monitor notes

* **Monitor detection** — `Find OBS` reports the OBS window's title, which
  monitor it is on, and its real pixel rect. *Refresh* re-enumerates monitors;
  *Move window* puts the timer on the monitor you pick (the list is numbered
  primary-first).
* **Keep OBS on its monitor** (on by default) — in hotkey mode the window's rect
  is captured before activation and restored after, so OBS cannot be dragged to
  the other screen by the activation call.
* **DPI** — per-monitor-v2 awareness is set before any window exists, so a
  150%-scaled second monitor reports real pixels instead of scaled ones. The
  startup log line tells you which awareness mode was granted.
* **Log pane** — every action logs a timestamped line: mode used, window found,
  monitor it was on, whether placement had to be restored. That is the first
  place to look if something misbehaves.
* Ticking **Timer always on top** keeps the timer visible over a fullscreen
  video on its own monitor.

## Behaviour details

* Start refused when OBS is already recording — the timer never double-starts a
  recording, and it will not stop a recording it did not start.
* *Cancel session* stops the recording the timer started (and only that one).
* Short sessions still work: actions are serialised, never dropped, so a stop
  that lands while a start is still in flight still runs.
* `H:MM:SS`, `MM:SS` and bare seconds are all accepted for the offsets;
  bare numbers are seconds (`900` = 15 minutes).
* Start/stop hotkeys are validated before anything is dispatched (a shortcut
  must end in a non-modifier key).

## Building a standalone .exe

See **[`pytoexe_guide.md`](pytoexe_guide.md)** — full VS Code + PyInstaller
walkthrough (venv, console build, windowed release build, spec file, troubleshooting).
In short, from the repo root in a venv with `pyinstaller` installed:

```
pyinstaller --noconfirm --clean "OBS Session Timer.spec"
```

The result is `dist\OBS Session Timer.exe` — one file, no Python needed at runtime.
A copy of `OBS Session Timer.spec` is committed, so the recipe is the same every time.

## Tests

```
python -m pytest test_obs_ws.py test_timers.py test_win32_stub.py -q   # 47 tests
xvfb-run -a python smoke_ui.py                                        # Linux/macOS headless
python smoke_ui.py                                                    # Windows: opens a real window
```

The protocol tests run against a fake OBS server that performs the real
`Hello → Identify (SHA-256) → Identified → RequestResponse` handshake, so the
client is exercised over an actual WebSocket rather than mocked out.
`smoke_ui.py` drives the real GUI and asserts the fake OBS saw `StartRecord` at
~1s and `StopRecord` ~2s later.
