"""
analytics.py
Trade performance analytics for the TOS ORB Trading Bot.

Pure functions — given a list of closed-position dicts (vars(OpenPosition)),
compute win rate, profit factor, expectancy, average R, drawdown, and the same
metrics sliced by setup type, time-of-day, and gap-size bucket.

These analytics are the foundation for data-driven strategy tuning: they let you
measure which setups / filters / sessions actually carry the edge instead of
guessing.
"""

from __future__ import annotations

import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Reasons that represent a genuine exit (a completed round-trip trade).
# Anything still "open" / "pending" is excluded from performance stats.
_CLOSED_STATUSES = {"target", "stop", "manual", "trail", "eod_flat",
                    "kill_switch", "broker_exit", "closed", "stopped"}


# ──────────────────────────────────────────────────────────────────────────────
# Classification helpers
# ──────────────────────────────────────────────────────────────────────────────

def classify_setup(reason: str) -> str:
    """Map a trade's entry reason to a coarse setup bucket."""
    r = (reason or "").lower()
    if "first_pullback" in r:
        return "first_pullback"
    if "orb" in r:
        return "orb"
    if "swing" in r:
        return "swing"
    if "manual" in r:
        return "manual"
    return "other"


def hour_et(timestamp: Optional[float]) -> Optional[int]:
    """Convert a unix timestamp to the ET hour-of-day (0-23), or None."""
    if not timestamp:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(timestamp), _ET).hour
    except (ValueError, OSError, OverflowError):
        return None


def gap_bucket(gap_pct: float) -> str:
    """Bucket a gap percentage for slicing."""
    g = abs(float(gap_pct or 0.0))
    if g < 5:
        return "<5%"
    if g < 10:
        return "5-10%"
    if g < 20:
        return "10-20%"
    if g < 50:
        return "20-50%"
    return "50%+"


def trade_r_multiple(trade: Dict) -> Optional[float]:
    """Realized R-multiple = total PnL / initial dollar risk (risk_dollars)."""
    risk = float(trade.get("risk_dollars", 0) or 0)
    if risk <= 0:
        return None
    return float(trade.get("pnl", 0) or 0) / risk


# ──────────────────────────────────────────────────────────────────────────────
# Core metric computation
# ──────────────────────────────────────────────────────────────────────────────

def _apply_cost_estimate(trades: List[Dict], slippage_bps: float = 50.0) -> float:
    """Estimate total round-trip costs (slippage + spread) in dollars.
    Default 50 bps (0.5%) per trade. Schwab charges $0 commission.
    """
    total = 0.0
    for t in trades:
        shares = int(t.get("shares", 0) or 0)
        entry  = float(t.get("entry_price", 0) or 0)
        if shares > 0 and entry > 0:
            total += shares * entry * (slippage_bps / 10_000.0)
    return round(total, 2)


def _basic_metrics(trades: List[Dict]) -> Dict:
    """Compute the core performance metrics for a list of closed trades."""
    n = len(trades)
    if n == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "win_rate": 0.0, "total_pnl": 0.0, "gross_profit": 0.0,
            "gross_loss": 0.0, "profit_factor": None,
            "avg_win": 0.0, "avg_loss": 0.0, "avg_r": None,
            "expectancy_r": None, "expectancy_usd": 0.0,
            "largest_win": 0.0, "largest_loss": 0.0,
            "max_drawdown": 0.0,
            "est_cost_usd": 0.0, "net_pnl": 0.0, "net_expectancy_usd": 0.0,
        }

    pnls = [float(t.get("pnl", 0) or 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakeven = [p for p in pnls if p == 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total_pnl = sum(pnls)

    win_rate = len(wins) / n
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0

    r_values = [r for r in (trade_r_multiple(t) for t in trades) if r is not None]
    avg_r = (sum(r_values) / len(r_values)) if r_values else None
    # Expectancy in R is simply the mean R per trade.
    expectancy_r = avg_r
    # Expectancy in dollars per trade.
    expectancy_usd = total_pnl / n

    # Max drawdown on the cumulative equity curve (in dollars).
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_r": round(avg_r, 3) if avg_r is not None else None,
        "expectancy_r": round(expectancy_r, 3) if expectancy_r is not None else None,
        "expectancy_usd": round(expectancy_usd, 2),
        "largest_win": round(max(pnls), 2),
        "largest_loss": round(min(pnls), 2),
        "max_drawdown": round(max_dd, 2),
        "est_cost_usd":       _apply_cost_estimate(trades),
        "net_pnl":            round(total_pnl - _apply_cost_estimate(trades), 2),
        "net_expectancy_usd": round((total_pnl - _apply_cost_estimate(trades)) / n, 2),
    }


def _slice(trades: List[Dict], key_fn) -> Dict[str, Dict]:
    """Group trades by key_fn(trade) and compute metrics per group."""
    groups: Dict[str, List[Dict]] = {}
    for t in trades:
        key = key_fn(t)
        if key is None:
            continue
        groups.setdefault(str(key), []).append(t)
    return {k: _basic_metrics(v) for k, v in groups.items()}


def compute_analytics(closed: List[Dict]) -> Dict:
    """
    Full analytics report over a list of closed-position dicts.

    Returns a dict with:
      - overall:   core metrics across all closed trades
      - by_setup:  metrics per setup type (orb / first_pullback / swing / manual)
      - by_hour:   metrics per ET entry hour
      - by_gap:    metrics per gap-size bucket
    Only genuine round-trip trades (a recognized exit status with a PnL) count.
    """
    trades = [
        t for t in (closed or [])
        if t.get("status") in _CLOSED_STATUSES and t.get("exit_price") is not None
    ]

    return {
        "overall": _basic_metrics(trades),
        "by_setup": _slice(trades, lambda t: classify_setup(t.get("entry_reason", ""))),
        "by_hour": _slice(trades, lambda t: hour_et(t.get("entry_time"))),
        "by_gap": _slice(trades, lambda t: gap_bucket(t.get("gap_pct", 0.0))),
    }
