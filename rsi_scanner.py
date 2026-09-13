"""
rsi_scanner.py
Dedicated scanner for the RSI Mean Reversion strategy.
Uses DAILY candles to score each symbol on 6 trigger pillars.
Trade fires only when 95% (6/6) are met.
"""
from __future__ import annotations
import concurrent.futures
import time
from typing import Any, Dict, List, Optional

from utils import log_message
from pillars import pillar, apply as apply_pillars


class RSIScanner:
    """
    Score each symbol on 6 daily-chart pillars:
      1. uptrend  — price > SMA20 AND SMA20 is rising over the past 10 bars
      2. volume   — 20-day average daily volume >= min_avg_vol
      3. macd     — MACD histogram >= -0.05 OR MACD line > signal (not deeply bearish)
      4. ema      — EMA9 >= EMA20 * 0.995 (at or near bullish cross)
      5. rsi      — RSI(14) <= rsi_entry threshold (20)
      6. turn     — last bar green OR RSI ticking up from prior bar

    Watching: 4+/6 pillars. Trade: 95% = 6/6.
    """

    MIN_BARS = 40  # 26 slow EMA + 9 signal EMA + buffer

    def __init__(self, client) -> None:
        self.client       = client
        self.min_avg_vol: int   = 300_000
        self.min_price:   float = 2.0
        self.max_price:   float = 500.0
        self.rsi_watch:   float = 40.0
        self.rsi_entry:   float = 20.0
        self.sma_period:  int   = 20
        self.sma_lookback: int  = 10   # bars back to compare SMA for rising-trend check
        self.timeframe:   str   = "daily"
        # Seconds to cache candles per timeframe — shorter for intraday
        self._CACHE_TTL = {
            "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
            "1h": 3600, "4h": 7200, "daily": 3600, "weekly": 86400,
        }
        self._cache: Dict[str, tuple] = {}
        self._cache_ttl: float = 3600.0  # fallback

    def scan(self, symbols: List[str], max_workers: int = 8) -> List[Dict[str, Any]]:
        if not symbols:
            return []
        results: List[Dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._analyze, sym): sym for sym in symbols}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    res = fut.result()
                    if res:
                        results.append(res)
                except Exception as exc:
                    log_message(f"[RSI-SCAN] future error: {exc}")
        results.sort(key=lambda x: x.get("rsi", 100))
        log_message(f"[RSI-SCAN] {len(results)} candidates from {len(symbols)} symbols")
        return results

    def _analyze(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            candles = self._fetch_daily(symbol)
            if not candles or len(candles) < self.MIN_BARS:
                return None

            closes  = [c["close"]            for c in candles]
            volumes = [c.get("volume", 0)    for c in candles]
            last    = closes[-1]

            if last < self.min_price or last > self.max_price:
                return None

            rsi = self._rsi(closes, 14)
            rsi_prev = self._rsi(closes[:-1], 14) if len(closes) > 16 else None
            if rsi is None or rsi > self.rsi_watch:
                return None

            sma20     = self._sma(closes, self.sma_period)
            sma20_old = self._sma(closes[:-self.sma_lookback], self.sma_period)
            ema9      = self._ema(closes, 9)
            ema20     = self._ema(closes, 20)
            macd_d    = self._macd(closes)
            avg_vol   = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0

            uptrend = bool(
                sma20 and sma20_old and
                last > sma20 and
                sma20 > sma20_old
            )
            vol_ok  = avg_vol >= self.min_avg_vol
            macd_ok = bool(
                macd_d and (
                    macd_d["hist"] >= -0.05 or
                    macd_d["macd"] > macd_d["signal"]
                )
            )
            ema_ok  = bool(ema9 and ema20 and ema9 >= ema20 * 0.995)
            rsi_ok  = rsi <= self.rsi_entry
            last_green = candles[-1]["close"] > candles[-1]["open"]
            rsi_rising = rsi_prev is not None and rsi > rsi_prev
            turn_ok = last_green or rsi_rising

            items = [
                pillar("uptrend", "2-week uptrend", uptrend),
                pillar("volume", "Average volume", vol_ok, f"{int(avg_vol):,}"),
                pillar("macd", "MACD not bearish", macd_ok),
                pillar("ema", "EMA9 / EMA20", ema_ok),
                pillar("rsi", "RSI oversold", rsi_ok, f"{rsi:.1f}"),
                pillar("turn", "Turn / bounce start", turn_ok),
            ]
            row = {
                "symbol":       symbol,
                "last":         round(last, 4),
                "price":        round(last, 4),
                "rsi":          round(rsi, 2),
                "sma20":        round(sma20, 4)           if sma20    else None,
                "ema9":         round(ema9, 4)            if ema9     else None,
                "ema20":        round(ema20, 4)           if ema20    else None,
                "macd_hist":    round(macd_d["hist"], 5)  if macd_d   else None,
                "avg_vol":      int(avg_vol),
            }
            apply_pillars(row, items)
            if not row["ready"] and not row["watching"]:
                return None
            return row
        except Exception as exc:
            log_message(f"[RSI-SCAN] Error on {symbol}: {exc}")
            return None

    # Lookback days of minute data needed for each intraday timeframe
    _INTRADAY_DAYS = {"5m": 6, "10m": 12, "15m": 18, "30m": 28, "1h": 50, "4h": 90}
    _INTRADAY_MIN  = {"5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "4h": 240}

    @staticmethod
    def _resample(candles: List[Dict], minutes: int) -> List[Dict]:
        if minutes <= 1 or not candles:
            return candles
        from collections import defaultdict
        iv = minutes * 60 * 1000
        groups: dict = defaultdict(list)
        for c in candles:
            groups[(c["datetime"] // iv) * iv].append(c)
        result = []
        for key in sorted(groups):
            b = groups[key]
            result.append({"datetime": b[0]["datetime"], "open": b[0]["open"],
                           "high": max(x["high"] for x in b), "low": min(x["low"] for x in b),
                           "close": b[-1]["close"], "volume": sum(x.get("volume", 0) for x in b)})
        return result

    def _fetch_daily(self, symbol: str) -> List[Dict]:
        now_ts = time.time()
        ttl    = self._CACHE_TTL.get(self.timeframe, self._cache_ttl)
        cache_key = f"{symbol}:{self.timeframe}"
        cached = self._cache.get(cache_key)
        if cached and now_ts - cached[0] < ttl:
            return cached[1]
        if self.timeframe in self._INTRADAY_DAYS:
            import datetime as _dt
            from zoneinfo import ZoneInfo
            _et = ZoneInfo("America/New_York")
            days = self._INTRADAY_DAYS[self.timeframe]
            mins = self._INTRADAY_MIN[self.timeframe]
            now_et = _dt.datetime.now(tz=_et)
            start_ms = int((now_et - _dt.timedelta(days=days)).timestamp() * 1000)
            end_ms   = int(now_et.timestamp() * 1000)
            raw = self.client.get_price_history(
                symbol, frequency_type="minute", frequency=1,
                extended_hours=False, start_ms=start_ms, end_ms=end_ms,
            ) or []
            candles = self._resample(raw, mins)
        elif self.timeframe == "weekly":
            candles = self.client.get_price_history(
                symbol, period_type="year", period=2,
                frequency_type="weekly", frequency=1,
            ) or []
        else:  # daily
            candles = self.client.get_price_history(
                symbol, period_type="month", period=3,
                frequency_type="daily", frequency=1,
            ) or []
        if candles:
            self._cache[cache_key] = (now_ts, candles)
        return candles

    def get_cached_rsi(self, symbol: str) -> Optional[float]:
        cache_key = f"{symbol}:{self.timeframe}"
        cached = self._cache.get(cache_key)
        if not cached:
            return None
        candles = cached[1]
        closes  = [c["close"] for c in candles]
        return self._rsi(closes, 14)

    @staticmethod
    def _sma(closes: List[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        return sum(closes[-period:]) / period

    @staticmethod
    def _ema(closes: List[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        k   = 2.0 / (period + 1)
        ema = sum(closes[:period]) / period
        for v in closes[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    @staticmethod
    def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        gains = losses = 0.0
        for i in range(1, period + 1):
            d = closes[i] - closes[i - 1]
            if d > 0:
                gains += d
            else:
                losses -= d
        ag, al = gains / period, losses / period
        for i in range(period + 1, len(closes)):
            d  = closes[i] - closes[i - 1]
            ag = (ag * (period - 1) + max(d, 0))  / period
            al = (al * (period - 1) + max(-d, 0)) / period
        if al == 0:
            return 100.0
        return round(100 - 100 / (1 + ag / al), 2)

    @staticmethod
    def _macd(closes: List[float], fast: int = 12, slow: int = 26, sig: int = 9) -> Optional[Dict]:
        if len(closes) < slow + sig:
            return None
        def _ema_s(v: List[float], p: int) -> List[float]:
            k = 2.0 / (p + 1)
            e = sum(v[:p]) / p
            out = [e]
            for x in v[p:]:
                e = x * k + e * (1 - k)
                out.append(e)
            return out
        fe = _ema_s(closes, fast)
        se = _ema_s(closes, slow)
        ml = [fe[slow - fast + i] - se[i] for i in range(len(se))]
        if len(ml) < sig:
            return None
        sl = _ema_s(ml, sig)
        return {"macd": ml[-1], "signal": sl[-1], "hist": ml[-1] - sl[-1]}
