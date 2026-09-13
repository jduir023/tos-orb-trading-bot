"""
scalp_screener.py  v2
Independent dip-to-support scalp screener.
Finds stocks in an intraday uptrend that have pulled back near a support level.

Six trigger pillars; trade at 95% (6/6):
  1  Daily uptrend
  2  Price above VWAP
  3  EMA9 > EMA20
  4  Dip from session high
  5  At swing support
  6  Volume on the dip
"""
from __future__ import annotations
import concurrent.futures
import datetime
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from utils import log_message
from pillars import pillar, apply as apply_pillars

_ET = ZoneInfo("America/New_York")


class ScalpScreener:

    def __init__(self, client=None) -> None:
        self.client          = client
        self.min_price:      float = 1.0
        self.max_price:      float = 100.0
        self.min_avg_vol:    int   = 200_000
        self.min_dip_pct:    float = 1.0
        self.max_dip_pct:    float = 20.0
        self.proximity_pct:  float = 1.5
        self.min_score:      int   = 4   # watching threshold (4/6); trade still needs 6/6
        self.candle_minutes: int   = 5
        self._results:       List[Dict] = []
        self._last_ts:       float = 0.0
        self.at_support_pct: float = 0.8   # % above support still counts as "at" it
        self.uptrend_days:   int   = 2      # consecutive daily up-closes required

    # ------------------------------------------------------------------
    def _fetch_daily(self, symbol: str, n: int = 6) -> list:
        """Fetch the last `n` calendar days of daily OHLC for `symbol`."""
        try:
            end_ms   = int(datetime.datetime.now(tz=_ET).timestamp() * 1000)
            start_ms = int((datetime.datetime.now(tz=_ET)
                            - datetime.timedelta(days=n + 4)).timestamp() * 1000)
            return self.client.get_price_history(
                symbol, frequency_type="daily", frequency=1,
                extended_hours=False, start_ms=start_ms, end_ms=end_ms,
            ) or []
        except Exception:
            return []

    def _is_uptrending(self, symbol: str) -> bool:
        """Return True when the last `uptrend_days` daily closes each exceeded the one before.

        Missing daily data fails this pillar (no free pass).
        """
        try:
            candles = self._fetch_daily(symbol, n=self.uptrend_days + 4)
            closes  = [c["close"] for c in candles if c.get("close")]
            needed  = self.uptrend_days + 1   # need N+1 closes to check N consecutive rises
            if len(closes) < needed:
                return False
            tail = closes[-needed:]
            return all(tail[i] > tail[i - 1] for i in range(1, len(tail)))
        except Exception:
            return False

    def scan(self, symbols: List[str], max_workers: int = 8) -> List[Dict]:
        if not self.client or not symbols:
            return []
        seen: set = set()
        unique = [s for s in symbols if not (s in seen or seen.add(s))]  # type: ignore
        results: List[Dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._analyze, sym): sym for sym in unique}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    res = fut.result()
                    if res:
                        results.append(res)
                except Exception as exc:
                    log_message(f"[SCALP-SCREEN] {futures[fut]}: {exc}")
        results.sort(key=lambda x: (x["score"], -x["dip_pct"]), reverse=True)
        self._results = results
        self._last_ts = time.time()
        log_message(f"[SCALP-SCREEN] {len(results)} dip-to-support candidates from {len(unique)} symbols")
        return results

    def get_results(self) -> List[Dict]:
        return list(self._results)

    def _analyze(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            daily_up = self._is_uptrending(symbol)
            raw = self._fetch_today(symbol)
            if not raw or len(raw) < 20:
                return None
            candles = self._resample(raw, self.candle_minutes)
            if len(candles) < 10:
                return None
            closes  = [c["close"]         for c in candles]
            highs   = [c["high"]          for c in candles]
            lows    = [c["low"]           for c in candles]
            volumes = [c.get("volume", 0) for c in candles]
            price   = closes[-1]
            avg_vol = sum(volumes) / len(volumes) if volumes else 0
            if not (self.min_price <= price <= self.max_price):
                return None
            if avg_vol < self.min_avg_vol:
                return None
            vwap  = self._vwap(candles)
            ema9  = self._ema(closes, 9)
            ema20 = self._ema(closes, 20)
            session_high = max(highs)
            dip_pct = (session_high - price) / session_high * 100 if session_high > 0 else 0
            dipped = self.min_dip_pct <= dip_pct <= self.max_dip_pct
            pivot_low = self._find_pivot_low(lows)
            support   = pivot_low if pivot_low else ema20
            if support and support > 0:
                near_sup     = price <= support * (1 + self.proximity_pct / 100)
                sup_dist_pct = round((price - support) / support * 100, 2)
            else:
                near_sup     = False
                sup_dist_pct = None
            cur_vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
            at_support  = bool(
                support and support > 0
                and price <= support * (1 + self.at_support_pct / 100)
            )
            swing_low_touch = at_support and any(
                abs(l - support) / support * 100 <= 1.0 for l in lows[-4:]
            ) if support else False
            above_vwap  = bool(vwap  and price > vwap)
            bullish_ema = bool(ema9  and ema20 and ema9 > ema20)
            vol_ok      = cur_vol_ratio >= 0.4
            support_ok  = bool(at_support and swing_low_touch)
            items = [
                pillar("uptrend", "Daily uptrend", daily_up),
                pillar("vwap", "Above VWAP", above_vwap),
                pillar("ema", "Bullish EMA", bullish_ema),
                pillar("dip", "Dip from high", dipped, f"{dip_pct:.1f}%"),
                pillar("support", "At swing support", support_ok),
                pillar("volume", "Volume on dip", vol_ok, f"{cur_vol_ratio:.2f}x"),
            ]
            row = {
                "symbol":       symbol,
                "price":        round(price, 4),
                "vwap":         round(vwap, 4)    if vwap    else None,
                "ema9":         round(ema9, 4)    if ema9    else None,
                "ema20":        round(ema20, 4)   if ema20   else None,
                "session_high": round(session_high, 4),
                "dip_pct":      round(dip_pct, 2),
                "support":      round(support, 4) if support else None,
                "sup_dist_pct": sup_dist_pct,
                "near_support": near_sup,
                "avg_vol":      int(avg_vol),
                "direction":       "up",
                "at_support":      at_support,
                "swing_low_touch": swing_low_touch,
                "daily_uptrend":   daily_up,
            }
            apply_pillars(row, items)
            if not row["ready"] and not row["watching"]:
                return None
            return row
        except Exception as exc:
            log_message(f"[SCALP-SCREEN] Error on {symbol}: {exc}")
            return None

    def _fetch_today(self, symbol: str) -> List[Dict]:
        today_et = datetime.datetime.now(tz=_ET).date()
        start_ms = int(datetime.datetime(
            today_et.year, today_et.month, today_et.day, 4, 0, tzinfo=_ET
        ).timestamp() * 1000)
        end_ms = int(datetime.datetime.now(tz=_ET).timestamp() * 1000)
        return self.client.get_price_history(
            symbol, frequency_type="minute", frequency=1,
            extended_hours=False, start_ms=start_ms, end_ms=end_ms,
        ) or []

    @staticmethod
    def _resample(candles: List[Dict], minutes: int) -> List[Dict]:
        if minutes <= 1 or not candles:
            return candles
        iv = minutes * 60 * 1000
        groups: dict = defaultdict(list)
        for c in candles:
            groups[(c["datetime"] // iv) * iv].append(c)
        result = []
        for key in sorted(groups):
            b = groups[key]
            result.append({
                "datetime": b[0]["datetime"],
                "open":   b[0]["open"],
                "high":   max(x["high"] for x in b),
                "low":    min(x["low"]  for x in b),
                "close":  b[-1]["close"],
                "volume": sum(x.get("volume", 0) for x in b),
            })
        return result

    @staticmethod
    def _vwap(candles: List[Dict]) -> Optional[float]:
        tv  = sum((c["high"] + c["low"] + c["close"]) / 3 * c.get("volume", 0) for c in candles)
        vol = sum(c.get("volume", 0) for c in candles)
        return round(tv / vol, 4) if vol > 0 else None

    @staticmethod
    def _ema(closes: List[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        k = 2.0 / (period + 1)
        ema = sum(closes[:period]) / period
        for v in closes[period:]:
            ema = v * k + ema * (1 - k)
        return round(ema, 4)

    @staticmethod
    def _find_pivot_low(lows: List[float], n: int = 3, lookback: int = 30) -> Optional[float]:
        recent = lows[-lookback:] if len(lows) > lookback else lows
        if len(recent) < n * 2 + 1:
            return None
        for i in range(len(recent) - n - 1, n - 1, -1):
            window = recent[max(0, i - n): i + n + 1]
            if recent[i] <= min(window):
                return recent[i]
        return None
