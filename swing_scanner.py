"""
swing_scanner.py
Intraday swing oscillator scanner.
Finds stocks that have made 3+ pivot high/low cycles during the trading day.
These are range-bound names ideal for repeated scalp entries at support/resistance.
"""

import datetime
import concurrent.futures
from typing import Any, Dict, List, Optional, Tuple

from schwab_client import SchwabClient
from utils import log_message
from pillars import pillar, apply as apply_pillars

# Expanded watchlist of liquid/volatile names always worth checking for swings
SWING_BASE_WATCHLIST = [
    # Mega-cap tech & high-beta
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD","NFLX","INTC",
    "SMCI","AVGO","QCOM","MU","MRVL","ARM","CRWD","PANW","SNOW","PLTR",
    # ETFs & leveraged
    "SPY","QQQ","IWM","TQQQ","SQQQ","UVXY","VIXY","ARKK","XLK","XLF",
    "LABU","LABD","FNGU","SOXL","SOXS","TECS","TECL","FAS","FAZ","UPRO",
    # Volatile mid-caps / meme-adjacent
    "SOFI","RIVN","LCID","NIO","PLTR","HOOD","OPEN","AFRM","UPST","COIN",
    "MSTR","RIOT","MARA","HUT","CLSK","CIFR","BITO","IBIT","FBTC","GBTC",
    # Biotech / small-cap runners
    "SNDL","SPCE","GME","AMC","BBBY","CLOV","WISH","EXPR","KOSS","CTRM",
    # Sector leaders
    "JPM","BAC","GS","MS","C","WFC","XOM","CVX","OXY","SLB",
    "BA","LMT","RTX","NOC","GE","CAT","DE","MMM","HON","UPS",
]


