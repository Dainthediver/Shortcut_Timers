"""Windows window + monitor helpers for the OBS session timer.

Standard library only (ctypes).  On a dual-monitor desk the naive approach --
``pygetwindow.activate()`` -- fails in three ways: it silently no-ops when the
calling process is not already foreground, it can re-place a maximized window,
and it reports coordinates in the caller's DPI virtualisation instead of real
pixels so a 150%-scaled second monitor gets the wrong rect.  This module fixes
all three: per-monitor DPI awareness, largest-intersection monitor detection,
AttachThreadInput + ALT-tap foregrounding, and placement restoration after the
window is brought forward.

Pure helpers (``Rect``, ``monitor_for_rect``, ``parse_hotkey``) have no Windows
dependency and are unit tested on Linux; the ctypes layer degrades to a clear
RuntimeError off-Windows.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys

IS_WINDOWS = sys.platform == "win32"

# ShowWindow / SetWindowPos constants
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
DWMWA_EXTENDED_FRAME_BOUNDS = 9
VK_MENU = 0x12

# Virtual key names accepted in shortcut strings.
_VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "cmd": 0x5B,
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "minus": 0xBD, "plus": 0xBB, "equal": 0xBB,
    "comma": 0xBC, "period": 0xBE, "slash": 0xBF, "semicolon": 0xBA,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
_VK.update({f"f{n}": 0x6F + n for n in range(1, 25)})  # F1..F24
_VK.update({c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"})
_VK.update({str(d): ord(str(d)) for d in range(10)})


class Rect:
    __slots__ = ("left", "top", "right", "bottom")

    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom

    def intersection_area(self, other: "Rect") -> int:
        w = min(self.right, other.right) - max(self.left, other.left)
        h = min(self.bottom, other.bottom) - max(self.top, other.top)
        return w * h if w > 0 and h > 0 else 0

    def contains(self, point: tuple[int, int]) -> bool:
        x, y = point
        return self.left <= x < self.right and self.top <= y < self.bottom

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Rect) and self.as_tuple() == other.as_tuple()

    def __repr__(self) -> str:
        return "Rect(%d, %d, %d, %d)" % self.as_tuple()


class Monitor:
    __slots__ = ("index", "handle", "rect", "work", "primary", "device")

    def __init__(self, index, handle, rect: Rect, work: Rect, primary: bool, device: str):
        self.index, self.handle, self.rect, self.work = index, handle, rect, work
        self.primary, self.device = primary, device

    @property
    def label(self) -> str:
        return "Monitor %d (%dx%d)%s" % (
            self.index + 1,
            self.rect.width,
            self.rect.height,
            " [primary]" if self.primary else "",
        )

    def __repr__(self) -> str:
        return "Monitor(%s, %s)" % (self.label, self.rect)


class Window:
    __slots__ = ("hwnd", "title", "rect")

    def __init__(self, hwnd: int, title: str, rect: Rect):
        self.hwnd, self.title, self.rect = hwnd, title, rect

    def __repr__(self) -> str:
        return "Window(0x%X, %r, %s)" % (self.hwnd, self.title, self.rect)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def monitor_for_rect(rect: Rect, monitors: list[Monitor]) -> Monitor | None:
    """The monitor a window mostly lives on (largest overlap, then centre hit)."""
    if not monitors:
        return None
    best, best_area = None, 0
    for mon in monitors:
        area = rect.intersection_area(mon.rect)
        if area > best_area:
            best, best_area = mon, area
    if best:
        return best
    for mon in monitors:  # window entirely off-screen / straddling oddly
        if mon.rect.contains(rect.center) or mon.work.contains(rect.center):
            return mon
    return monitors[0]


def parse_hotkey(text: str) -> list[int]:
    """'ctrl+alt+b' -> [VK_CONTROL, VK_MENU, VK_B].  Raises ValueError."""
    tokens = [t.strip().lower() for t in (text or "").split("+") if t.strip()]
    if not tokens:
        raise ValueError("empty shortcut")
    codes = []
    for token in tokens:
        if token not in _VK:
            raise ValueError("unknown key %r" % token)
        codes.append(_VK[token])
    if codes[-1] in (0x11, 0x12, 0x10, 0x5B, 0x5C):
        raise ValueError("shortcut needs a final key, e.g. ctrl+alt+b")
    return codes


# --------------------------------------------------------------------------
# ctypes plumbing (Windows only)
# --------------------------------------------------------------------------
if IS_WINDOWS:  # pragma: no cover - exercised on Windows only
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        _dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:  # pragma: no cover
        _dwmapi = None

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.SetFocus.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM
    ]
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HMONITOR
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    _kernel32.Sleep.argtypes = [wintypes.DWORD]

    def enable_dpi_awareness() -> str:
        """Per-monitor v2 so window rects are real pixels on mixed-DPI setups."""
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
            return "per-monitor-v2"
        except Exception:
            pass
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
            return "system"
        except Exception:
            pass
        try:
            _user32.SetProcessDPIAware()
            return "legacy"
        except Exception:
            return "none (coordinates may be scaled)"

    def list_monitors() -> list[Monitor]:
        found: list[Monitor] = []

        def _cb(hmon, hdc, lprect, lparam):
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                found.append(
                    Monitor(
                        index=len(found),
                        handle=hmon,
                        rect=Rect(info.rcMonitor.left, info.rcMonitor.top,
                                  info.rcMonitor.right, info.rcMonitor.bottom),
                        work=Rect(info.rcWork.left, info.rcWork.top,
                                  info.rcWork.right, info.rcWork.bottom),
                        primary=bool(info.dwFlags & 1),
                        device=info.szDevice,
                    )
                )
            return True

        _user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
        # Stable, predictable numbering: primary first, then left-to-right.
        found.sort(key=lambda m: (not m.primary, m.rect.left, m.rect.top))
        for i, mon in enumerate(found):
            mon.index = i
        return found

    def window_rect(hwnd: int) -> Rect:
        """Visible bounds -- DWM extended frame, which excludes the invisible
        resize border that makes GetWindowRect over-report maximized windows."""
        if _dwmapi is not None:
            r = wintypes.RECT()
            try:
                if _dwmapi.DwmGetWindowAttribute(
                    wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                    ctypes.byref(r), ctypes.sizeof(r),
                ) == 0:
                    return Rect(r.left, r.top, r.right, r.bottom)
            except Exception:
                pass
        r = wintypes.RECT()
        _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
        return Rect(r.left, r.top, r.right, r.bottom)

    def list_windows() -> list[Window]:
        own_pid = _kernel32.GetCurrentProcessId()
        windows: list[Window] = []

        def _cb(hwnd, lparam):
            if not _user32.IsWindowVisible(hwnd):
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if not title.strip():
                return True
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own_pid:
                return True  # never target this timer's own window
            windows.append(Window(int(hwnd), title, window_rect(int(hwnd))))
            return True

        _user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return windows

    def find_window(prefix: str, windows: list[Window] | None = None) -> Window | None:
        prefix_lower = (prefix or "").strip().lower()
        if not prefix_lower:
            return None
        candidates = [
            w for w in (windows if windows is not None else list_windows())
            if w.title.lower().startswith(prefix_lower)
        ]
        if not candidates:
            return None
        # Prefer a real, on-screen, largest window; else the longest title match.
        candidates.sort(key=lambda w: (w.rect.width * w.rect.height), reverse=True)
        return candidates[0]

    def force_foreground(hwnd: int, keep_placement: bool = True) -> tuple[bool, str]:
        """Bring a window to the front reliably, then put it back where it was."""
        notes = []
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)
            _kernel32.Sleep(120)
            notes.append("restored from minimized")
        before = window_rect(hwnd)

        if _user32.GetForegroundWindow() != hwnd:
            fg = _user32.GetForegroundWindow()
            fg_thread = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
            me = _kernel32.GetCurrentThreadId()
            attached = bool(
                fg_thread and _user32.AttachThreadInput(me, fg_thread, True)
            )
            try:
                _user32.BringWindowToTop(hwnd)
                _user32.SetForegroundWindow(hwnd)
                _user32.SetFocus(hwnd)
            finally:
                if attached:
                    _user32.AttachThreadInput(me, fg_thread, False)

        for _ in range(3):
            if _user32.GetForegroundWindow() == hwnd:
                break
            # A synthetic ALT tap clears Windows' foreground lock.
            _user32.keybd_event(VK_MENU, 0, 0, None)
            _user32.keybd_event(VK_MENU, 0, 2, None)
            _user32.SetForegroundWindow(hwnd)
            _kernel32.Sleep(150)

        ok = _user32.GetForegroundWindow() == hwnd
        after = window_rect(hwnd)
        if keep_placement and after != before:
            _user32.SetWindowPos(
                hwnd, 0, before.left, before.top, before.width, before.height,
                SWP_NOZORDER | SWP_NOACTIVATE,
            )
            notes.append("placement restored (%s -> %s)" % (before, after))
        return ok, "; ".join(notes)

    def send_hotkey(keys: list[int]) -> None:
        """Press modifiers, tap the key, release modifiers (no pyautogui needed)."""
        KEYUP = 2
        pressed = []
        for vk in keys[:-1]:
            _user32.keybd_event(vk, 0, 0, None)
            pressed.append(vk)
            _kernel32.Sleep(30)
        final = keys[-1]
        _user32.keybd_event(final, 0, 0, None)
        _kernel32.Sleep(40)
        _user32.keybd_event(final, 0, KEYUP, None)
        for vk in reversed(pressed):
            _kernel32.Sleep(20)
            _user32.keybd_event(vk, 0, KEYUP, None)

else:  # pragma: no cover - Linux/macOS import path
    def enable_dpi_awareness() -> str:  # type: ignore[misc]
        raise RuntimeError("Windows only")

    def list_monitors() -> list[Monitor]:  # type: ignore[misc]
        raise RuntimeError("Windows only")

    def list_windows() -> list[Window]:  # type: ignore[misc]
        raise RuntimeError("Windows only")

    def find_window(prefix: str, windows=None):  # type: ignore[misc]
        raise RuntimeError("Windows only")

    def window_rect(hwnd: int) -> Rect:  # type: ignore[misc]
        raise RuntimeError("Windows only")

    def force_foreground(hwnd: int, keep_placement: bool = True):  # type: ignore[misc]
        raise RuntimeError("Windows only")

    def send_hotkey(keys: list[int]) -> None:  # type: ignore[misc]
        raise RuntimeError("Windows only")
