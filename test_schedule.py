"""Weekday RTH schedule helpers."""
from datetime import datetime
from zoneinfo import ZoneInfo
from schedule import (
    FLAT_HHMM, in_entry_window, in_scan_window, is_weekday, session_label, should_flatten,
)

ET = ZoneInfo("America/New_York")


def _dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_weekday_rth():
    tue = _dt(2026, 9, 1, 10, 0)  # Tuesday
    assert is_weekday(tue)
    assert in_scan_window(tue)
    assert in_entry_window(tue)
    assert not should_flatten(tue)
    assert session_label(tue) == "REGULAR"


def test_before_open_and_weekend():
    monday_open = _dt(2026, 8, 31, 9, 29)
    assert is_weekday(monday_open)
    assert not in_scan_window(monday_open)
    sat = _dt(2026, 8, 29, 12, 0)
    assert not is_weekday(sat)
    assert not in_scan_window(sat)
    assert not in_entry_window(sat)
    assert session_label(sat) == "WEEKEND"


def test_flatten_1558():
    just_before = _dt(2026, 9, 1, 15, 57)
    at_flat = _dt(2026, 9, 1, 15, 58)
    at_close = _dt(2026, 9, 1, 16, 0)
    assert in_entry_window(just_before)
    assert not should_flatten(just_before)
    assert not in_entry_window(at_flat)
    assert should_flatten(at_flat)
    assert in_scan_window(at_flat)  # scanners may still look until 4:00
    assert not in_scan_window(at_close)
    assert should_flatten(at_close)
    assert FLAT_HHMM == 1558


if __name__ == "__main__":
    test_weekday_rth()
    test_before_open_and_weekend()
    test_flatten_1558()
    print("OK schedule")
