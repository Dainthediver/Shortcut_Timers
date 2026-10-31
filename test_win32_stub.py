"""Exercise the Windows code path of win_layout.py on Linux with stubbed ctypes.

The Windows branch is the one that actually runs on Dain's machine, and we
cannot execute it here -- so at least prove it imports, its ctypes signatures
apply, and its control flow is right (monitor enumeration/sorting, window
matching, foreground + placement restoration) against a fake user32.
"""

from __future__ import annotations

import ctypes
import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

MODULE_PATH = Path(__file__).with_name("win_layout.py")


class _FakeFn:
    def __init__(self, name, handler=None):
        self.name = name
        self.argtypes = None
        self.restype = None
        self.handler = handler
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.handler:
            return self.handler(*args, **kwargs)
        return 0


class _FakeDLL:
    def __init__(self, name="fake"):
        self.name = name
        self.fns: dict[str, _FakeFn] = {}

    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        if item not in self.fns:
            self.fns[item] = _FakeFn(item)
        return self.fns[item]


def _fake_wintypes_ok() -> bool:
    import ctypes.wintypes as w

    return all(hasattr(w, n) for n in ("HMONITOR", "HDC", "HWND", "LPARAM", "WCHAR", "BYTE"))


pytestmark = pytest.mark.skipif(
    not _fake_wintypes_ok(), reason="ctypes.wintypes lacks Windows handle types"
)


def load_win_layout():
    """Import win_layout.py as if it were running on Windows."""
    dlls: dict[str, _FakeDLL] = {}

    def fake_windll_factory(name, *args, **kwargs):
        dlls.setdefault(name, _FakeDLL(name))
        return dlls[name]

    def fake_winfunctype(*args, **kwargs):
        return lambda fn: fn

    spec = importlib.util.spec_from_file_location("win_layout_win", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "platform", "win32"), \
            mock.patch.object(ctypes, "WinDLL", fake_windll_factory, create=True), \
            mock.patch.object(ctypes, "WINFUNCTYPE", fake_winfunctype, create=True):
        spec.loader.exec_module(module)
    return module, dlls


@pytest.fixture()
def win():
    module, dlls = load_win_layout()
    assert module.IS_WINDOWS is True
    # Force the GetWindowRect fallback so the fake rect state is authoritative.
    dlls["dwmapi"].fns["DwmGetWindowAttribute"] = _FakeFn(
        "DwmGetWindowAttribute", lambda *args: 1
    )
    return module, dlls["user32"]


# --- import / signature sanity -------------------------------------------
def test_windows_branch_defines_every_entry_point(win):
    module, _ = win
    for name in ("enable_dpi_awareness", "list_monitors", "list_windows", "find_window",
                 "window_rect", "force_foreground", "send_hotkey"):
        assert callable(getattr(module, name)), name


def test_pure_helpers_still_work_on_windows_path(win):
    module, _ = win
    assert module.parse_hotkey("ctrl+alt+b") == [0x11, 0x12, ord("B")]
    monitors = [
        module.Monitor(0, 1, module.Rect(0, 0, 1920, 1080), module.Rect(0, 0, 1920, 1080), True, "L"),
        module.Monitor(1, 2, module.Rect(1920, 0, 4480, 1440), module.Rect(1920, 0, 4480, 1440), False, "R"),
    ]
    assert module.monitor_for_rect(module.Rect(2000, 100, 3000, 900), monitors).index == 1


def test_enable_dpi_awareness_reports_mode(win):
    module, _ = win
    assert module.enable_dpi_awareness() in ("per-monitor-v2", "system", "legacy",
                                             "none (coordinates may be scaled)")


# --- monitor enumeration --------------------------------------------------
def test_list_monitors_numbers_primary_first(win):
    module, user32 = win
    # Right monitor is enumerated first, left (primary) second: primary must win.
    layout = {
        10: (0, 0, 1920, 1080, 0, True, "LEFT"),      # primary, dwFlags=1
        20: (1920, 0, 4480, 1440, 0, False, "RIGHT"),
    }
    order = [20, 10]

    def enum_monitors(hdc, lprect, callback, lparam):
        for hmon in order:
            callback(hmon, None, None, None)
        return 1

    def get_monitor_info(hmon, ptr):
        info = ptr._obj
        left, top, right, bottom, flags_extra, primary, _ = layout[hmon]
        info.rcMonitor.left, info.rcMonitor.top = left, top
        info.rcMonitor.right, info.rcMonitor.bottom = right, bottom
        info.rcWork.left, info.rcWork.top = left, top
        info.rcWork.right, info.rcWork.bottom = right, bottom
        info.dwFlags = 1 if primary else 0
        info.szDevice = layout[hmon][6]
        return 1

    user32.fns["EnumDisplayMonitors"] = _FakeFn("EnumDisplayMonitors", enum_monitors)
    user32.fns["GetMonitorInfoW"] = _FakeFn("GetMonitorInfoW", get_monitor_info)

    monitors = module.list_monitors()
    assert len(monitors) == 2
    assert monitors[0].primary and monitors[0].index == 0
    assert monitors[0].device == "LEFT"
    assert monitors[1].rect.left == 1920
    assert "Monitor 2" in monitors[1].label


