"""
scanner.py
Pre-market gap + relative volume scanner.
Mirrors the signal-detection role of strategies.py in the crypto bot.
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from schwab_client import SchwabClient
from utils import log_message
from pillars import pillar, apply as apply_pillars


# Legacy default watchlist (kept for backward compatibility)
DEFAULT_WATCHLIST = [
    "AMC", "GME", "SNDL", "CLOV", "SPCE", "SOFI", "PLTR", "HOOD",
    "LCID", "RIVN", "NIO", "MVIS", "SQQQ", "TQQQ", "UVXY",
]

# Expanded intraday volume-monitor list — scanned every cycle for volume spikes.
# Covers high-beta, crypto-adjacent, EV/clean energy, AI/quantum, and
# leveraged ETFs.  Update this to add sectors you follow.
VOLUME_MONITOR_LIST = [
    # ── Small/mid-cap momentum (core targets for divergence) ──────────────
    "SOFI", "PLTR", "HOOD", "MVIS", "SNDL", "SPCE", "AMC", "GME",
    "CLOV", "SOUN", "BBAI", "IONQ", "QBTS", "RGTI", "ARQQ",
    # ── EV / clean energy ─────────────────────────────────────────────────
    "NIO", "RIVN", "LCID", "ACHR", "JOBY", "LILM", "BLNK", "CHPT",
    "WKHS", "GOEV", "IDRA",
    # ── Crypto / blockchain ───────────────────────────────────────────────
    "MARA", "RIOT", "CLSK", "BITF", "BTBT", "HUT", "CIFR",
    "MSTR", "COIN", "BITO", "IBIT",
    # ── AI / robotics / quantum ───────────────────────────────────────────
    "RKLB", "SATL", "BBAI", "SOUN", "AIOT", "QUBT", "ARQQ",
    # ── Biotech / high-vol small caps ─────────────────────────────────────
    "SNDL", "NVAX", "SRPT", "ARDX", "IMVT", "KRYS",
    # ── Leveraged ETFs (catch broad market vol spikes) ───────────────────
    "SQQQ", "TQQQ", "UVXY", "LABU", "SOXL", "SOXS", "FNGU", "FNGD",
    "TECL", "TECS", "TNA", "TZA", "FAS", "FAZ",
    # ── Sector leaders with frequent vol spikes ───────────────────────────
    "AMD", "NVDA", "TSLA", "NFLX", "META", "SNAP", "UBER", "LYFT",
    "ROKU", "DKNG", "PENN", "PLUG", "FCEL", "BE", "STEM",
]


class StockScanner:
    """
    Scans a watchlist for:
      - Pre-market gap >= min_gap_pct
      - Relative volume >= min_rel_vol
      - Price between min_price and max_price
      - Float < max_float (informational — sourced from fundamental data)

    Emits scan results via callback.
    """

    def __init__(
        self,
        client: SchwabClient,
        on_results: Optional[Callable[[List[Dict]], None]] = None,
    ) -> None:
        self.client     = client
        self.on_results = on_results
        self.watchlist:  List[str] = list(DEFAULT_WATCHLIST)

        # Scan config — apply_config() overrides these from data_handler defaults
        self.min_gap_pct:  float = 2.0    # lower for intraday pops (rel_vol is the real gate)
        self.min_rel_vol:  float = 3.0    # 3x minimum at all times
        self.vol_spike_threshold: float = 4.0   # >= 4x allows gap_pct as low as 1%
        self.min_realtime_rvol: float  = 2.0    # current vol rate must be >= 2x expected rate for this time of day
        self.min_price:    float = 1.00
        self.max_price:    float = 0.0      # 0 = no upper limit
        self.max_float:    float = 0.0      # 0 = no float limit
        self.avg_vol_bars: int   = 20
        self.scan_interval_sec: float = 60.0

        self._running   = False
        self._thread: Optional[threading.Thread] = None
        self._last_results: List[Dict] = []
        self._last_scan_ts: float = 0.0
        self._last_error: str = ""
        self._last_universe: int = 0
        self.should_scan = lambda: True  # engine sets this to the owning strategy's enabled flag

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True, name="scanner")
        self._thread.start()
        log_message("[SCANNER] Started.")

    def stop(self) -> None:
        self._running = False
        log_message("[SCANNER] Stopped.")

    def get_last_results(self) -> List[Dict]:
        return list(self._last_results)

    def set_config(
        self,
        min_gap_pct: float = 2.0,
        min_rel_vol: float = 3.0,
        min_price: float = 1.00,
        max_price: float = 0.0,
        max_float: float = 0.0,
        scan_interval_sec: float = 60.0,
        vol_spike_threshold: float = 4.0,
        min_realtime_rvol:   float = 2.0,
    ) -> None:
        self.min_gap_pct          = min_gap_pct
        self.min_rel_vol          = min_rel_vol
        self.min_price            = min_price
        self.max_price            = max_price
        self.max_float            = max_float
        self.scan_interval_sec    = scan_interval_sec
        self.vol_spike_threshold  = vol_spike_threshold
        self.min_realtime_rvol    = min_realtime_rvol

    def set_watchlist(self, symbols: List[str]) -> None:
        self.watchlist = [s.upper().strip() for s in symbols if s.strip()]

    # ------------------------------------------------------------------
    # Scan loop
    # ------------------------------------------------------------------

    def _scan_loop(self) -> None:
        from schedule import in_scan_window
        _idle_logged = False
        while self._running:
            try:
                armed = True
                try:
                    armed = bool(self.should_scan())
                except Exception:
                    armed = True
                if not armed or not in_scan_window():
                    if not _idle_logged:
                        why = "scanner off" if not armed else "outside weekday 9:30 AM–4:00 PM ET"
                        log_message(f"[SCANNER] Idle — {why}")
                        _idle_logged = True
                    time.sleep(min(30.0, self.scan_interval_sec))
                    continue
                _idle_logged = False
                results = self.run_scan()
                self._last_results = results
                self._last_scan_ts = time.time()
                self._last_error = ""
                self._last_universe = len(results) if results else 0
                if self.on_results:
                    self.on_results(results)
            except Exception as e:
                self._last_error = str(e)
                self._last_scan_ts = time.time()
                log_message(f"[SCANNER] Error in scan loop: {e}")
            time.sleep(self.scan_interval_sec)

    def run_scan(self) -> List[Dict]:
        """Discover top-10 gap-up and top-10 gap-down candidates from the market.

        Queries the broker's live movers endpoint for both directions, merges
        with any user-pinned watchlist symbols, applies quality filters, and
        returns up to 10 gap-up + 10 gap-down results sorted by magnitude.
        """
        up_symbols:   List[str] = []
        down_symbols: List[str] = []

        if hasattr(self.client, "get_movers"):
            try:
                up_symbols   = self.client.get_movers(direction="up")
            except Exception as e:
                log_message(f"[SCANNER] get_movers(up) error: {e}")
            try:
                down_symbols = self.client.get_movers(direction="down")
            except Exception as e:
                log_message(f"[SCANNER] get_movers(down) error: {e}")

        # Merge with user-pinned watchlist, deduplicate
        all_symbols = list(dict.fromkeys(up_symbols + down_symbols + list(self.watchlist)))

        if not all_symbols:
            log_message("[SCANNER] No candidates from movers or watchlist.")
            return []

        log_message(f"[SCANNER] Scanning {len(all_symbols)} symbols (movers + watchlist)...")
        quotes = self.client.get_quotes(all_symbols)
        results = []

        for symbol, data in quotes.items():
            try:
                result = self._evaluate_symbol(symbol, data)
                if result:
                    results.append(result)
            except Exception as e:
                log_message(f"[SCANNER] Error evaluating {symbol}: {e}")

        # --- Volume-monitor scan: check VOLUME_MONITOR_LIST for intraday spikes ---
        # This catches stocks that pop mid-session with high volume but might not
        # appear in the top-movers API list yet (early stage of the move).
        monitor_symbols = [s for s in VOLUME_MONITOR_LIST if s not in all_symbols]
        if monitor_symbols:
            try:
                monitor_quotes = self.client.get_quotes(monitor_symbols)
                for symbol, data in monitor_quotes.items():
                    try:
                        result = self._evaluate_symbol(symbol, data)
                        if result:
                            results.append(result)
                    except Exception:
                        pass
            except Exception as e:
                log_message(f"[SCANNER] volume-monitor batch error: {e}")

        # De-duplicate by symbol (keep entry with highest real-time vol rate)
        seen: Dict[str, dict] = {}
        for r in results:
            sym = r["symbol"]
            if sym not in seen or r.get("realtime_rvol", 0) > seen[sym].get("realtime_rvol", 0):
                seen[sym] = r
        results = list(seen.values())

        # Sort by realtime_rvol (primary) — stocks CURRENTLY running hot rank first.
        # This naturally pushes faded morning gaps down and keeps mid-day pops on top.
        sorted_all = sorted(
            results,
            key=lambda x: (
                int(bool(x.get("ready"))),
                float(x.get("score_pct") or 0),
                x.get("realtime_rvol", 0),
                abs(x.get("gap_pct", 0)),
            ),
            reverse=True,
        )

        gap_up   = [r for r in sorted_all if r.get("gap_pct", 0) > 0][:20]
        gap_down = [r for r in sorted_all if r.get("gap_pct", 0) < 0][:10]

        combined = gap_up + gap_down
        if sorted_all:
            top = sorted_all[0]
            log_message(
                f"[SCANNER] Found {len(gap_up)} gap-up {len(gap_down)} gap-down "
                f"| top: {top['symbol']} rtRVol={top.get('realtime_rvol',0):.1f}x "
                f"rvol={top.get('rel_vol',0):.1f}x {top.get('gap_pct',0):.1f}%"
            )
        else:
            log_message("[SCANNER] 0 candidates (no volume spikes detected)")
        return combined

    def _evaluate_symbol(self, symbol: str, data: Dict) -> Optional[Dict[str, Any]]:
        quote       = data.get("quote", {})
        fundamental = data.get("fundamental", {})

        last_price  = float(quote.get("lastPrice", 0) or 0)
        prev_close  = float(quote.get("closePrice", 0) or 0)
        open_price  = float(quote.get("openPrice", 0) or last_price)
        volume      = float(quote.get("totalVolume", 0) or 0)
        avg_vol_10  = float(fundamental.get("vol10DayAvg", 0) or 0)
        avg_vol_1y  = float(fundamental.get("vol1YearAvg", 0) or 0)
        shares_float = float(fundamental.get("sharesFloat", 0) or 0)

        # Use best available average volume
        avg_vol = avg_vol_10 if avg_vol_10 > 0 else avg_vol_1y
        if avg_vol == 0:
            avg_vol = 100_000  # fallback to avoid division by zero

        if prev_close == 0:
            return None

        gap_pct = ((open_price - prev_close) / prev_close) * 100   # opening gap (reference only)
        chg_pct = ((last_price - prev_close) / prev_close) * 100     # real-time intraday % change
        rel_vol  = volume / avg_vol if avg_vol > 0 else 0

        # ── Time-normalised real-time relative volume ─────────────────────
        # Compares current volume RATE to expected rate at this exact time of
        # day.  Stocks that faded mid-session fall below the threshold and
        # naturally drop from _active_symbols on the next scan cycle.
        #
        #   realtime_rvol = total_vol / (avg_daily_vol × elapsed/390)
        #
        # Example: 10x total vol at 9:45 → realtime_rvol ≈ 260x (still blazing)
        #          10x total vol at 2:00  → realtime_rvol ≈ 14x  (still hot)
        #          3x  total vol at 2:00  → realtime_rvol ≈  4x  (okay)
        #          3x  total vol at 3:30  → realtime_rvol ≈  3x  (borderline — almost done)
        try:
            from utils import now_et as _now_et_scan
            _tnow = _now_et_scan()
            elapsed_min = (_tnow.hour - 9) * 60 + (_tnow.minute - 30)
            elapsed_min = max(1, min(elapsed_min, 390))   # 1 min floor; cap at full day
        except Exception:
            elapsed_min = 195   # default to mid-day if clock fails
        expected_vol = avg_vol * (elapsed_min / 390.0)
        realtime_rvol = (volume / expected_vol) if expected_vol > 0 else 0.0

        vol_spike     = rel_vol >= self.vol_spike_threshold
        chg_threshold = 1.0 if vol_spike else self.min_gap_pct
        bid = float(quote.get("bidPrice", 0) or 0)
        ask = float(quote.get("askPrice", 0) or 0)
        spread_pct = ((ask - bid) / last_price * 100) if last_price > 0 and bid > 0 and ask > bid else 99.0
        day_high = float(quote.get("highPrice") or 0) or last_price
        day_low = float(quote.get("lowPrice") or 0) or last_price

        price_ok = last_price >= self.min_price and (self.max_price <= 0 or last_price <= self.max_price)
        float_ok = True
        if self.max_float > 0 and shares_float > 0:
            float_ok = shares_float <= self.max_float
        elif shares_float > 0:
            float_ok = shares_float <= 150_000_000

        items = [
            pillar("move", "Move size", abs(chg_pct) >= chg_threshold, f"{chg_pct:+.1f}% vs {chg_threshold:.1f}%"),
            pillar("rel_vol", "Relative volume", rel_vol >= self.min_rel_vol, f"{rel_vol:.1f}x"),
            pillar("pace", "Live volume pace", realtime_rvol >= self.min_realtime_rvol, f"{realtime_rvol:.1f}x"),
            pillar("price", "Price in range", price_ok, f"${last_price:.2f}"),
            pillar("float", "Float / liquidity", float_ok, f"{int(shares_float):,}" if shares_float else "n/a"),
            pillar("spread", "Tradeable spread", spread_pct <= 2.0, f"{spread_pct:.2f}%"),
        ]

        # Ross Cameron ★ flag: stock meets ALL 5 criteria (informational — not a filter)
        cameron_setup = (
            chg_pct >= 10.0
            and rel_vol >= 5.0
            and 2.0 <= last_price <= 20.0
            and (shares_float == 0 or shares_float < 10_000_000)
        )

        row = {
            "symbol":         symbol,
            "last":           round(last_price, 4),
            "price":          round(last_price, 4),
            "open":           round(open_price, 4),
            "prev_close":     round(prev_close, 4),
            "gap_pct":        round(chg_pct, 2),     # real-time % change from prev close
            "open_gap_pct":   round(gap_pct, 2),     # opening gap (for reference)
            "rel_vol":        round(rel_vol, 2),
            "realtime_rvol":  round(realtime_rvol, 2),
            "elapsed_min":    int(elapsed_min),
            "volume":         int(volume),
            "avg_vol":        int(avg_vol),
            "float":          int(shares_float),
            "bid":            round(bid, 4),
            "ask":            round(ask, 4),
            "direction":      "up" if chg_pct > 0 else "down",
            "cameron_setup":  cameron_setup,
            "support":        round(day_low, 4),
            "resistance":     round(day_high, 4),
            "day_low":        round(day_low, 4),
            "day_high":       round(day_high, 4),
        }
        apply_pillars(row, items)
        if not row["ready"] and not row["watching"]:
            return None
        return row

    # ------------------------------------------------------------------
    # ORB level calculation from 1-min candles
    # ------------------------------------------------------------------

    def get_premarket_levels(self, symbol: str) -> dict:
        """Pre-market range: high/low from 4:00 AM to 9:30 AM ET.
        Used as the ORB range for extended-hours entries before the
        regular session opens.  Works identically to get_orb_levels()
        but targets the pre-market session instead of 9:30-9:45.
        """
        candles = self._get_today_candles(symbol)
        if not candles:
            return None
        import datetime
        from zoneinfo import ZoneInfo
        from utils import now_et as _now_et
        _et = ZoneInfo("America/New_York")
        today_et = _now_et().date()
        market_open_ms = int(
            datetime.datetime(today_et.year, today_et.month, today_et.day,
                              9, 30, 0, tzinfo=_et).timestamp() * 1000
        )
        pm_candles = [c for c in candles if c.get("datetime", 0) < market_open_ms]
        if not pm_candles:
            return None
        pm_high = max(c["high"] for c in pm_candles)
        pm_low  = min(c["low"]  for c in pm_candles)
        return {
            "high": round(pm_high, 4),
            "low":  round(pm_low,  4),
            "mid":  round((pm_high + pm_low) / 2, 4),
        }

    def get_orb_levels(self, symbol: str, orb_minutes: int = 15) -> Optional[Dict[str, float]]:
        """
        Fetch today's 1-min candles and compute the opening range high/low
        for the first `orb_minutes` minutes after 09:30 ET.
        Returns {'high': x, 'low': x, 'mid': x} or None.
        """
        import datetime
        from zoneinfo import ZoneInfo
        from utils import now_et as _now_et
        _ET = ZoneInfo("America/New_York")
        today_et = _now_et().date()

        # Explicit today date range: 4 AM ET (pre-market start) to now
        # This ensures we get today's live intraday candles, not last completed day.
        day_start_ms = int(
            datetime.datetime(today_et.year, today_et.month, today_et.day, 4, 0, 0, tzinfo=_ET)
            .timestamp() * 1000
        )
        day_end_ms = int(_now_et().timestamp() * 1000)

        candles = self.client.get_price_history(
            symbol,
            frequency_type="minute",
            frequency=1,
            extended_hours=True,
            start_ms=day_start_ms,
            end_ms=day_end_ms,
        )
        if not candles:
            return None

        # Market opens at 09:30 ET — compute epoch using ET timezone
        open_epoch_ms = int(
            datetime.datetime(today_et.year, today_et.month, today_et.day, 9, 30, 0, tzinfo=_ET)
            .timestamp() * 1000
        )
        cutoff_ms = open_epoch_ms + orb_minutes * 60 * 1000

        orb_candles = [
            c for c in candles
            if open_epoch_ms <= c["datetime"] < cutoff_ms
        ]
        if not orb_candles:
            return None

        orb_high = max(c["high"] for c in orb_candles)
        orb_low  = min(c["low"]  for c in orb_candles)
        orb_mid  = round((orb_high + orb_low) / 2, 4)

        return {
            "high": round(orb_high, 4),
            "low":  round(orb_low,  4),
            "mid":  round(orb_mid,  4),
        }

    def _get_today_candles(self, symbol: str) -> List[Dict]:
        """Fetch today's 1-min candles using an explicit date range (4 AM ET → now).

        Using period=1 returns the last *completed* trading day, which is wrong
        after a holiday weekend or early in a new session.  Explicit start/end
        always returns the current intraday candles.
        """
        import datetime
        from zoneinfo import ZoneInfo
        from utils import now_et as _now_et
        _ET = ZoneInfo("America/New_York")
        today_et = _now_et().date()
        start_ms = int(
            datetime.datetime(today_et.year, today_et.month, today_et.day, 4, 0, 0, tzinfo=_ET)
            .timestamp() * 1000
        )
        end_ms = int(_now_et().timestamp() * 1000)
        return self.client.get_price_history(
            symbol, frequency_type="minute", frequency=1,
            extended_hours=True, start_ms=start_ms, end_ms=end_ms,
        )

    def get_avg_volume_per_min(self, symbol: str, bars: int = 20) -> float:
        """
        Returns average 1-minute bar volume over the last `bars` candles.
        Used by the trading engine to calibrate volume spike detection.
        """
        candles = self._get_today_candles(symbol)
        if not candles:
            return 0.0
        recent = candles[-bars:] if len(candles) >= bars else candles
        return sum(c.get("volume", 0) for c in recent) / len(recent) if recent else 0.0

    def get_vwap(self, symbol: str) -> Optional[float]:
        """Calculate VWAP from today's 1-min candles."""
        candles = self._get_today_candles(symbol)
        if not candles:
            return None
        return self._vwap_from_candles(candles)

    def get_intraday_retrace_pct(self, symbol: str, current_price: float) -> Optional[float]:
        """Return % of intraday gains (9:30 open -> day high) already given back.

        None when candles/price are unavailable or the stock has not made a
        meaningful move above the regular-session open (< $0.01 gain).
        """
        if current_price <= 0:
            return None
        candles = self._get_today_candles(symbol)
        if not candles:
            return None

        import datetime
        from zoneinfo import ZoneInfo
        from utils import now_et as _now_et

        _ET = ZoneInfo("America/New_York")
        today_et = _now_et().date()
        open_ms = int(
            datetime.datetime(
                today_et.year, today_et.month, today_et.day, 9, 30, 0, tzinfo=_ET
            ).timestamp() * 1000
        )
        reg_open = next((c for c in candles if c.get("datetime", 0) >= open_ms), None)
        open_price = reg_open["open"] if reg_open else candles[0]["open"]
        day_high = max(c["high"] for c in candles)
        gains = day_high - open_price
        if gains <= 0.01:
            return None
        return (day_high - current_price) / gains * 100.0

    # ------------------------------------------------------------------
    # Technical indicators (computed from real 1-min candle closes)
    # ------------------------------------------------------------------

    @staticmethod
    def _vwap_from_candles(candles: List[Dict]) -> Optional[float]:
        cumulative_pv = 0.0
        cumulative_v  = 0.0
        for c in candles:
            typical = (c["high"] + c["low"] + c["close"]) / 3
            cumulative_pv += typical * c["volume"]
            cumulative_v  += c["volume"]
        if cumulative_v == 0:
            return None
        return round(cumulative_pv / cumulative_v, 4)

    @staticmethod
    def compute_sma(closes: List[float], period: int) -> Optional[float]:
        """Simple moving average of the last `period` closes."""
        if len(closes) < period:
            return None
        return round(sum(closes[-period:]) / period, 6)

    @staticmethod
    def compute_ema(closes: List[float], period: int) -> Optional[float]:
        """Exponential moving average of the last `period` closes (SMA-seeded)."""
        if len(closes) < period:
            return None
        k = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for p in closes[period:]:
            ema = p * k + ema * (1 - k)
        return round(ema, 6)

    @staticmethod
    def compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
        """Wilder's RSI over `period` candle closes. Returns 0-100 or None."""
        if len(closes) < period + 1:
            return None
        gains = 0.0
        losses = 0.0
        # Seed with the first `period` deltas
        for i in range(1, period + 1):
            delta = closes[i] - closes[i - 1]
            if delta >= 0:
                gains += delta
            else:
                losses -= delta
        avg_gain = gains / period
        avg_loss = losses / period
        # Wilder smoothing for the remainder
        for i in range(period + 1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gain = delta if delta > 0 else 0.0
            loss = -delta if delta < 0 else 0.0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    @staticmethod
    def compute_atr(candles: List[Dict], period: int = 14) -> Optional[float]:
        """Average True Range over `period` candles (Wilder smoothing)."""
        if len(candles) < period + 1:
            return None
        trs: List[float] = []
        for i in range(1, len(candles)):
            high = candles[i]["high"]
            low  = candles[i]["low"]
            prev_close = candles[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        # Wilder-smoothed ATR
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        return round(atr, 6)

    @staticmethod
    def _ema_series(values: List[float], period: int) -> List[float]:
        """Full EMA series (SMA-seeded) aligned to `values` from index period-1."""
        if len(values) < period:
            return []
        k = 2.0 / (period + 1)
        ema = sum(values[:period]) / period
        out = [ema]
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
            out.append(ema)
        return out

    @staticmethod
    def compute_macd(
        closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Optional[Dict[str, float]]:
        """MACD line, signal line and histogram from candle closes.
        Returns {'macd', 'signal', 'hist'} or None if not enough data."""
        if len(closes) < slow + signal:
            return None
        fast_ema = StockScanner._ema_series(closes, fast)
        slow_ema = StockScanner._ema_series(closes, slow)
        # Align the two EMA series to the same (slow) starting index
        offset = slow - fast
        fast_aligned = fast_ema[offset:]
        n = min(len(fast_aligned), len(slow_ema))
        macd_line = [fast_aligned[i] - slow_ema[i] for i in range(n)]
        if len(macd_line) < signal:
            return None
        signal_series = StockScanner._ema_series(macd_line, signal)
        if not signal_series:
            return None
        macd_val   = macd_line[-1]
        signal_val = signal_series[-1]
        return {
            "macd":   round(macd_val, 6),
            "signal": round(signal_val, 6),
            "hist":   round(macd_val - signal_val, 6),
        }

    @staticmethod
    def compute_adx(candles: List[Dict], period: int = 14) -> Optional[float]:
        """Average Directional Index (Wilder). Measures trend STRENGTH (not
        direction): < 20 = choppy/range, > 25 = trending. Returns 0-100 or None."""
        if len(candles) < period * 2 + 1:
            return None
        plus_dm, minus_dm, trs = [], [], []
        for i in range(1, len(candles)):
            high, low = candles[i]["high"], candles[i]["low"]
            ph, pl    = candles[i - 1]["high"], candles[i - 1]["low"]
            pc        = candles[i - 1]["close"]
            up_move   = high - ph
            down_move = pl - low
            plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
            minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
            trs.append(max(high - low, abs(high - pc), abs(low - pc)))

        # Wilder smoothing of TR, +DM, -DM
        def _smooth(series: List[float]) -> List[float]:
            s = sum(series[:period])
            out = [s]
            for v in series[period:]:
                s = s - (s / period) + v
                out.append(s)
            return out

        atr_s   = _smooth(trs)
        plus_s  = _smooth(plus_dm)
        minus_s = _smooth(minus_dm)
        dxs: List[float] = []
        for i in range(len(atr_s)):
            if atr_s[i] == 0:
                dxs.append(0.0)
                continue
            plus_di  = 100.0 * plus_s[i] / atr_s[i]
            minus_di = 100.0 * minus_s[i] / atr_s[i]
            denom = plus_di + minus_di
            dxs.append(100.0 * abs(plus_di - minus_di) / denom if denom else 0.0)
        if len(dxs) < period:
            return None
        adx = sum(dxs[:period]) / period
        for dx in dxs[period:]:
            adx = (adx * (period - 1) + dx) / period
        return round(adx, 2)

    @staticmethod
    def _resample_5min_bars(candles: List[Dict]) -> List[Dict]:
        """Aggregate 1-min candles into completed 5-min OHLCV bars."""
        bars: List[Dict] = []
        n = len(candles) - (len(candles) % 5)
        for i in range(0, n, 5):
            block = candles[i:i + 5]
            if len(block) < 5:
                continue
            bars.append({
                "open":   block[0]["open"],
                "high":   max(c["high"] for c in block),
                "low":    min(c["low"] for c in block),
                "close":  block[-1]["close"],
                "volume": sum(c.get("volume", 0) for c in block),
            })
        return bars

    @staticmethod
    def _current_5min_partial(candles: List[Dict]) -> Optional[Dict]:
        """In-progress 5-min bar built from trailing 1-min candles."""
        rem = len(candles) % 5
        if rem == 0:
            return None
        block = candles[-rem:]
        return {
            "open":   block[0]["open"],
            "high":   max(c["high"] for c in block),
            "low":    min(c["low"] for c in block),
            "close":  block[-1]["close"],
            "volume": sum(c.get("volume", 0) for c in block),
            "bars":   len(block),
        }

    @staticmethod
    def _resample_5min(candles: List[Dict]) -> List[float]:
        """Aggregate 1-min candle closes into 5-min closes (close of each block)."""
        closes = []
        for i in range(0, len(candles) - (len(candles) % 5), 5):
            block = candles[i:i + 5]
            if len(block) == 5:
                closes.append(block[-1]["close"])
        return closes

    @staticmethod
    def _breakout_vol_ratio(
        candles: List[Dict],
        orb_high: Optional[float],
        orb_minutes: int,
    ) -> float:
        """Peak rel-volume among post-ORB candles that closed above orb_high.

        Uses the strongest volume bar from the breakout sequence so quiet
        consolidation candles after the breakout do not invalidate the signal.
        """
        if not candles or not orb_high or orb_high <= 0:
            return 0.0
        vols = [c.get("volume", 0) for c in candles]
        avg_vol = (sum(vols) / len(vols)) if vols else 0.0
        if avg_vol <= 0:
            return 0.0
        post_orb = candles[orb_minutes:] if len(candles) > orb_minutes else candles
        best = 0.0
        for c in post_orb:
            if c["close"] > orb_high:
                ratio = c.get("volume", 0) / avg_vol
                if ratio > best:
                    best = ratio
        return best


    def get_5min_long_entry_check(
        self,
        symbol: str,
        price: float,
        sma_period: int = 10,
        dip_sma_touch_pct: float = 0.30,
        require_vol: bool = True,
        require_dip: bool = True,
    ) -> Dict:
        """
        Global long-entry rules on the 5-minute chart:
          1. Price above SMA(10) on 5-min closes
          2. Current (in-progress) 5-min candle volume above average 5-min volume
          3. Dip only — recent red 5m/1m candle OR pullback touch of SMA10
        Returns {ok, reason, sma10, vol_ratio, checks}.
        """
        out: Dict = {
            "ok": False,
            "reason": "no candle data",
            "sma10": None,
            "vol_ratio": None,
            "checks": [],
        }
        if price <= 0:
            out["reason"] = "invalid price"
            return out

        candles = self._get_today_candles(symbol)
        if not candles or len(candles) < sma_period * 5:
            out["reason"] = "insufficient 1-min candles for 5m SMA"
            return out

        bars_5m = self._resample_5min_bars(candles)
        if len(bars_5m) < sma_period:
            out["reason"] = f"need {sma_period}+ completed 5m bars"
            return out

        closes_5m = [b["close"] for b in bars_5m]
        sma10 = self.compute_sma(closes_5m, sma_period)
        if sma10 is None:
            out["reason"] = "SMA10 unavailable"
            return out
        out["sma10"] = round(sma10, 4)

        above_sma = price > sma10
        out["checks"].append({
            "label": f"Price above 5m SMA{sma_period} (${sma10:.2f})",
            "pass": above_sma,
            "group": "5m",
        })
        if not above_sma:
            out["reason"] = f"price ${price:.2f} below 5m SMA{sma_period} ${sma10:.2f}"
            return out

        vols_5m = [b.get("volume", 0) for b in bars_5m]
        avg_5m_vol = sum(vols_5m) / len(vols_5m) if vols_5m else 0.0
        partial = self._current_5min_partial(candles)
        if partial:
            cur_vol = partial["volume"]
            # Scale projected full-bar volume when bar is partially formed
            cur_vol_est = cur_vol * (5 / max(1, partial.get("bars", 1)))
        else:
            cur_vol_est = bars_5m[-1].get("volume", 0)

        vol_ratio = (cur_vol_est / avg_5m_vol) if avg_5m_vol > 0 else 0.0
        out["vol_ratio"] = round(vol_ratio, 2)
        vol_ok = (vol_ratio > 1.0) if require_vol else True
        if require_vol:
            out["checks"].append({
                "label": f"5m candle vol above avg ({vol_ratio:.2f}x)",
                "pass": vol_ok,
                "group": "5m",
            })
        if require_vol and not vol_ok:
            out["reason"] = f"5m vol {vol_ratio:.2f}x not above average"
            return out

        dip_ok = False
        dip_reason = ""
        touch_zone = sma10 * (1 - dip_sma_touch_pct / 100.0)

        if bars_5m[-1]["close"] < bars_5m[-1]["open"]:
            dip_ok = True
            dip_reason = "completed red 5m candle"

        if not dip_ok:
            for b in bars_5m[-4:]:
                if b["low"] <= touch_zone:
                    dip_ok = True
                    dip_reason = f"pullback touched 5m SMA{sma_period} (${sma10:.2f})"
                    break

        if not dip_ok and partial and partial["low"] <= touch_zone:
            dip_ok = True
            dip_reason = f"live bar touched 5m SMA{sma_period}"

        if not dip_ok:
            for c in candles[-10:]:
                if c.get("close", 0) < c.get("open", 0):
                    dip_ok = True
                    dip_reason = "recent red 1m pullback candle"
                    break

        if require_dip and not dip_ok and bars_5m:
            recent_high = max(b["high"] for b in bars_5m[-6:])
            if price >= recent_high * 0.998:
                out["reason"] = "chasing 5m highs — wait for dip to SMA10 or red candle"
                out["checks"].append({
                    "label": "Dip entry (SMA touch or red candle)",
                    "pass": False,
                    "group": "5m",
                })
                return out

        if require_dip:
            out["checks"].append({
                "label": "Dip entry (SMA touch or red candle)",
                "pass": dip_ok,
                "group": "5m",
            })
        if require_dip and not dip_ok:
            out["reason"] = "no dip — wait for pullback to 10 SMA or red candle"
            return out

        out["ok"] = True
        out["reason"] = dip_reason
        return out


    def get_indicators(
        self,
        symbol: str,
        orb_minutes: int = 15,
        orb_high: Optional[float] = None,
    ) -> Optional[Dict]:
        """
        Single candle fetch → all indicators the strategy needs:
          ema9, ema20, rsi, atr, vwap, last_closed_close, breakout_vol_ratio.

        breakout_vol_ratio = peak volume ratio among post-ORB candles that
        closed above orb_high (not the latest bar — avoids false rejects
        during quiet consolidation after a valid volume breakout).
        last_closed_close is the close of the last fully-formed candle (used for
        candle-close breakout confirmation rather than an intrabar wick).
        """
        candles = self._get_today_candles(symbol)
        if not candles or len(candles) < 2:
            return None

        closes = [c["close"] for c in candles]
        vols   = [c.get("volume", 0) for c in candles]

        avg_vol = (sum(vols) / len(vols)) if vols else 0.0
        if orb_high and orb_high > 0:
            breakout_vol_ratio = self._breakout_vol_ratio(candles, orb_high, orb_minutes)
        else:
            last_vol = vols[-1] if vols else 0.0
            breakout_vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 0.0

        macd = self.compute_macd(closes)

        bars_5m = self._resample_5min_bars(candles)
        htf_closes = [b["close"] for b in bars_5m] or self._resample_5min(candles)
        htf_sma10 = self.compute_sma(htf_closes, 10) if len(htf_closes) >= 10 else None
        htf_ema9   = self.compute_ema(htf_closes, 9)  if len(htf_closes) >= 9  else None
        htf_ema20  = self.compute_ema(htf_closes, 20) if len(htf_closes) >= 20 else None
        if htf_ema9 is not None and htf_ema20 is not None:
            htf_uptrend = htf_ema9 > htf_ema20
        else:
            htf_uptrend = None

        return {
            "ema9":               self.compute_ema(closes, 9),
            "ema20":              self.compute_ema(closes, 20),
            "rsi":                self.compute_rsi(closes, 14),
            "atr":                self.compute_atr(candles, 14),
            "vwap":               self._vwap_from_candles(candles),
            "last_closed_close":  closes[-1],
            "breakout_vol_ratio": round(breakout_vol_ratio, 2),
            "macd":               (macd or {}).get("macd"),
            "macd_signal":        (macd or {}).get("signal"),
            "macd_hist":          (macd or {}).get("hist"),
            "adx":                self.compute_adx(candles, 14),
            "htf_uptrend":        htf_uptrend,
            "htf_sma10":          htf_sma10,
            "bars_5m":            bars_5m,
            "candles":            candles,   # raw candles for pattern detection
        }

    @staticmethod
    def detect_first_pullback(
        candles: List[Dict],
        min_pole_pct: float = 1.0,
        max_retrace_pct: float = 50.0,
        min_flag_candles: int = 1,
        max_flag_candles: int = 4,
        min_pole_candles: int = 2,
        lookback: int = 30,
    ) -> Optional[Dict]:
        """
        Detect Ross Cameron's 'First Pullback' candlestick setup on 1-min candles.

        Pattern structure:
          POLE  — ≥2 consecutive green candles (close > open), net move ≥ min_pole_pct %
          FLAG  — 1-4 red candles (close < open), retracing ≤ max_retrace_pct % of pole
          ENTRY — most recent candle closes ABOVE the flag's high (new high after pullback)

        Returns a dict with entry/stop levels or None if pattern not found.
        """
        if len(candles) < min_pole_candles + min_flag_candles + 1:
            return None

        # Focus on the most recent `lookback` candles to avoid stale pole structures
        if len(candles) > lookback:
            candles = candles[-lookback:]
        n = len(candles)

        # Signal candle = candles[-1] (most recently closed 1-min bar)
        signal = candles[-1]

        # --- Identify the FLAG: consecutive red candles ending at candles[-2] ---
        flag_end_idx   = n - 2
        flag_start_idx = flag_end_idx
        while (flag_start_idx >= 0 and
               candles[flag_start_idx]["close"] < candles[flag_start_idx]["open"]):
            flag_start_idx -= 1
        flag_start_idx += 1  # step forward to first red candle

        red_count = flag_end_idx - flag_start_idx + 1
        if red_count < min_flag_candles or red_count > max_flag_candles:
            return None
        if flag_start_idx <= 0:
            return None  # no room for a pole before the flag

        flag_low   = min(c["low"]            for c in candles[flag_start_idx:flag_end_idx + 1])
        flag_high  = max(c["high"]           for c in candles[flag_start_idx:flag_end_idx + 1])
        flag_vols  = [c.get("volume", 0)     for c in candles[flag_start_idx:flag_end_idx + 1]]
        avg_flag_vol = sum(flag_vols) / len(flag_vols) if flag_vols else 0.0

        # --- Identify the POLE: green candles immediately before the flag ---
        pole_end_idx   = flag_start_idx - 1
        pole_start_idx = pole_end_idx
        while (pole_start_idx > 0 and
               candles[pole_start_idx - 1]["close"] >= candles[pole_start_idx - 1]["open"]):
            pole_start_idx -= 1

        pole_count = pole_end_idx - pole_start_idx + 1
        if pole_count < min_pole_candles:
            return None

        pole_bottom = candles[pole_start_idx]["open"]
        pole_top    = max(c["high"] for c in candles[pole_start_idx:pole_end_idx + 1])
        pole_height = pole_top - pole_bottom

        if pole_bottom <= 0 or pole_height <= 0:
            return None

        pole_pct = (pole_height / pole_bottom) * 100.0
        if pole_pct < min_pole_pct:
            return None

        # --- 50 % retracement rule (Ross Cameron): flag must hold upper half of pole ---
        fifty_pct_level = pole_bottom + pole_height * (max_retrace_pct / 100.0)
        if flag_low < fifty_pct_level:
            return None

        # --- Signal candle must CLOSE above the flag's high (new high after pullback) ---
        if signal["close"] <= flag_high:
            return None

        return {
            "flag_high":    round(flag_high,    4),
            "flag_low":     round(flag_low,     4),
            "pole_top":     round(pole_top,     4),
            "pole_bottom":  round(pole_bottom,  4),
            "pole_height":  round(pole_height,  4),
            "pole_pct":     round(pole_pct,     2),
            "red_candles":  red_count,
            "fifty_pct":    round(fifty_pct_level, 4),
            "avg_flag_vol": round(avg_flag_vol, 2),
            "signal_vol":   signal.get("volume", 0),
        }
