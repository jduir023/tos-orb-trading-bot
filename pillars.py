"""
Shared trigger-pillar scoring.

Each scanner/strategy scores 5–6 named pillars. Scan results can show
WATCHING names that are close. A trade only fires when score_pct >= 95%.

  5 pillars → 5/5 (100%)
  6 pillars → 6/6 (100%)
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

TRIGGER_PCT = 95.0

# Display names + short labels used in the UI checklists.
SPECS: Dict[str, List[Dict[str, str]]] = {
    "gap": [
        {"id": "move", "name": "Move size"},
        {"id": "rel_vol", "name": "Relative volume"},
        {"id": "pace", "name": "Live volume pace"},
        {"id": "price", "name": "Price in range"},
        {"id": "float", "name": "Float / liquidity"},
        {"id": "spread", "name": "Tradeable spread"},
    ],
    "orb": [
        {"id": "breakout", "name": "ORB breakout"},
        {"id": "vwap", "name": "VWAP bias"},
        {"id": "trend", "name": "Trend (EMA/MACD/HTF)"},
        {"id": "volume", "name": "Breakout volume"},
        {"id": "range", "name": "Range / chase quality"},
        {"id": "timing", "name": "Pullback or momentum"},
    ],
    "rsi": [
        {"id": "uptrend", "name": "2-week uptrend"},
        {"id": "volume", "name": "Average volume"},
        {"id": "macd", "name": "MACD not bearish"},
        {"id": "ema", "name": "EMA9 / EMA20"},
        {"id": "rsi", "name": "RSI oversold"},
        {"id": "turn", "name": "Turn / bounce start"},
    ],
    "divergence": [
        {"id": "band", "name": "Near lower BB"},
        {"id": "div", "name": "Bullish RSI divergence"},
        {"id": "rsi", "name": "RSI below entry max"},
        {"id": "macd", "name": "MACD rising"},
        {"id": "reversal", "name": "Reversal bar"},
        {"id": "stop", "name": "Stop distance valid"},
    ],
    "swing": [
        {"id": "swings", "name": "3+ swing cycles"},
        {"id": "amplitude", "name": "Swing amplitude"},
        {"id": "volume", "name": "Average volume"},
        {"id": "range", "name": "Intraday range"},
        {"id": "support", "name": "At / near support"},
        {"id": "retrace", "name": "Retrace + bounce"},
    ],
    "scalp": [
        {"id": "uptrend", "name": "Daily uptrend"},
        {"id": "vwap", "name": "Above VWAP"},
        {"id": "ema", "name": "Bullish EMA"},
        {"id": "dip", "name": "Dip from high"},
        {"id": "support", "name": "At swing support"},
        {"id": "volume", "name": "Volume on dip"},
    ],
    "fp": [
        {"id": "pattern", "name": "Pole + flag"},
        {"id": "macd", "name": "MACD bullish"},
        {"id": "rsi", "name": "RSI not overbought"},
        {"id": "stop", "name": "Stop in band"},
        {"id": "window", "name": "Time window"},
        {"id": "retrace", "name": "Flag retrace"},
    ],
    "options": [
        {"id": "weekly", "name": "Wed/Fri weekly"},
        {"id": "spread", "name": "Tight ATM book"},
        {"id": "oi", "name": "Open interest"},
        {"id": "liquid", "name": "Liquid underlying"},
        {"id": "box", "name": "5-min box ready"},
        {"id": "confirm", "name": "5-min close through"},
    ],
    "opt_confirm": [
        {"id": "weekly", "name": "Wed/Fri weekly"},
        {"id": "spread", "name": "Tight ATM book"},
        {"id": "oi", "name": "Open interest"},
        {"id": "liquid", "name": "Liquid underlying"},
        {"id": "box", "name": "5-min box ready"},
        {"id": "confirm", "name": "5-min close through"},
    ],
}


def pillar(pid: str, name: str, passed: bool, detail: str = "") -> Dict[str, Any]:
    return {"id": pid, "name": name, "pass": bool(passed), "detail": detail or ""}


def pack(items: List[Dict[str, Any]], trigger_pct: float = TRIGGER_PCT) -> Dict[str, Any]:
    total = len(items)
    passed = sum(1 for p in items if p.get("pass"))
    pct = (passed / total * 100.0) if total else 0.0
    ready = pct + 1e-9 >= float(trigger_pct)
    watching = (not ready) and passed >= max(1, total - 2)
    return {
        "pillars": items,
        "passed": passed,
        "total": total,
        "score": passed,
        "score_pct": round(pct, 1),
        "ready": ready,
        "watching": watching,
        "trigger_pct": float(trigger_pct),
        "needed": total,  # 95% of 5 or 6 integer pillars == all of them
    }


def apply(row: Dict[str, Any], items: List[Dict[str, Any]], trigger_pct: float = TRIGGER_PCT) -> Dict[str, Any]:
    packed = pack(items, trigger_pct)
    row.update(packed)
    row["score_detail"] = {p["id"]: p["pass"] for p in items}
    return row


def failed_names(packed: Optional[Dict[str, Any]]) -> str:
    if not packed:
        return ""
    misses = [p["name"] for p in packed.get("pillars") or [] if not p.get("pass")]
    return ", ".join(misses)