class SwingScanner:
    """
    Detects intraday high/low swing cycles from 1-minute candles.

    A "swing" is one full cycle: price moves from a local low to a local high
    (or vice versa) with meaningful amplitude.

    Stocks with 3-4+ swings per day are ideal for scalping the range.
    """

    def __init__(self, client: SchwabClient) -> None:
        self.client = client
        self.min_swings:      int   = 3
        self.min_swing_pct:   float = 0.5
        self.pivot_lookback:  int   = 5
        self.min_price:       float = 1.00
        self.max_price:       float = 500.00
        self.min_avg_vol:     int   = 100_000
        self.max_retrace_pct: float = 40.0
        self.candle_minutes:  int   = 1

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def set_config(
        self,
        min_swings: int = 3,
        min_swing_pct: float = 0.5,
        pivot_lookback: int = 5,
        min_price: float = 1.00,
        max_price: float = 500.00,
        min_avg_vol: int = 100_000,
        max_retrace_pct: float = 40.0,
        candle_minutes: int = 1,
    ) -> None:
        self.min_swings      = min_swings
        self.min_swing_pct   = min_swing_pct
        self.pivot_lookback  = pivot_lookback
        self.min_price       = min_price
        self.max_price       = max_price
        self.min_avg_vol     = min_avg_vol
        self.max_retrace_pct = max_retrace_pct
        self.candle_minutes  = max(1, int(candle_minutes))

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    def scan(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """
        Scan a list of symbols. Returns qualifying swing candidates sorted
        by swing count descending.
        """
        results = []
        for symbol in symbols:
            try:
                result = self._analyze(symbol)
                if result:
                    results.append(result)
            except Exception as e:
                log_message(f"[SWING] Error analyzing {symbol}: {e}")

        results.sort(key=lambda x: (int(bool(x.get("ready"))), float(x.get("score_pct") or 0), x.get("swing_count", 0)), reverse=True)
        log_message(f"[SWING] Found {len(results)} swing candidates from {len(symbols)} symbols.")
        return results

    def scan_market(
        self,
        extra_symbols: Optional[List[str]] = None,
        top_n: int = 10,
        max_workers: int = 12,
    ) -> List[Dict[str, Any]]:
        """Full market swing scan using two stages:

        Stage 1 — Build universe:
          • Query Schwab movers across all major indices (up + down)
          • Merge with SWING_BASE_WATCHLIST + any caller-supplied symbols

        Stage 2 — Bulk quote pre-filter (one API call):
          • Drop symbols outside price / volume thresholds
          • Only proceed with the qualifying subset (avoids hundreds of wasted candle calls)

        Stage 3 — Parallel candle analysis:
          • Fetch today's 1-min candles concurrently (ThreadPoolExecutor)
          • Run pivot detection on each and return top_n sorted by swing count
        """
        ALL_INDICES = [
            "$COMPX",   # NASDAQ Composite
            "$SPX.X",   # S&P 500
            "$DJI",     # Dow Jones
            "$RUT.X",   # Russell 2000
            "$NDX.X",   # NASDAQ 100
            "$MID",     # S&P MidCap 400
            "$SML",     # S&P SmallCap 600
        ]

        # --- Stage 1: universe assembly ---
        universe: List[str] = list(SWING_BASE_WATCHLIST)
        if extra_symbols:
            universe += extra_symbols

        if hasattr(self.client, "get_movers"):
            for direction in ("up", "down"):
                try:
                    universe += self.client.get_movers(
                        indices=ALL_INDICES, direction=direction
                    )
                except Exception as e:
                    log_message(f"[SWING] get_movers({direction}) error: {e}")

        # Deduplicate, preserve order
        seen: set = set()
        candidates = [s for s in universe if not (s in seen or seen.add(s))]  # type: ignore
        log_message(f"[SWING] Market scan: {len(candidates)} candidates before pre-filter")

        # --- Stage 2: bulk quote pre-filter ---
        try:
            quotes = self.client.get_quotes(candidates)
        except Exception as e:
            log_message(f"[SWING] Bulk quote error: {e}")
            quotes = {}

        filtered: List[str] = []
        for sym in candidates:
            q = quotes.get(sym, {}).get("quote", {})
            price  = float(q.get("lastPrice", 0) or 0)
            volume = float(q.get("totalVolume", 0) or 0)
            if price < self.min_price or price > self.max_price:
                continue
            if volume < self.min_avg_vol:
                continue
            filtered.append(sym)

        log_message(f"[SWING] Pre-filter: {len(filtered)} symbols pass price/vol check")

        # --- Stage 3: parallel candle analysis ---
        results: List[Dict[str, Any]] = []

        def _safe_analyze(sym: str) -> Optional[Dict[str, Any]]:
            try:
                return self._analyze(sym)
            except Exception as e:
                log_message(f"[SWING] Error analyzing {sym}: {e}")
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_safe_analyze, sym): sym for sym in filtered}
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res:
                    results.append(res)

        results.sort(key=lambda x: (int(bool(x.get("ready"))), float(x.get("score_pct") or 0), x.get("swing_count", 0)), reverse=True)
        top = results[:top_n]
        log_message(
            f"[SWING] Market scan complete: {len(results)} qualifying / "
            f"{len(filtered)} analyzed. Top {top_n}: "
            + ", ".join(f"{r['symbol']}({r['swing_count']}sw)" for r in top)
        )
        return top

    # ------------------------------------------------------------------
    # Per-symbol analysis
    # ------------------------------------------------------------------

    @staticmethod
    def _resample_candles(candles: List[Dict], minutes: int) -> List[Dict]:
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
                           "close": b[-1]["close"], "volume": sum(x.get("volume",0) for x in b)})
        return result

    def _get_today_candles(self, symbol: str) -> List[Dict]:
        """Fetch today's 1-min candles using explicit date range (4 AM ET to now).

        Using period=1 returns the last completed trading day, which fails after
        holiday weekends or early in a new session.
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
        end_ms = int(datetime.datetime.now(tz=_ET).timestamp() * 1000)
        return self.client.get_price_history(
            symbol, frequency_type="minute", frequency=1,
            extended_hours=True, start_ms=start_ms, end_ms=end_ms,
        )

    def _analyze(self, symbol: str) -> Optional[Dict[str, Any]]:
        raw = self._get_today_candles(symbol)
        candles = self._resample_candles(raw, self.candle_minutes)
        if not candles or len(candles) < self.pivot_lookback * 2 + 1:
            return None

        closes = [c["close"] for c in candles]
        highs  = [c["high"]  for c in candles]
        lows   = [c["low"]   for c in candles]
        vols   = [c["volume"] for c in candles]

        last_price = closes[-1]
        price_ok = self.min_price <= last_price <= self.max_price
        if not price_ok:
            return None

        avg_vol = sum(vols) / len(vols) if vols else 0
        vol_ok = avg_vol >= self.min_avg_vol

        # Detect pivot highs and lows
        pivot_highs = self._find_pivot_highs(highs)
        pivot_lows  = self._find_pivot_lows(lows)

        # Build alternating pivot sequence
        swing_count, pivots = self._count_swings(pivot_highs, pivot_lows, closes)
        swings_ok = swing_count >= self.min_swings

        # Check minimum amplitude
        if pivots:
            amplitudes = []
            for i in range(1, len(pivots)):
                amp = abs(pivots[i]["price"] - pivots[i-1]["price"])
                pct = amp / pivots[i-1]["price"] * 100
                amplitudes.append(pct)
            avg_amplitude = sum(amplitudes) / len(amplitudes) if amplitudes else 0
        else:
            avg_amplitude = 0
        amp_ok = avg_amplitude >= self.min_swing_pct

        # Day high/low range
        day_high = max(highs)
        day_low  = min(lows)
        day_range_pct = (day_high - day_low) / day_low * 100 if day_low > 0 else 0

        # Regular-session open: first candle at/after 9:30 ET.
        # Used to measure retrace of intraday gains (not just position in range).
        from zoneinfo import ZoneInfo as _ZI
        _et = _ZI("America/New_York")
        _td = datetime.datetime.now(tz=_et).date()
        _open_ms = int(datetime.datetime(_td.year, _td.month, _td.day, 9, 30, 0, tzinfo=_et).timestamp() * 1000)
        _moc = next((c for c in candles if c.get("datetime", 0) >= _open_ms), None)
        open_price = _moc["open"] if _moc else candles[0]["open"]

        # Last pivot direction tells us where we are in the cycle
        last_pivot = pivots[-1] if pivots else None
        current_bias = "At High" if (last_pivot and last_pivot["type"] == "high") else "At Low"

        # Support/resistance levels from recent pivots
        recent_highs = sorted([p["price"] for p in pivots if p["type"] == "high"], reverse=True)
        recent_lows  = sorted([p["price"] for p in pivots if p["type"] == "low"])
        resistance   = recent_highs[0] if recent_highs else day_high
        support      = recent_lows[0]  if recent_lows  else day_low

        range_ok = day_range_pct >= max(self.min_swing_pct * 2, 1.0)
        range_size = resistance - support
        near_support = range_size > 0 and last_price <= support + range_size * 0.15
        gains = day_high - open_price
        retrace_ok = True
        if gains > 0.01 and near_support:
            retrace_pct = (day_high - last_price) / gains * 100
            retrace_ok = retrace_pct <= self.max_retrace_pct
        bounce = candles[-1]["close"] > candles[-1]["open"] or last_price > support
        retrace_pillar = retrace_ok and bounce

        items = [
            pillar("swings", "3+ swing cycles", swings_ok, str(swing_count)),
            pillar("amplitude", "Swing amplitude", amp_ok, f"{avg_amplitude:.2f}%"),
            pillar("volume", "Average volume", vol_ok, f"{int(avg_vol):,}"),
            pillar("range", "Intraday range", range_ok, f"{day_range_pct:.2f}%"),
            pillar("support", "At / near support", near_support, current_bias),
            pillar("retrace", "Retrace + bounce", retrace_pillar),
        ]
        row = {
            "symbol":        symbol,
            "last":          round(last_price, 4),
            "price":         round(last_price, 4),
            "swing_count":   swing_count,
            "avg_amplitude": round(avg_amplitude, 2),
            "day_high":      round(day_high, 4),
            "day_low":       round(day_low, 4),
            "open_price":    round(open_price, 4),
            "day_range_pct": round(day_range_pct, 2),
            "support":       round(support, 4),
            "resistance":    round(resistance, 4),
            "current_bias":  current_bias,
            "avg_vol":       int(avg_vol),
            "pivots":        pivots[-6:],  # last 6 pivots for charting
        }
        apply_pillars(row, items)
        if not row["ready"] and not row["watching"]:
            return None
        log_message(
            f"[SWING] {symbol} — {swing_count} swings | {row['passed']}/{row['total']} pillars "
            f"({row['score_pct']}%) | last=${last_price:.4f} | bias={current_bias}"
        )
        return row

    # ------------------------------------------------------------------
    # Pivot detection
    # ------------------------------------------------------------------

    def _find_pivot_highs(self, highs: List[float]) -> List[Dict]:
        pivots = []
        lb = self.pivot_lookback
        for i in range(lb, len(highs) - lb):
            window = highs[i - lb: i + lb + 1]
            if highs[i] == max(window):
                pivots.append({"index": i, "price": highs[i], "type": "high"})
        return pivots

    def _find_pivot_lows(self, lows: List[float]) -> List[Dict]:
        pivots = []
        lb = self.pivot_lookback
        for i in range(lb, len(lows) - lb):
            window = lows[i - lb: i + lb + 1]
            if lows[i] == min(window):
                pivots.append({"index": i, "price": lows[i], "type": "low"})
        return pivots

    def _count_swings(
        self,
        pivot_highs: List[Dict],
        pivot_lows: List[Dict],
        closes: List[float],
    ) -> Tuple[int, List[Dict]]:
        """
        Merge pivot highs and lows into a chronological alternating sequence.
        Returns (swing_count, pivot_list).
        A swing = one high-to-low or low-to-high transition.
        """
        all_pivots = sorted(pivot_highs + pivot_lows, key=lambda p: p["index"])

        # Build alternating sequence (no two highs or two lows in a row)
        alternating: List[Dict] = []
        for p in all_pivots:
            if not alternating:
                alternating.append(p)
            elif alternating[-1]["type"] != p["type"]:
                alternating.append(p)
            else:
                # Keep the more extreme one
                last = alternating[-1]
                if p["type"] == "high" and p["price"] > last["price"]:
                    alternating[-1] = p
                elif p["type"] == "low" and p["price"] < last["price"]:
                    alternating[-1] = p

        swing_count = max(0, len(alternating) - 1)
        return swing_count, alternating

    # ------------------------------------------------------------------
    # Signal: is price near support for a long entry?
    # ------------------------------------------------------------------

    def get_swing_entry_signal(self, symbol: str, current_price: float) -> Optional[Dict]:
        """
        Returns a signal dict if price is within 0.5% of the last swing support level
        (buy the dip in a range-bound stock).
        """
        result = self._analyze(symbol)
        if not result or not result.get("ready"):
            if result:
                log_message(
                    f"[SWING] {symbol} skip — {result.get('passed')}/{result.get('total')} pillars "
                    f"({result.get('score_pct')}%), need 95%"
                )
            return None

        support    = result["support"]
        resistance = result["resistance"]
        range_size = resistance - support

        if range_size <= 0:
            return None

        near_support    = current_price <= support + range_size * 0.15
        near_resistance = current_price >= resistance - range_size * 0.15

        if not near_support:
            return None

        direction = "LONG"
        target    = round(support + range_size * 0.75, 4)  # target 75% of range
        stop      = round(support - range_size * 0.10, 4)  # stop 10% below support

        return {
            "symbol":     symbol,
            "direction":  direction,
            "entry":      current_price,
            "stop":       stop,
            "target":     target,
            "support":    support,
            "resistance": resistance,
            "swing_count": result["swing_count"],
            "reason":     (
                f"Swing range entry near support (swings={result['swing_count']}) "
                f"pillars={result.get('passed')}/{result.get('total')} ({result.get('score_pct')}%)"
            ),
        }
