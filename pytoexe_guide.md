# Python → .exe in VS Code: OBS Session Timer

How to turn `Timersv2.py` into a standalone `OBS Session Timer.exe` that runs on a
Windows machine with **no Python installed**. Written for VS Code + an existing
Python install, using PyInstaller.

---

## 0. What this project actually needs

Checked against the code in this repo, so you don't guess:

| Item | Value |
| --- | --- |
| Entry point | `Timersv2.py` (`if __name__ == "__main__":` near the end, line 763) |
| Third-party deps | **none** — stdlib + tkinter only |
| Bundled local modules | `obs_ws.py`, `win_layout.py` (imported by `Timersv2.py`) |
| Data files / assets | **none** |
| Build-time tool | `pyinstaller` (the only thing you pip install) |

Because `Timersv2.py` imports `obs_ws` and `win_layout`, PyInstaller follows those
imports and bundles them automatically. **No `--hidden-import` flags, no
`--add-data`, no spec editing required** to get a working exe. Everything below
past step 1 is the build loop (and step 1 is already done for you).

> PyInstaller does not cross-compile. A Windows `.exe` must be built **on
> Windows**. Building on Linux/macOS produces a Linux/macOS binary.

---

## 1. The settings-path fix — already applied, read this to understand it

> **Status: done.** Commit `968d389` + the follow-up commit in this repo changed
> `Timersv2.py` for you and bumped `APP_VERSION` to `2.0.1`. You do **not** have to
> type or paste any code from this section. Read it so you know why the file looks
> the way it does, and so you don't accidentally undo it later.

### The one line that mattered

Original line 43 of `Timersv2.py`:

```python
SETTINGS_PATH = Path(__file__).with_name("timer_settings.json")
```

`SETTINGS_PATH` is simply **where the app saves its settings file** —
`timer_settings.json`, which holds the OBS WebSocket port, the password, your start
and stop offsets, and your shortcuts. Saving and loading both use this one variable.

`__file__` is a Python built-in meaning **"the path of the file I am running
from"**. So that line reads as: *"save my settings next to the script that's
executing right now."* That is correct when you run `python Timersv2.py`. It is
wrong once the code is inside an exe.

### Why an exe breaks it

A `--onefile` exe is not "the script". It's a small launcher with the whole app
compressed inside it. Every time you double-click it, it does this:

1. Picks a brand-new empty folder in `%TEMP%`, named something like `_MEI123456`.
2. Unpacks your `.py`, `obs_ws.py`, `win_layout.py` and the Python runtime into it.
3. Runs `Timersv2.py` **from that temp folder** — so `__file__` points there.
4. Deletes the temp folder when the app closes.

So with the original line, the app writes `timer_settings.json` into
`%TEMP%\_MEI123456\` instead of next to your exe — and Windows wipes it minutes
later. This is the real output of a frozen build, measured, not guessed:

```
frozen     : True
__file__   : C:\Users\Dain\AppData\Local\Temp\_MEI123456\Timersv2.py
Path(__file__).with_name -> ...\_MEI123456\timer_settings.json   ← deleted on exit
```

**What that looks like from the outside:** everything works the first time. You
paste your OBS password, tick *Test connection*, it goes green, you close the app.
Next launch — port back to `4455`, password box empty, offsets back to
`00:00:05`/`00:15:00`. It reads like the app "forgot" its settings, or like you
never clicked Save. Nothing is broken in the save code; it saved perfectly, into a
folder that no longer exists.

### What the fix does

It asks one question at startup: *am I a `.py` or an `.exe`?* PyInstaller sets
`sys.frozen = True` in the exe (and leaves it unset when you run the script), so:

```python
# Where timer_settings.json lives.
#
# Running as a normal .py  -> next to this source file.
# Running as a frozen .exe -> next to the .exe.
#
# Do NOT use Path(__file__) when frozen: PyInstaller's --onefile build unpacks
# the script into %TEMP%\_MEIxxxxxx, so __file__ points in there and the folder
# is deleted when the app exits -- settings (including the OBS password) would
# silently reset on every launch. sys.executable is the exe's real path.
if getattr(sys, "frozen", False):
    SETTINGS_PATH = Path(sys.executable).with_name("timer_settings.json")
else:
    SETTINGS_PATH = Path(__file__).with_name("timer_settings.json")
