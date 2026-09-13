"""
Live support / resistance from session quotes + 1-min pivots.
Updated as often as candles/quotes arrive so scanner rows stay current.
"""
from __future__ import annotations
import time
from typing import Any, Dict, List, Optional

from utils import log_message, now_et


class SREngine:
    def __init__(self, ttl_sec: float = 20.0) -> None:
        self.ttl = float(ttl_sec)
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._cache.get((symbol or "").upper())

    def from_quote(self, symbol: str, quote: Dict[str, Any]) -> Dict[str, Any]:
        q = quote.get("quote") or quote
        last = float(q.get("lastPrice") or q.get("mark") or 0 or 0)
        day_high = float(q.get("highPrice") or q.get("high52") or last or 0)
        day_low = float(q.get("lowPrice") or 0)
        open_px = float(q.get("openPrice") or 0)
        if day_low <= 0:
            day_low = min(last, open_px) if last else open_px
        if day_high <= 0:
            day_high = max(last, open_px) if last else last
        row = {
            "symbol": symbol.upper(),
            "price": round(last, 4) if last else None,
            "support": round(day_low, 4) if day_low else None,
            "resistance": round(day_high, 4) if day_high else None,
            "support2": round(open_px, 4) if open_px else None,
            "resistance2": None,
            "day_high": round(day_high, 4) if day_high else None,
            "day_low": round(day_low, 4) if day_low else None,
            "vwap": None,
            "source": "quote",
            "updated": time.time(),
        }
        prev = self._cache.get(symbol.upper()) or {}
        if prev.get("source") == "pivots" and time.time() - float(prev.get("updated") or 0) < self.ttl * 3:
            # Keep pivot S/R, refresh day high/low from the quote.
            prev["day_high"] = row["day_high"]
            prev["day_low"] = row["day_low"]
            prev["price"] = row["price"]
            if row["day_high"] and (not prev.get("resistance") or row["day_high"] > prev["resistance"]):
                prev["resistance"] = row["day_high"]
            if row["day_low"] and (not prev.get("support") or row["day_low"] < prev["support"]):
                prev["support"] = row["day_low"]
            self._cache[symbol.upper()] = prev
            return prev
        self._cache[symbol.upper()] = row
        return row

    def from_candles(self, symbol: str, candles: List[Dict], price: float = 0.0) -> Optional[Dict[str, Any]]:
        if not candles:
            return self.get(symbol)
        resampled = _resample(candles, 5)
        bars = resampled if len(resampled) >= 12 else candles
        highs = [float(c.get("high") or 0) for c in bars]
        lows = [float(c.get("low") or 0) for c in bars]
        last = price or float(bars[-1].get("close") or 0)
        day_high = max(highs) if highs else last
        day_low = min(x for x in lows if x > 0) if any(x > 0 for x in lows) else last
        vwap = _vwap(candles)
        ph = _pivots(highs, "high", 3)
        pl = _pivots(lows, "low", 3)
        res_levels = sorted({round(p, 4) for p in ph if p >= last * 0.995}, reverse=False)
        sup_levels = sorted({round(p, 4) for p in pl if p <= last * 1.005}, reverse=True)
        resistance = res_levels[0] if res_levels else round(day_high, 4)
        support = sup_levels[0] if sup_levels else round(day_low, 4)
        resistance2 = res_levels[1] if len(res_levels) > 1 else round(day_high, 4)
        support2 = sup_levels[1] if len(sup_levels) > 1 else round(day_low, 4)
        row = {
            "symbol": symbol.upper(),
            "price": round(last, 4),
            "support": support,
            "resistance": resistance,
            "support2": support2,
            "resistance2": resistance2,
            "day_high": round(day_high, 4),
            "day_low": round(day_low, 4),
            "vwap": round(vwap, 4) if vwap else None,
            "source": "pivots",
            "updated": time.time(),
            "clock": now_et().strftime("%I:%M %p").lstrip("0"),
        }
        self._cache[symbol.upper()] = row
        return row

    def refresh(self, client, symbols: List[str], candle_fn=None, max_n: int = 12) -> None:
        """Refresh pivot S/R for a few symbols; quotes should already have seeded cache."""
        now = time.time()
        todo = []
        for s in symbols:
            s = (s or "").upper()
            if not s:
                continue
            prev = self._cache.get(s)
            if prev and now - float(prev.get("updated") or 0) < self.ttl:
                continue
            todo.append(s)
            if len(todo) >= max_n:
                break
        for s in todo:
            try:
                candles = candle_fn(s) if candle_fn else None
                if not candles and hasattr(client, "get_price_history"):
                    from datetime import datetime, timedelta
                    from zoneinfo import ZoneInfo
                    et = ZoneInfo("America/New_York")
                    today = now_et().date()
                    start = int(datetime(today.year, today.month, today.day, 4, 0, tzinfo=et).timestamp() * 1000)
                    end = int(now_et().timestamp() * 1000)
                    candles = client.get_price_history(
                        s, frequency_type="minute", frequency=1,
                        extended_hours=True, start_ms=start, end_ms=end,
                    )
                if candles:
                    self.from_candles(s, candles)
            except Exception as exc:
                log_message(f"[SR] {s}: {exc}")

    def attach(self, row: Dict[str, Any]) -> Dict[str, Any]:
        sym = (row.get("symbol") or "").upper()
        lv = self.get(sym)
        if not lv:
            return row
        row["support"] = lv.get("support")
        row["resistance"] = lv.get("resistance")
        row["support2"] = lv.get("support2")
        row["resistance2"] = lv.get("resistance2")
        row["sr_vwap"] = lv.get("vwap")
        row["sr_source"] = lv.get("source")
        row["sr_updated"] = lv.get("clock") or lv.get("updated")
        return row

    def near_resistance(self, symbol: str, price: float, pct: float = 0.4) -> bool:
        lv = self.get(symbol)
        if not lv or not lv.get("resistance") or price <= 0:
            return False
        r = float(lv["resistance"])
        return abs(price - r) / r * 100 <= pct or price >= r

    def near_support(self, symbol: str, price: float, pct: float = 0.4) -> bool:
        lv = self.get(symbol)
        if not lv or not lv.get("support") or price <= 0:
            return False
        s = float(lv["support"])
        return abs(price - s) / s * 100 <= pct or price <= s


def _resample(candles: List[Dict], minutes: int) -> List[Dict]:
    if minutes <= 1 or not candles:
        return candles
    iv = minutes * 60 * 1000
    groups: Dict[int, list] = {}
    for c in candles:
        key = int(c.get("datetime") or 0) // iv * iv
        groups.setdefault(key, []).append(c)
    out = []
    for key in sorted(groups):
        b = groups[key]
        out.append({
            "datetime": b[0].get("datetime"),
            "open": b[0]["open"],
            "high": max(x["high"] for x in b),
            "low": min(x["low"] for x in b),
            "close": b[-1]["close"],
            "volume": sum(x.get("volume") or 0 for x in b),
        })
    return out


def _vwap(candles: List[Dict]) -> Optional[float]:
    tv = vol = 0.0
    for c in candles:
        v = float(c.get("volume") or 0)
        tp = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        tv += tp * v
        vol += v
    return tv / vol if vol else None


def _pivots(vals: List[float], kind: str, n: int) -> List[float]:
    out = []
    for i in range(n, len(vals) - n):
        w = vals[i - n: i + n + 1]
        if kind == "high" and vals[i] == max(w):
            out.append(vals[i])
        if kind == "low" and vals[i] == min(w) and vals[i] > 0:
            out.append(vals[i])
    return out
