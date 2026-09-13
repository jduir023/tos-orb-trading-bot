"""
divergence_strategy.py  —  v2: Swing-timeframe redesign
Standard Deviation Channel divergence on 30-min candles.

Unlike the intraday ORB / First-Pullback strategies, divergence signals
often need 1-5 trading days to reach their target (the mean/middle band or
upper band).  Positions generated here are tagged "div_swing" so the engine
skips the EOD 3:55 PM flatten and carries them overnight.

Architecture:
  • Fetches multi-day 1-min candles, resamples to resample_minutes (default 30)
  • Detects classic RSI divergence + MACD histogram confirmation
  • Positions carry overnight — max_hold_days hard-cap prevents zombie trades

Stop-loss controls (all configurable):
  stop_buffer_pct   — % buffer below pivot low (breathing room)
  min_stop_pct      — minimum stop distance as % of entry  (floor)
  max_stop_pct      — maximum stop distance as % of entry  (safety ceiling)
  risk_per_trade_pct— % of account to risk per trade (dollar risk)
"""

import time
import datetime
import concurrent.futures
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from orb_strategy import TradeSignal
from utils import log_message, now_et as _now_et
from pillars import pillar, apply as apply_pillars, pack as pack_pillars, failed_names


class DivergenceStrategy:

    def __init__(self) -> None:
        self.enabled: bool = False

        # Timeframe
        self.resample_minutes: int   = 30
        self.lookback_days:    int   = 10

        # Bollinger / StdDev Channel
        self.bb_period:   int   = 21
        self.bb_std_mult: float = 1.0

        # Divergence detection
        self.rsi_period:     int   = 14
        self.div_lookback:   int   = 60
        self.pivot_bars:     int   = 3
        self.min_rsi_div:    float = 2.0
        self.max_rsi_entry:  float = 45.0
        self.band_proximity: float = 0.15
        self.use_macd_confirm: bool = True

        # Stop-loss controls
        self.stop_buffer_pct: float = 0.5   # % cushion below pivot low
        self.min_stop_pct:    float = 0.5   # floor: stop at least this % from entry
        self.max_stop_pct:    float = 8.0   # ceiling: stop at most this % from entry

        # Risk / sizing
        self.account_size:       float = 7111.00
        self.risk_per_trade_pct: float = 1.0

        # Swing settings
        self.is_swing:           bool = True
        self.premarket_start_hhmm: int = 400   # 04:00 ET — div entries allowed from here
        self.entry_cutoff_hhmm:  int  = 1530
        self.max_hold_days:      int  = 5
        self.allow_short:        bool = False

        # State
        self._triggered:    Dict[str, bool]  = {}
        self._trade_date:   str              = ""
        self._candle_cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def set_config(
        self,
        enabled:            bool  = False,
        account_size:       float = 7111.00,
        risk_pct:           float = 1.0,
        resample_minutes:   int   = 30,
        lookback_days:      int   = 10,
        bb_period:          int   = 21,
        bb_std_mult:        float = 1.0,
        rsi_period:         int   = 14,
        div_lookback:       int   = 60,
        pivot_bars:         int   = 3,
        min_rsi_div:        float = 2.0,
        max_rsi_entry:      float = 45.0,
        band_proximity:     float = 0.15,
        use_macd_confirm:   bool  = True,
        stop_buffer_pct:    float = 0.5,
        min_stop_pct:       float = 0.5,
        max_stop_pct:       float = 8.0,
        entry_cutoff_hhmm:     int   = 1530,
        premarket_start_hhmm:  int   = 400,
        max_hold_days:         int   = 5,
        allow_short:           bool  = False,
    ) -> None:
        self.enabled            = enabled
        self.account_size       = account_size
        self.risk_per_trade_pct = risk_pct
        self.resample_minutes   = resample_minutes
        self.lookback_days      = lookback_days
        self.bb_period          = bb_period
        self.bb_std_mult        = bb_std_mult
        self.rsi_period         = rsi_period
        self.div_lookback       = div_lookback
        self.pivot_bars         = pivot_bars
        self.min_rsi_div        = min_rsi_div
        self.max_rsi_entry      = max_rsi_entry
        self.band_proximity     = band_proximity
        self.use_macd_confirm   = use_macd_confirm
        self.stop_buffer_pct    = stop_buffer_pct
        self.min_stop_pct       = min_stop_pct
        self.max_stop_pct       = max_stop_pct
        self.entry_cutoff_hhmm     = entry_cutoff_hhmm
        self.premarket_start_hhmm  = premarket_start_hhmm
        self.max_hold_days         = max_hold_days
        self.allow_short           = allow_short

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def reset_day(self) -> None:
        today = _now_et().date().isoformat()
        if self._trade_date != today:
            self._trade_date = today
            self._triggered  = {}

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calc_position_size(self, entry: float, stop: float) -> Tuple[int, float, float]:
        dist = abs(entry - stop)
        if dist < 0.0001:
            dist = 0.0001
        risk_dollars = self.account_size * (self.risk_per_trade_pct / 100)
        shares = max(1, int(risk_dollars / dist))
        if entry > 0:
            max_cost = self.account_size * 0.25
            shares   = min(shares, max(1, int(max_cost / entry)))
        return shares, round(shares * dist, 2), round(dist, 4)

    # ------------------------------------------------------------------
    # Candle helpers
    # ------------------------------------------------------------------

    @staticmethod
    def resample_candles(candles: List[dict], minutes: int) -> List[dict]:
        """Aggregate 1-min candles into N-min candles, clock-aligned."""
        if minutes <= 1:
            return candles
        from zoneinfo import ZoneInfo
        _et = ZoneInfo("America/New_York")

        def bucket(dt_ms: int) -> int:
            # Round timestamp DOWN to nearest N-minute interval.
            # Using absolute ms ensures uniqueness across different dates
            # (the old time-of-day approach collapsed all days to ~13 slots).
            interval_ms = minutes * 60 * 1000
            return (dt_ms // interval_ms) * interval_ms

        groups: Dict[int, list] = defaultdict(list)
        for c in candles:
            groups[bucket(c["datetime"])].append(c)

        result = []
        for key in sorted(groups.keys()):
            block = groups[key]
            if not block:
                continue
            result.append({
                "datetime": block[0]["datetime"],
                "open":   block[0]["open"],
                "high":   max(c["high"]           for c in block),
                "low":    min(c["low"]             for c in block),
                "close":  block[-1]["close"],
                "volume": sum(c.get("volume", 0)   for c in block),
            })
        return result

    def _fetch_candles(self, symbol: str, client) -> List[dict]:
        """Fetch multi-day 1-min candles and resample to resample_minutes."""
        now  = _now_et()  # respects sim clock — backtest and sim replays work correctly

        cached = self._candle_cache.get(symbol)
        if cached and time.time() - cached[0] < 300:
            return cached[1]

        start_ms = int((now - datetime.timedelta(days=self.lookback_days)).timestamp() * 1000)
        end_ms   = int(now.timestamp() * 1000)
        try:
            raw = client.get_price_history(
                symbol, frequency_type="minute", frequency=1,
                extended_hours=False, start_ms=start_ms, end_ms=end_ms,
            ) or []
        except Exception as e:
            log_message(f"[DIV] Candle fetch error for {symbol}: {e}")
            return []

        resampled = self.resample_candles(raw, self.resample_minutes)
        self._candle_cache[symbol] = (time.time(), resampled)
        return resampled

    # ------------------------------------------------------------------
    # Indicators
    # ------------------------------------------------------------------

    @staticmethod
    def compute_bb(closes, period, mult):
        if len(closes) < period:
            return None, None, None
        w   = closes[-period:]
        mid = sum(w) / period
        std = (sum((x - mid) ** 2 for x in w) / period) ** 0.5
        return round(mid + mult * std, 4), round(mid, 4), round(mid - mult * std, 4)

    @staticmethod
    def compute_rsi_series(closes, period=14):
        result = [None] * period
        if len(closes) < period + 1:
            return result
        ag = sum(max(closes[i]-closes[i-1],0) for i in range(1,period+1)) / period
        al = sum(max(closes[i-1]-closes[i],0) for i in range(1,period+1)) / period
        result.append(100.0 if al == 0 else round(100 - 100/(1+ag/al), 2))
        for i in range(period+1, len(closes)):
            d  = closes[i] - closes[i-1]
            ag = (ag*(period-1) + max(d,0))  / period
            al = (al*(period-1) + max(-d,0)) / period
            result.append(100.0 if al == 0 else round(100 - 100/(1+ag/al), 2))
        return result

    @staticmethod
    def compute_macd_hist_series(closes, fast=12, slow=26, sig=9):
        n = len(closes)
        if n < slow + sig:
            return [None] * n
        def ema_s(v, p):
            k, e = 2.0/(p+1), sum(v[:p])/p
            out = [e]
            for x in v[p:]: e = x*k + e*(1-k); out.append(e)
            return out
        fe, se = ema_s(closes,fast), ema_s(closes,slow)
        ml = [fe[slow-fast+i] - se[i] for i in range(len(se))]
        if len(ml) < sig:
            return [None] * n
        sl = ema_s(ml, sig)
        h  = [ml[sig-1+i] - sl[i] for i in range(len(sl))]
        return [None]*(n-len(h)) + h

    @staticmethod
    def find_pivot_lows(data, n):
        return [(i,data[i]) for i in range(n,len(data)-n)
                if all(data[i]<=data[i-j] for j in range(1,n+1))
                and all(data[i]<=data[i+j] for j in range(1,n+1))]

    @staticmethod
    def find_pivot_highs(data, n):
        return [(i,data[i]) for i in range(n,len(data)-n)
                if all(data[i]>=data[i-j] for j in range(1,n+1))
                and all(data[i]>=data[i+j] for j in range(1,n+1))]

    # ------------------------------------------------------------------
    # Divergence detection
    # ------------------------------------------------------------------

    def _detect_bull_div(self, candles, rsi_series):
        if len(candles) < self.pivot_bars*2+1 or len(rsi_series) != len(candles):
            return None
        lows   = [c["low"] for c in candles]
        pivots = self.find_pivot_lows(lows, self.pivot_bars)
        if len(pivots) < 2:
            return None
        (i1,p1),(i2,p2) = pivots[-2], pivots[-1]
        if p2 >= p1:
            return None
        r1, r2 = rsi_series[i1], rsi_series[i2]
        if r1 is None or r2 is None or r2 <= r1:
            return None
        strength = round(r2 - r1, 2)
        if strength < self.min_rsi_div:
            return None
        return {"p1":p1,"p2":p2,"r1":r1,"r2":r2,"strength":strength,"pivot_low":p2}

    def _detect_bear_div(self, candles, rsi_series):
        if len(candles) < self.pivot_bars*2+1 or len(rsi_series) != len(candles):
            return None
        highs  = [c["high"] for c in candles]
        pivots = self.find_pivot_highs(highs, self.pivot_bars)
        if len(pivots) < 2:
            return None
        (i1,p1),(i2,p2) = pivots[-2], pivots[-1]
        if p2 <= p1:
            return None
        r1, r2 = rsi_series[i1], rsi_series[i2]
        if r1 is None or r2 is None or r2 >= r1:
            return None
        strength = round(r1 - r2, 2)
        if strength < self.min_rsi_div:
            return None
        return {"p1":p1,"p2":p2,"r1":r1,"r2":r2,"strength":strength,"pivot_high":p2}

    # ------------------------------------------------------------------
    # Stop placement — three adjustable controls
    # ------------------------------------------------------------------

    def _calc_stop(self, entry: float, anchor: float, direction: str) -> float:
        """
        LONG:  stop = anchor × (1 - stop_buffer_pct%)
               clamped so it is at least min_stop_pct% below entry
               and at most max_stop_pct% below entry.

        SHORT: mirror logic above entry.
        """
        if direction == "LONG":
            raw   = anchor  * (1 - self.stop_buffer_pct / 100)
            floor = entry   * (1 - self.min_stop_pct    / 100)   # can't be tighter
            ceil_ = entry   * (1 - self.max_stop_pct    / 100)   # can't be wider
            stop  = min(raw, floor)   # start with raw; tighten to floor if raw is too tight
            stop  = max(stop, ceil_)  # then enforce the wide-stop ceiling
        else:
            raw   = anchor  * (1 + self.stop_buffer_pct / 100)
            floor = entry   * (1 + self.min_stop_pct    / 100)
            ceil_ = entry   * (1 + self.max_stop_pct    / 100)
            stop  = max(raw, floor)
            stop  = min(stop, ceil_)
        return round(stop, 4)

    # ------------------------------------------------------------------
    # Div candidate discovery (separate from gap/momentum scanner)
    # ------------------------------------------------------------------

    def _near_lower_band(self, price: float, upper: float, mid: float, lower: float) -> bool:
        """True when price is within band_proximity of the lower StdDev band."""
        band_range = upper - lower
        if band_range <= 0 or price <= 0:
            return False
        return price <= lower + band_range * self.band_proximity

    def _score_div_setup(self, symbol: str, price: float, candles: List[Dict]) -> Optional[Dict[str, Any]]:
        min_bars = self.bb_period + self.rsi_period + self.pivot_bars * 2 + 2
        if len(candles) < min_bars:
            return None
        window = candles[-self.div_lookback:] if len(candles) > self.div_lookback else candles
        closes = [c["close"] for c in window]
        upper, mid, lower = self.compute_bb(closes, self.bb_period, self.bb_std_mult)
        if upper is None:
            return None
        band_range = upper - lower
        if band_range <= 0:
            return None
        near = self._near_lower_band(price, upper, mid, lower)
        dist_pct = round((price - lower) / band_range * 100, 1)
        rsi_series = self.compute_rsi_series(closes, self.rsi_period)
        macd_series = self.compute_macd_hist_series(closes)
        div = self._detect_bull_div(window, rsi_series) if near else None
        cur_rsi = rsi_series[-1] if rsi_series else None
        rsi_ok = cur_rsi is None or cur_rsi <= self.max_rsi_entry
        macd_ok = True
        if self.use_macd_confirm and len(macd_series) >= 2:
            h1, h2 = macd_series[-2], macd_series[-1]
            macd_ok = not (h1 is not None and h2 is not None and h2 <= h1)
        reversal = window[-1]["close"] > window[-1]["open"]
        stop_ok = False
        if div:
            stop = self._calc_stop(price, div["pivot_low"], "LONG")
            dist = price - stop
            stop_pct = (dist / price * 100) if price else 0
            stop_ok = dist > 0 and self.min_stop_pct <= stop_pct <= self.max_stop_pct
        items = [
            pillar("band", "Near lower BB", near, f"{dist_pct}%"),
            pillar("div", "Bullish RSI divergence", div is not None),
            pillar("rsi", "RSI below entry max", bool(rsi_ok), f"{cur_rsi:.1f}" if cur_rsi is not None else ""),
            pillar("macd", "MACD rising", macd_ok),
            pillar("reversal", "Reversal bar", reversal),
            pillar("stop", "Stop distance valid", stop_ok),
        ]
        row = {
            "symbol":   symbol,
            "last":     round(price, 4),
            "price":    round(price, 4),
            "lower":    lower,
            "mid":      mid,
            "upper":    upper,
            "dist_pct": dist_pct,
            "rsi":      round(cur_rsi, 2) if cur_rsi is not None else None,
        }
        apply_pillars(row, items)
        return row

    def _analyze_div_candidate(self, symbol: str, data_client, min_price: float, max_price: float) -> Optional[Dict[str, Any]]:
        try:
            q = data_client.get_quote(symbol)
            if not q:
                return None
            quote = q.get("quote", {})
            price = float(quote.get("lastPrice", 0) or 0)
            if price < min_price:
                return None
            if max_price > 0 and price > max_price:
                return None

            candles = self._fetch_candles(symbol, data_client)
            row = self._score_div_setup(symbol, price, candles)
            if not row:
                return None
            if not row["ready"] and not row["watching"]:
                return None
            return row
        except Exception as e:
            log_message(f"[DIV-SCAN] Error analyzing {symbol}: {e}")
            return None

    def scan_candidates(
        self,
        data_client,
        watchlist: Optional[List[str]] = None,
        min_price: float = 1.0,
        max_price: float = 0.0,
        top_n: int = 20,
        max_workers: int = 8,
    ) -> List[Dict[str, Any]]:
        """Discover symbols near the lower BB — independent of gap-up momentum scan.

        Universe: user watchlist + volume-monitor list + movers (up AND down).
        Pre-filter: price near lower StdDev band on 30-min candles.
        Full divergence checks run later in evaluate().
        """
        from scanner import VOLUME_MONITOR_LIST

        universe: List[str] = []
        for src in (watchlist or [], VOLUME_MONITOR_LIST):
            for s in src:
                sym = str(s).upper().strip()
                if sym:
                    universe.append(sym)

        if hasattr(data_client, "get_movers"):
            for direction in ("up", "down"):
                try:
                    universe.extend(data_client.get_movers(direction=direction))
                except Exception as e:
                    log_message(f"[DIV-SCAN] get_movers({direction}) error: {e}")

        seen: set = set()
        symbols = [s for s in universe if not (s in seen or seen.add(s))]  # type: ignore

        if not symbols:
            log_message("[DIV-SCAN] No symbols in universe.")
            return []

        log_message(f"[DIV-SCAN] Scanning {len(symbols)} symbols for lower-band setups...")

        results: List[Dict[str, Any]] = []

        def _work(sym: str) -> Optional[Dict[str, Any]]:
            return self._analyze_div_candidate(sym, data_client, min_price, max_price)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            for fut in concurrent.futures.as_completed({pool.submit(_work, s): s for s in symbols}):
                res = fut.result()
                if res:
                    results.append(res)

        results.sort(key=lambda x: (int(not x.get("ready")), x.get("dist_pct", 999)))
        top = results[:top_n]
        if top:
            summary = ", ".join(f"{r['symbol']}({r['dist_pct']}%)" for r in top[:8])
            log_message(f"[DIV-SCAN] {len(top)} lower-band candidates | top: {summary}")
        else:
            log_message("[DIV-SCAN] 0 lower-band candidates this cycle.")
        return top


    def check_entry_criteria(
        self,
        symbol:         str,
        current_price:  float,
        data_client,
        open_positions: dict,
    ) -> Dict[str, Any]:
        """Return checklist criteria without placing a trade."""
        met: List[str] = []
        blocked: List[str] = []
        detail: Dict[str, Any] = {}
        checks: List[Dict[str, Any]] = []

        def add(label: str, passed: bool, group: str = "div") -> None:
            checks.append({"label": label, "pass": passed, "group": group})
            (met if passed else blocked).append(label)

        add("Div strategy enabled", self.enabled)
        if not self.enabled:
            return {
                "met": met, "blocked": blocked, "checks": checks,
                "ready": False, "price": current_price, "detail": detail,
            }

        self.reset_day()

        add("Not triggered today", not self._triggered.get(symbol))
        add("No open position", symbol not in open_positions)

        now = _now_et()
        hm = now.hour * 100 + now.minute
        in_window = self.premarket_start_hhmm <= hm < self.entry_cutoff_hhmm
        add("Within div trading window", in_window)

        if current_price <= 0:
            try:
                q = data_client.get_quote(symbol)
                if q:
                    current_price = float(q.get("quote", {}).get("lastPrice", 0) or 0)
            except Exception:
                pass
        if current_price <= 0:
            add("Valid live price", False)
            return {"met": met, "blocked": blocked, "checks": checks, "ready": False, "price": 0, "detail": detail}
        add("Valid live price", True)

        detail["price"] = round(current_price, 4)
        tf = f"{self.resample_minutes}m"

        candles = self._fetch_candles(symbol, data_client)
        min_bars = self.bb_period + self.rsi_period + self.pivot_bars * 2 + 2
        has_bars = len(candles) >= min_bars
        add(f"Sufficient {tf} history ({min_bars}+ bars)", has_bars)
        if not has_bars:
            return {"met": met, "blocked": blocked, "checks": checks, "ready": False, "price": current_price, "detail": detail}

        window = candles[-self.div_lookback:] if len(candles) > self.div_lookback else candles
        closes = [c["close"] for c in window]
        upper, mid, lower = self.compute_bb(closes, self.bb_period, self.bb_std_mult)
        if upper is None:
            add("Bollinger bands available", False)
            return {"met": met, "blocked": blocked, "checks": checks, "ready": False, "price": current_price, "detail": detail}

        band_range = upper - lower
        if band_range <= 0:
            add("Valid band range", False)
            return {"met": met, "blocked": blocked, "checks": checks, "ready": False, "price": current_price, "detail": detail}

        detail.update({
            "lower": round(lower, 4),
            "mid": round(mid, 4),
            "upper": round(upper, 4),
            "dist_pct": round((current_price - lower) / band_range * 100, 1) if band_range > 0 else 0,
        })

        near_lower = current_price <= lower + band_range * self.band_proximity
        add(f"Near lower Bollinger Band ({detail['dist_pct']}% from band)", near_lower)

        rsi_series = self.compute_rsi_series(closes, self.rsi_period)
        macd_series = self.compute_macd_hist_series(closes)

        div = self._detect_bull_div(window, rsi_series) if near_lower else None
        add("Bullish RSI divergence", div is not None)
        if div:
            cur_rsi = rsi_series[-1]
            rsi_ok = cur_rsi is None or cur_rsi <= self.max_rsi_entry
            add(f"RSI below max entry ({self.max_rsi_entry})", rsi_ok)
            if self.use_macd_confirm and len(macd_series) >= 2:
                h1, h2 = macd_series[-2], macd_series[-1]
                macd_ok = not (h1 is not None and h2 is not None and h2 <= h1)
                add("MACD histogram rising", macd_ok)
            bar_green = window[-1]["close"] > window[-1]["open"]
            add(f"Last {tf} bar green", bar_green)

        ready = bool(checks) and all(c["pass"] for c in checks)
        return {
            "met": met,
            "blocked": blocked,
            "checks": checks,
            "ready": ready,
            "price": current_price,
            "detail": detail,
        }


    def evaluate(
        self,
        symbol:         str,
        indicators:     dict,
        open_positions: dict,
        current_price:  float,
        data_client=None,
    ) -> Optional[TradeSignal]:
        if not self.enabled or data_client is None:
            return None

        self.reset_day()

        if self._triggered.get(symbol) or symbol in open_positions:
            return None

        now = _now_et()
        hm  = now.hour * 100 + now.minute
        if hm < self.premarket_start_hhmm or hm >= self.entry_cutoff_hhmm:
            return None

        candles  = self._fetch_candles(symbol, data_client)
        min_bars = self.bb_period + self.rsi_period + self.pivot_bars * 2 + 2
        if len(candles) < min_bars:
            log_message(
                f"[DIV] {symbol}: only {len(candles)} {self.resample_minutes}m bars "
                f"(need {min_bars}) — divergence requires more history"
            )
            return None

        scored = self._score_div_setup(symbol, current_price, candles)
        if not scored or not scored.get("ready"):
            misses = failed_names(scored)
            log_message(
                f"[DIV] {symbol} skip — {scored.get('passed', 0) if scored else 0}/6 pillars "
                f"({scored.get('score_pct', 0) if scored else 0}%). Missing: {misses}"
            )
            return None

        window = candles[-self.div_lookback:] if len(candles) > self.div_lookback else candles
        closes = [c["close"] for c in window]
        upper, mid, lower = scored["upper"], scored["mid"], scored["lower"]
        rsi_series  = self.compute_rsi_series(closes, self.rsi_period)
        macd_series = self.compute_macd_hist_series(closes)
        div = self._detect_bull_div(window, rsi_series)
        tf          = f"{self.resample_minutes}m"

        if True:
            if div:
                entry = current_price
                stop  = self._calc_stop(entry, div["pivot_low"], "LONG")
                dist  = entry - stop
                if dist <= 0:
                    return None

                shares, risk_dollars, _ = self.calc_position_size(entry, stop)
                target_r2 = round(mid,   4) if mid   > entry else round(entry + dist*2, 4)
                target_r3 = round(upper, 4) if upper > entry else round(entry + dist*3, 4)
                stop_pct  = round((entry - stop) / entry * 100, 2)

                reason = (
                    f"div_swing LONG [{tf} BB{self.bb_period}] "
                    f"pillars={scored['passed']}/{scored['total']} ({scored['score_pct']}%) | "
                    f"lower={lower:.4f} mid={mid:.4f} upper={upper:.4f} | "
                    f"RSI +{div['strength']:.1f}pts | "
                    f"stop={stop:.4f} ({stop_pct}%)"
                )
                log_message(
                    f"[DIV] SIGNAL LONG {symbol} @ {entry:.4f} "
                    f"stop={stop:.4f}({stop_pct}%) "
                    f"mid={target_r2:.4f} upper={target_r3:.4f} "
                    f"risk=${risk_dollars:.2f}"
                )
                self._triggered[symbol] = True
                return TradeSignal(
                    symbol=symbol, direction="LONG",
                    entry_price=entry, stop_price=stop,
                    target_r2=target_r2, target_r3=target_r3,
                    shares=shares, risk_dollars=risk_dollars,
                    orb_high=upper, orb_low=lower,
                    vwap=indicators.get("vwap"),
                    rel_vol=float(indicators.get("breakout_vol_ratio") or 0),
                    gap_pct=0.0, reason=reason,
                )

        # ── SHORT ──────────────────────────────────────────────────────
        band_range = (upper or 0) - (lower or 0)
        if self.allow_short and band_range > 0 and current_price >= upper - band_range * self.band_proximity:
            div = self._detect_bear_div(window, rsi_series)
            if div:
                cur_rsi = rsi_series[-1]
                if cur_rsi is not None and cur_rsi < (100 - self.max_rsi_entry):
                    return None
                if self.use_macd_confirm and len(macd_series) >= 2:
                    h1, h2 = macd_series[-2], macd_series[-1]
                    if h1 is not None and h2 is not None and h2 >= h1:
                        return None
                if window[-1]["close"] >= window[-1]["open"]:
                    return None

                entry = current_price
                stop  = self._calc_stop(entry, div["pivot_high"], "SHORT")
                dist  = stop - entry
                if dist <= 0:
                    return None

                shares, risk_dollars, _ = self.calc_position_size(entry, stop)
                target_r2 = round(mid,   4) if mid   < entry else round(entry - dist*2, 4)
                target_r3 = round(lower, 4) if lower < entry else round(entry - dist*3, 4)

                reason = (
                    f"div_swing SHORT [{tf} BB{self.bb_period}] "
                    f"RSI -{div['strength']:.1f}pts | "
                    f"stop={stop:.4f} ({round((stop-entry)/entry*100,2)}%)"
                )
                log_message(f"[DIV] SIGNAL SHORT {symbol} @ {entry:.4f} stop={stop:.4f}")
                self._triggered[symbol] = True
                return TradeSignal(
                    symbol=symbol, direction="SHORT",
                    entry_price=entry, stop_price=stop,
                    target_r2=target_r2, target_r3=target_r3,
                    shares=shares, risk_dollars=risk_dollars,
                    orb_high=upper, orb_low=lower,
                    vwap=indicators.get("vwap"),
                    rel_vol=0.0, gap_pct=0.0, reason=reason,
                )

        return None