```

| Piece | Meaning |
| --- | --- |
| `getattr(sys, "frozen", False)` | `True` inside a PyInstaller/Nuitka exe, `False` when running the `.py`. `getattr` with a default avoids `AttributeError` when the attribute doesn't exist. |
| `sys.executable` | The actual file being run: `...\python.exe` normally, your `OBS Session Timer.exe` when frozen. Unlike `__file__`, it never points into the temp folder. |
| `.with_name(...)` | "Take the folder this file is in, and put `timer_settings.json` in that same folder." |

Result, same folder in both cases — settings survive restarts, and they sit beside
the exe where you can find and back them up.

### Finding it in VS Code

`Ctrl+G` → type `41` → `Enter` jumps straight to `APP_VERSION`, and the block above
is immediately below it. To see the whole thing in context, read lines 41–55. `sys`
was already imported at line 31, so no new imports were needed.

### Verify it after your first build (30 seconds)

1. Build and launch the exe once, then close it.
2. Look in the `dist` folder next to `OBS Session Timer.exe` — `timer_settings.json`
   must be **there**.
3. Change a setting (e.g. the stop offset), close, reopen. The change must still be
   there.

If the file instead appears under `%TEMP%\_MEI*`, this block got reverted.

> **If you ever move the exe into `C:\Program Files\`**, that folder is
> read-only for normal users and saving next to the exe will fail. Either keep the
> exe somewhere user-writable (Desktop, `C:\Tools\`, `%LOCALAPPDATA%`) or change the
> frozen branch to
> `Path(os.getenv("LOCALAPPDATA")) / "OBSSessionTimer" / "timer_settings.json"`
> and `mkdir(parents=True, exist_ok=True)` before writing. For a personal tool on
> your own desktop, next-to-the-exe is the right call.

---

## 2. One-time VS Code setup

1. **File → Open Folder →** this repo.
2. **Ctrl+Shift+P → `Python: Select Interpreter` → `Create Virtual Environment…` → Venv →**
   pick your Python 3.8+ install (tkinter ships with the python.org installer).
   VS Code creates `.venv/` and activates it in new terminals.
3. **Terminal → New Terminal** (it should show `(.venv)`) and install the build tool:

   ```powershell
   python -m pip install --upgrade pyinstaller
   pyinstaller --version
   ```

   Pin it if you want reproducible builds: `pip install "pyinstaller==6.22.3"`.
4. Sanity check the app runs from source before packaging anything:

   ```powershell
   python Timersv2.py
   ```

   If it fails here, it will fail as an exe. Fix it first.

---

## 3. Keep build output out of git

`.gitignore` already ignores `timer_settings.json` (it holds the WebSocket
password) but **not** PyInstaller's output. Add:

```gitignore
# PyInstaller
build/
dist/
OBS-Session-Timer-debug.spec
```

Do **not** ignore `OBS Session Timer.spec` — that file is the release recipe and
belongs in the repo (see step 6). Only the throwaway debug spec is ignored.

---

## 4. First build: console version (debuggable)

`--windowed` hides all errors, including tracebacks. Build the console version
first so a crash is visible.

In the VS Code terminal:

```powershell
pyinstaller --noconfirm --clean --onefile --console --name OBS-Session-Timer-debug Timersv2.py
```

| Flag | Why |
| --- | --- |
| `--onefile` | Single .exe — cleanest to copy around |
| `--console` | Keeps a terminal attached so exceptions print |
| `--clean` | Discards cached analysis; avoids stale-module weirdness |
| `--noconfirm` | Overwrites `dist/` without prompting |

Run it:

```powershell
.\dist\OBS-Session-Timer-debug.exe
```

Expect a console window **plus** the timer GUI. Watch the terminal for the
startup log line (WS mode / DPI awareness / monitor count). Test the real
workflow: `Test connection` to OBS, start a session, let it stop a recording.
Only then move on.

---

## 5. Release build: no console

```powershell
pyinstaller --noconfirm --clean --onefile --windowed --name "OBS Session Timer" Timersv2.py
```

`--windowed` (alias `--noconsole`) is the PyInstaller equivalent of `pythonw`:
no console window, GUI only. Output: `dist\OBS Session Timer.exe`.

Copy that single file to any Windows box — OBS 28+ and nothing else needed.

### Optional: add an icon

PyInstaller wants `.ico`, not `.png`. Convert with any converter, drop
`app.ico` in the repo, then:

```powershell
pyinstaller --noconfirm --clean --onefile --windowed --icon app.ico --name "OBS Session Timer" Timersv2.py
```

### Optional: file properties (right-click → Properties → Details)

Create `version_info.txt`:

```python
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(2, 0, 1, 0),
    prodvers=(2, 0, 1, 0),
    mask=0x3f, flags=0x0, OS=0x40004,
    fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Dain Wyman'),
        StringStruct('FileDescription', 'OBS Session Timer'),
        StringStruct('FileVersion', '2.0.1.0'),
        StringStruct('InternalName', 'OBS-Session-Timer'),
        StringStruct('OriginalFilename', 'OBS Session Timer.exe'),
        StringStruct('ProductName', 'OBS Session Timer'),
        StringStruct('ProductVersion', '2.0.1.0')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
