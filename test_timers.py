"""Unit tests for the pure (platform-independent) timer + layout helpers."""

from __future__ import annotations

import pytest

import win_layout as wl
from Timersv2 import hms_to_seconds, seconds_to_hms
from win_layout import Rect, monitor_for_rect, parse_hotkey


def mon(index, left, top, width, height, primary=False):
    rect = Rect(left, top, left + width, top + height)
    return wl.Monitor(index, 0, rect, rect, primary, "\\\\.\\DISPLAY%d" % (index + 1))


# --- time parsing ---------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [("00:00:05", 5), ("0:0:5", 5), ("00:15:00", 900), ("1:00:00", 3600),
     ("15:00", 900), ("900", 900), ("", None), ("abc", None), ("00:99:00", None),
     ("-1:00:00", None), ("1:2:3:4", None), ("1:-2:00", None)],
)
def test_hms_to_seconds(text, expected):
    if expected is None:
        with pytest.raises(ValueError):
            hms_to_seconds(text)
    else:
        assert hms_to_seconds(text) == expected


def test_seconds_to_hms_rounds_down_and_clamps():
    assert seconds_to_hms(0) == "00:00:00"
    assert seconds_to_hms(59.9) == "00:00:59"
    assert seconds_to_hms(3661) == "01:01:01"
    assert seconds_to_hms(-5) == "00:00:00"


# --- hotkey parsing -------------------------------------------------------
def test_parse_hotkey_basic():
    assert parse_hotkey("ctrl+alt+b") == [0x11, 0x12, ord("B")]
    assert parse_hotkey("  Ctrl + Alt + N ") == [0x11, 0x12, ord("N")]


def test_parse_hotkey_function_and_digit_keys():
    assert parse_hotkey("ctrl+f13")[-1] == 0x6F + 13
    assert parse_hotkey("shift+1") == [0x10, ord("1")]
    assert parse_hotkey("win+space") == [0x5B, 0x20]


@pytest.mark.parametrize("bad", ["", "   ", "ctrl+", "ctrl+nonsense", "+"])
def test_parse_hotkey_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_hotkey(bad)


# --- monitor maths --------------------------------------------------------
LEFT = mon(0, 0, 0, 1920, 1080, primary=True)  # primary, left
RIGHT = mon(1, 1920, 0, 2560, 1440)      # right, bigger
ABOVE = mon(2, 0, -1080, 1920, 1080)
MONITORS = [LEFT, RIGHT, ABOVE]


def test_monitor_by_largest_overlap():
    assert monitor_for_rect(Rect(100, 100, 900, 700), MONITORS) is LEFT
    assert monitor_for_rect(Rect(2000, 200, 4000, 1200), MONITORS) is RIGHT
    # Straddling the seam: mostly on the right monitor wins.
    assert monitor_for_rect(Rect(1800, 100, 2600, 900), MONITORS) is RIGHT
    assert monitor_for_rect(Rect(1500, 100, 2000, 900), MONITORS) is LEFT


def test_monitor_for_maximized_rect():
    assert monitor_for_rect(Rect(1920, 0, 4480, 1440), MONITORS) is RIGHT


def test_monitor_falls_back_when_offscreen():
    # Entirely outside every monitor: must still return something sane.
    assert monitor_for_rect(Rect(9000, 9000, 9400, 9400), MONITORS) is LEFT


def test_monitor_empty_list():
    assert monitor_for_rect(Rect(0, 0, 10, 10), []) is None


def test_rect_helpers():
    r = Rect(10, 20, 110, 220)
    assert (r.width, r.height) == (100, 200)
    assert r.center == (60, 120)
    assert r.contains((10, 20)) and not r.contains((110, 20))
    assert r.intersection_area(Rect(0, 0, 20, 30)) == 10 * 10
    assert r.intersection_area(Rect(500, 500, 600, 600)) == 0
    assert r == Rect(10, 20, 110, 220)


def test_monitor_label_starts_with_monitor_number():
    assert LEFT.label.startswith("Monitor 1")
    assert "[primary]" in LEFT.label
    assert "[primary]" not in RIGHT.label


def test_monitor_number_from_label():
    from Timersv2 import _monitor_number, _safe_int

    assert _monitor_number("Monitor 2 (2560x1440)") == 2
    assert _monitor_number("Monitor 1 (1920x1080) [primary]") == 1
    assert _monitor_number("garbage") == 1
    assert _safe_int("4455", 1) == 4455
    assert _safe_int("", 4455) == 4455
    assert _safe_int("abc", 7) == 7


def test_stub_functions_fail_loudly_off_windows():
    if wl.IS_WINDOWS:
        pytest.skip("Windows only")
    with pytest.raises(RuntimeError):
        wl.list_monitors()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
