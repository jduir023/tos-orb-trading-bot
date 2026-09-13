"""
5-minute box + confirmation on the UNDERLYING.

A 1-min poke is not an entry. Only a COMPLETED 5-min close through the lid/floor.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

from utils import now_et


def resample_5m(candles_1m: List[Dict], now_ms: Optional[int] = None) -> List[Dict]:
    """Build completed 5-min bars. Drops the in-progress bucket."""
    if not candles_1m:
        return []
    iv = 5 * 60 * 1000
    groups: Dict[int, list] = {}
    for c in candles_1m:
        ts = int(c.get("datetime") or 0)
        if ts <= 0:
            continue
        key = ts // iv * iv
        groups.setdefault(key, []).append(c)
    if not groups:
        return []
    keys = sorted(groups)
    # Drop last bucket if it is still forming (now inside the 5-min window)
    if now_ms is None:
        now_ms = int(now_et().timestamp() * 1000)
    if keys and now_ms < keys[-1] + iv:
        keys = keys[:-1]
    out = []
    for key in keys:
        b = groups[key]
        out.append({
            "datetime": key,
            "open": b[0]["open"],
            "high": max(x["high"] for x in b),
            "low": min(x["low"] for x in b),
            "close": b[-1]["close"],
            "volume": sum(x.get("volume") or 0 for x in b),
        })
    return out


def _pivots(bars: List[Dict], n: int = 2) -> tuple:
    highs, lows = [], []
    for i in range(n, len(bars) - n):
        h = bars[i]["high"]
        l = bars[i]["low"]
        if all(h >= bars[i + j]["high"] for j in range(-n, n + 1) if j):
            highs.append((i, h))
        if all(l <= bars[i + j]["low"] for j in range(-n, n + 1) if j):
            lows.append((i, l))
    return highs, lows


TARGET_FRAC = 0.75  # sell 3/4 of the way to the next level — full tag is rare


def take_profit_price(break_px: float, next_level: float, side: str, frac: float = TARGET_FRAC) -> float:
    """Price 3/4 of the way from the break to the next level."""
    try:
        brk = float(break_px)
        nxt = float(next_level)
        f = min(max(float(frac), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0
    kind = str(side or "").upper()
    if kind == "PUT":
        if nxt >= brk:
            return round(nxt, 4)
        return round(brk - (brk - nxt) * f, 4)
    if nxt <= brk:
        return round(nxt, 4)
    return round(brk + (nxt - brk) * f, 4)


class LevelEngine:
    def __init__(self) -> None:
        self._state: Dict[str, Dict[str, Any]] = {}  # last fired break per side
        self.anti_chase_pct = 0.4
        self.target_frac = TARGET_FRAC

    def evaluate(
        self,
        symbol: str,
        candles_1m: List[Dict],
        last: float,
        now_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        bars = resample_5m(candles_1m, now_ms=now_ms)
        if len(bars) < 6:
            return {
                "symbol": symbol,
                "ready": False,
                "skip": "need ~6 completed 5-min bars",
                "bars": len(bars),
                "last": last,
                "last_5m_close": None,
                "card": None,
            }
        session_high = max(b["high"] for b in bars)
        session_low = min(b["low"] for b in bars)
        ph, pl = _pivots(bars, 2)
        call_break = ph[-1][1] if ph else session_high
        put_break = pl[-1][1] if pl else session_low
        # coil lid/floor: last rejected high / defended low
        call_target = session_high
        put_target = session_low
        if len(ph) >= 2:
            call_target = max(ph[-1][1], ph[-2][1])
        if len(pl) >= 2:
            put_target = min(pl[-1][1], pl[-2][1])
        if call_target <= call_break:
            call_target = session_high if session_high > call_break else call_break
        if put_target >= put_break:
            put_target = session_low if session_low < put_break else put_break
        last_close = bars[-1]["close"]
        prior_close = bars[-2]["close"]
        call_next = call_target if call_target > call_break else call_break
        put_next = put_target if put_target < put_break else put_break
        call_sell = take_profit_price(call_break, call_next, "CALL", self.target_frac)
        put_sell = take_profit_price(put_break, put_next, "PUT", self.target_frac)
        card = (
            f"{symbol}\n\n"
            f"Calls on confirmation over {call_break:.2f}-{call_next:.2f} · sell {call_sell:.2f}\n\n"
            f"Puts on confirmation under {put_break:.2f}-{put_next:.2f} · sell {put_sell:.2f}"
        )
        # inside the box → no trade
        lo, hi = min(put_break, call_break), max(put_break, call_break)
        inside = lo <= last_close <= hi
        st = self._state.setdefault(symbol, {"call_break": None, "put_break": None, "failed_box": None})

        signal = None
        skip = None
        if inside:
            skip = "inside box"
        else:
            # CALL: first completed close through call_break
            if last_close > call_break and prior_close <= call_break:
                if st.get("call_break") == round(call_break, 4):
                    skip = "already used this call break"
                elif abs(last - call_break) / call_break * 100 > self.anti_chase_pct:
                    skip = f"chase {abs(last - call_break) / call_break * 100:.2f}% past break"
                    st["failed_box"] = ("CALL", round(call_break, 4))
                else:
                    nxt = call_target if call_target > call_break else last * 1.006
                    signal = {
                        "side": "CALL",
                        "break": call_break,
                        "level": nxt,
                        "target": take_profit_price(call_break, nxt, "CALL", self.target_frac),
                        "kill": put_break if put_break < call_break else prior_close,
                    }
            elif last_close < put_break and prior_close >= put_break:
                if st.get("put_break") == round(put_break, 4):
                    skip = "already used this put break"
                elif abs(last - put_break) / put_break * 100 > self.anti_chase_pct:
                    skip = f"chase {abs(last - put_break) / put_break * 100:.2f}% past break"
                    st["failed_box"] = ("PUT", round(put_break, 4))
                else:
                    nxt = put_target if put_target < put_break else last * 0.994
                    signal = {
                        "side": "PUT",
                        "break": put_break,
                        "level": nxt,
                        "target": take_profit_price(put_break, nxt, "PUT", self.target_frac),
                        "kill": call_break if call_break > put_break else prior_close,
                    }
            else:
                skip = "no first close through"

        return {
            "symbol": symbol,
            "ready": signal is not None,
            "skip": skip,
            "bars": len(bars),
            "last": last,
            "last_5m_close": last_close,
            "session_high": session_high,
            "session_low": session_low,
            "call_break": round(call_break, 4),
            "put_break": round(put_break, 4),
            "call_target": round(float(call_sell), 4),
            "put_target": round(float(put_sell), 4),
            "call_level": round(float(call_next), 4),
            "put_level": round(float(put_next), 4),
            "card": card,
            "signal": signal,
            "inside_box": inside,
        }

    def mark_used(self, symbol: str, side: str, break_px: float) -> None:
        """Consume a break only after a paper fill. Scan/display must not burn it."""
        st = self._state.setdefault(
            symbol, {"call_break": None, "put_break": None, "failed_box": None}
        )
        try:
            px = round(float(break_px), 4)
        except (TypeError, ValueError):
            return
        kind = str(side or "").upper()
        if kind == "CALL":
            st["call_break"] = px
        elif kind == "PUT":
            st["put_break"] = px

    def kill(
        self,
        symbol: str,
        candles_1m: List[Dict],
        pos: Dict[str, Any],
        now_ms: Optional[int] = None,
    ) -> Optional[str]:
        bars = resample_5m(candles_1m, now_ms=now_ms)
        if len(bars) < 2:
            return None
        close = bars[-1]["close"]
        side = (pos.get("side") or pos.get("type") or "").upper()
        kill = float(pos.get("kill") or 0)
        brk = float(pos.get("break") or 0)
        nxt = float(pos.get("level") or 0) or float(pos.get("target") or 0)
        target = take_profit_price(brk, nxt, side, self.target_frac) if brk and nxt else float(pos.get("target") or 0)
        if side == "CALL":
            if kill and close < kill:
                return f"5m close {close:.2f} under kill {kill:.2f}"
            if target and close >= target:
                return f"5m close {close:.2f} through 3/4 target {target:.2f}"
        elif side == "PUT":
            if kill and close > kill:
                return f"5m close {close:.2f} over kill {kill:.2f}"
            if target and close <= target:
                return f"5m close {close:.2f} through 3/4 target {target:.2f}"
        return None