```

Add `--version-file version_info.txt` to the build command. Bump both the tuple
and the string when you bump `APP_VERSION` in `Timersv2.py` (currently `2.0.1`, so the
exe reports `2.0.1.0`).

---

## 6. Commit the spec file, then rebuild from it

The release build writes `OBS Session Timer.spec` — the full recipe. Commit that
one and ignore the debug build's `OBS-Session-Timer-debug.spec`, so the repo keeps
exactly one authoritative recipe. From then on, rebuild with:

```powershell
pyinstaller --noconfirm --clean "OBS Session Timer.spec"
```

Why bother: flags typed in a terminal get lost; the spec is versioned. Edit it
directly for permanent changes:

- `icon='app.ico'` — inside the `EXE(...)` call
- `version='version_info.txt'` — inside the `EXE(...)` call
- `datas=[('somefile.txt', '.')]` — only if you ever add data files
- rename → change `name=` in `EXE(...)`

**Rule:** once a spec exists, pass the spec *instead of* the `.py` and flags.
Mixing them silently ignores your flags.

---

## 7. Wire it into VS Code (build with Ctrl+Shift+B)

`.vscode/tasks.json`:

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "exe: debug (console)",
      "type": "shell",
      "command": "${workspaceFolder}/.venv/Scripts/pyinstaller.exe",
      "args": [
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "OBS-Session-Timer-debug",
        "Timersv2.py"
      ],
      "problemMatcher": [],
      "group": "build"
    },
    {
      "label": "exe: release (windowed)",
      "type": "shell",
      "command": "${workspaceFolder}/.venv/Scripts/pyinstaller.exe",
      "args": ["--noconfirm", "--clean", "OBS Session Timer.spec"],
      "problemMatcher": [],
      "group": { "kind": "build", "isDefault": true }
    }
  ]
}
```

`Ctrl+Shift+B` runs the release task; `Ctrl+Shift+P → Tasks: Run Task` picks the
debug one.

`.vscode/launch.json` if you'd rather debug the source than the exe:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python: Timersv2",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/Timersv2.py",
      "console": "integratedTerminal",
      "justMyCode": true
    }
  ]
}
```

An exe is not debuggable with breakpoints — debug the `.py`, package when the
behaviour is right.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'obs_ws'` | You built from a different folder than `Timersv2.py`. Run `pyinstaller` with the repo as the working directory. |
| Exe launches and vanishes instantly (windowed build) | Rebuild `--console` and run it in a terminal — the traceback appears there. |
| Settings/password reset every launch | The step 1 block was reverted/overwritten. `timer_settings.json` is being written into the temp `_MEI` dir — check lines 41–55 of `Timersv2.py`. |
| Windows: *"Windows protected your PC"* (SmartScreen) | Unsigned exe. **More info → Run anyway.** Persistent fix is code signing (below). |
| Antivirus quarantines the exe | Common `--onefile` false positive (the bootloader self-extracts). Add an exclusion for `dist\`, or switch to `--onedir`. |
| 3–8 s delay before the window appears | `--onefile` extracts to `%TEMP%` on every launch. Use `--onedir` (a folder instead of one file) — starts instantly, still no Python needed. |
| `ImportError: DLL load failed ... tcl/tk` | Broken tkinter in the Python you built with. Reinstall Python from python.org with the *tcl/tk and IDLE* option checked, recreate `.venv`, rebuild. |
| Exe works on your PC, not on another | Built with a 32/64-bit mismatch. Build x64 on 64-bit Python. |
| Build succeeds but old code runs | `--clean` missing, or `build/` stale — delete `build/` and `dist/` and rebuild. |

### Code signing (optional, kills SmartScreen for good)

An OV certificate (~$200+/yr) signed with `signtool` removes the warning for
people who download the exe. For a personal tool on your own machines, the
right answer is: don't bother, just exclude `dist\` from Defender and click
through SmartScreen once.

---

## 9. Final checklist

- [ ] Settings-path fix still in place (lines 41–55), and `timer_settings.json` appears next to the exe after one launch
- [ ] `python Timersv2.py` runs clean from source
- [ ] Console build tested against real OBS: Test connection, start, auto-stop, cancel
- [ ] Windowed build launches with no console and no flash-and-die
- [ ] `build/`, `dist/` git-ignored; `.spec` committed
- [ ] Version numbers match between `APP_VERSION`, the spec, and `version_info.txt`
- [ ] Exe copied to a machine without Python and verified there — the only test that proves it's standalone

---

## 10. The whole thing in four commands

```powershell
python -m pip install --upgrade pyinstaller
pyinstaller --noconfirm --clean --onefile --console --name OBS-Session-Timer-debug Timersv2.py
.\dist\OBS-Session-Timer-debug.exe
pyinstaller --noconfirm --clean "OBS Session Timer.spec"
```

`dist\OBS Session Timer.exe` is the deliverable.