# --- window discovery + foregrounding ------------------------------------
def test_find_window_prefers_biggest_and_ignores_own_window(win):
    module, _ = win
    small = module.Window(1, "OBS Studio - small", module.Rect(0, 0, 100, 100))
    big = module.Window(2, "OBS Studio - 1920x1080", module.Rect(0, 0, 1920, 1080))
    other = module.Window(3, "Notepad", module.Rect(0, 0, 5000, 5000))
    chosen = module.find_window("obs", [small, big, other])
    assert chosen.hwnd == 2
    assert module.find_window("nothing-here", [small, big]) is None
    assert module.find_window("", [small]) is None


def test_force_foreground_restores_placement(win):
    module, user32 = win
    state = {"foreground": 99, "rect": (2000, 50, 3900, 1100)}

    def get_foreground():
        return state["foreground"]

    def set_foreground(hwnd):
        state["foreground"] = hwnd
        return 1

    def get_window_rect(hwnd, ptr):
        info = ptr._obj
        info.left, info.top, info.right, info.bottom = state["rect"]
        return 1

    def set_window_pos(hwnd, after, x, y, cx, cy, flags):
        state["rect"] = (x, y, x + cx, y + cy)
        state["restored_flags"] = flags
        return 1

    user32.fns["GetForegroundWindow"] = _FakeFn("GetForegroundWindow", get_foreground)
    user32.fns["SetForegroundWindow"] = _FakeFn("SetForegroundWindow", set_foreground)
    user32.fns["GetWindowRect"] = _FakeFn("GetWindowRect", get_window_rect)
    user32.fns["SetWindowPos"] = _FakeFn("SetWindowPos", set_window_pos)
    user32.fns["IsIconic"] = _FakeFn("IsIconic", lambda hwnd: 0)

    # DwmGetWindowAttribute would move the rect as OBS gets activated.
    original = state["rect"]

    def activate_moves_window(hwnd):
        state["foreground"] = hwnd
        state["rect"] = (0, 0, 1920, 1080)  # hopped to the other monitor
        return 1

    user32.fns["SetForegroundWindow"] = _FakeFn("SetForegroundWindow", activate_moves_window)

    ok, notes = module.force_foreground(42, keep_placement=True)
    assert ok is True
    assert state["rect"] == original, "window must be put back where it was"
    assert "placement restored" in notes
    assert user32.fns["SetWindowPos"].calls[-1][0][6] == (
        module.SWP_NOZORDER | module.SWP_NOACTIVATE
    )


def test_force_foreground_when_already_front(win):
    module, user32 = win
    user32.fns["GetForegroundWindow"] = _FakeFn("GetForegroundWindow", lambda: 7)
    user32.fns["IsIconic"] = _FakeFn("IsIconic", lambda hwnd: 0)
    user32.fns["GetWindowRect"] = _FakeFn("GetWindowRect", lambda h, p: 1)
    ok, notes = module.force_foreground(7, keep_placement=True)
    assert ok is True
    assert notes == ""


def test_force_foreground_reports_failure(win):
    module, user32 = win
    user32.fns["GetForegroundWindow"] = _FakeFn("GetForegroundWindow", lambda: 99)
    user32.fns["SetForegroundWindow"] = _FakeFn("SetForegroundWindow", lambda h: 0)
    user32.fns["IsIconic"] = _FakeFn("IsIconic", lambda hwnd: 0)
    user32.fns["GetWindowRect"] = _FakeFn("GetWindowRect", lambda h, p: 1)
    ok, _ = module.force_foreground(7, keep_placement=False)
    assert ok is False


def test_send_hotkey_presses_and_releases_in_order(win):
    module, user32 = win
    events = []
    user32.fns["keybd_event"] = _FakeFn(
        "keybd_event", lambda vk, scan, flags, extra: events.append((vk, flags))
    )
    module.send_hotkey([0x11, 0x12, ord("B")])
    kinds = [(vk, flags) for vk, flags in events]
    assert kinds[0] == (0x11, 0)      # ctrl down
    assert kinds[1] == (0x12, 0)      # alt down
    assert (ord("B"), 0) in kinds     # b down
    assert (ord("B"), 2) in kinds     # b up
    assert kinds[-1] == (0x11, 2)     # ctrl released last


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
