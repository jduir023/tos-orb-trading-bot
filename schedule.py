"""Weekday RTH schedule (America/New_York).

Scanners: 09:30–16:00 ET Mon–Fri
New entries: 09:30 until flatten
Flatten all paper trades: 15:58 ET Mon–Fri
"""
from __future__ import annotations
import datetime
from typing import Optional

from utils import now_et

SCAN_START_HHMM = 930    # 9:30 AM ET
SCAN_END_HHMM = 1600     # 4:00 PM ET
FLAT_HHMM = 1558         # 3:58 PM ET — flatten everything
ENTRY_CUTOFF_HHMM = 1558 # no new entries at flatten time


def hhmm(dt: datetime.datetime) -> int:
    return dt.hour * 100 + dt.minute


def is_weekday(dt: Optional[datetime.datetime] = None) -> bool:
    dt = dt or now_et()
    return dt.weekday() < 5  # Mon–Fri


def in_scan_window(dt: Optional[datetime.datetime] = None) -> bool:
    """Scanners may look for trades."""
    dt = dt or now_et()
    return is_weekday(dt) and SCAN_START_HHMM <= hhmm(dt) < SCAN_END_HHMM


def in_entry_window(dt: Optional[datetime.datetime] = None) -> bool:
    """New paper entries allowed."""
    dt = dt or now_et()
    return is_weekday(dt) and SCAN_START_HHMM <= hhmm(dt) < ENTRY_CUTOFF_HHMM


def should_flatten(dt: Optional[datetime.datetime] = None) -> bool:
    """Flatten-all time has arrived for this weekday."""
    dt = dt or now_et()
    return is_weekday(dt) and hhmm(dt) >= FLAT_HHMM


def session_label(dt: Optional[datetime.datetime] = None) -> str:
    dt = dt or now_et()
    if not is_weekday(dt):
        return "WEEKEND"
    if in_scan_window(dt):
        return "REGULAR"
    hm = hhmm(dt)
    if hm < SCAN_START_HHMM:
        return "CLOSED"
    return "CLOSED"
