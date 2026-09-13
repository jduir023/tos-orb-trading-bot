"""
utils.py
Shared logging utility and trading-clock helpers.
"""

import datetime
import os
from typing import Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# ──────────────────────────────────────────────────────────────────────────────
# Sim clock override
# All time-gated strategy logic reads the clock through now_et() so that
# simulation mode respects the virtual trading day rather than the real clock.
# TradingEngine calls set_sim_now(dt) each loop iteration while in sim mode
# and set_sim_now(None) when sim stops.
# ──────────────────────────────────────────────────────────────────────────────

_sim_now: Optional[datetime.datetime] = None


def set_sim_now(dt: Optional[datetime.datetime]) -> None:
    """Override the trading clock. Pass None to revert to real time."""
    global _sim_now
    _sim_now = dt


def now_et() -> datetime.datetime:
    """Current time in US Eastern — or the active sim clock in simulation mode."""
    return _sim_now if _sim_now is not None else datetime.datetime.now(_ET)


def format_12h(value, with_date: bool = True, with_seconds: bool = False) -> str:
    """Format a timestamp in US Eastern 12-hour clock (not military time).

    Accepts datetime, unix seconds, or ISO strings. Empty/invalid → '—'.
    Examples: 'Aug 17, 2026 4:23 PM'  or  '4:23:08 PM'
    """
    if value is None or value == "":
        return "—"
    dt = None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return "—"
        try:
            if s.replace(".", "", 1).isdigit():
                dt = datetime.datetime.fromtimestamp(float(s), tz=datetime.timezone.utc)
            else:
                iso = s.replace("Z", "+00:00")
                # Schwab: 2026-07-09T19:18:08+0000
                if len(iso) >= 5 and (iso[-5] in "+-") and iso[-3] != ":":
                    iso = iso[:-2] + ":" + iso[-2:]
                dt = datetime.datetime.fromisoformat(iso)
        except Exception:
            return str(value)[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    dt = dt.astimezone(_ET)
    clock = dt.strftime("%I:%M:%S %p" if with_seconds else "%I:%M %p").lstrip("0")
    if not with_date:
        return clock
    return dt.strftime("%b %d, %Y ") + clock



LOG_FILE = os.path.join("saved_data", "bot.log")

# Size-based rotation: when bot.log exceeds the cap, it is rolled to bot.log.1
# (keeping a fixed number of historical files) to bound disk usage.
_MAX_LOG_BYTES   = 5 * 1024 * 1024   # 5 MB
_MAX_LOG_BACKUPS = 3                  # bot.log.1 .. bot.log.3


def _rotate_if_needed() -> None:
    try:
        if os.path.getsize(LOG_FILE) < _MAX_LOG_BYTES:
            return
    except OSError:
        return  # file missing — nothing to rotate

    # Drop the oldest, shift the rest up by one.
    oldest = f"{LOG_FILE}.{_MAX_LOG_BACKUPS}"
    if os.path.exists(oldest):
        try:
            os.remove(oldest)
        except OSError:
            pass
    for i in range(_MAX_LOG_BACKUPS - 1, 0, -1):
        src = f"{LOG_FILE}.{i}"
        dst = f"{LOG_FILE}.{i + 1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(LOG_FILE, f"{LOG_FILE}.1")
    except OSError:
        pass


def log_message(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %I:%M:%S %p").replace(" 0", " ")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        os.makedirs("saved_data", exist_ok=True)
        _rotate_if_needed()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
