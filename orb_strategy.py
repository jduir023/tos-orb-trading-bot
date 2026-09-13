"""
orb_strategy.py
Opening Range Breakout strategy engine.
Detects ORB breakouts, calculates position size, and generates trade signals.
Mirrors strategies.py from the crypto bot.
"""

import time
import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from utils import log_message, now_et as _now_et
from pillars import pillar, pack as pack_pillars, failed_names


@dataclass
class TradeSignal:
    symbol:      str
    direction:   str          # LONG or SHORT
    entry_price: float
    stop_price:  float
    target_r2:   float        # 2:1 R:R target
    target_r3:   float        # 3:1 R:R target
    shares:      int
    risk_dollars: float
    orb_high:    float
    orb_low:     float
    vwap:        Optional[float]
    rel_vol:     float
    gap_pct:     float
    timestamp:   float = field(default_factory=time.time)
    reason:      str = ""


@dataclass
class OpenPosition:
    symbol:       str
    direction:    str
    entry_price:  float
    shares:       int
    stop_price:   float
    target_r2:    float
    target_r3:    float
    risk_dollars: float
    entry_time:   float = field(default_factory=time.time)
    entry_order_id: Optional[str] = None
    oco_order_id:   Optional[str] = None
    status:       str = "open"      # open | closed | stopped
    exit_price:   Optional[float] = None
    exit_time:    Optional[float] = None
    pnl:          float = 0.0
    # Trailing stop upgrade tracking
    trailing_stop_active:  bool  = False
    peak_price:            float = 0.0
    last_volume_snapshot:  int   = 0
    # Partial-exit / breakeven tracking
    original_shares:       int   = 0
    partial_exit_done:     bool  = False
    breakeven_active:      bool  = False
    realized_pnl:          float = 0.0   # locked-in PnL from partial exits
    # Analytics metadata (carried from the entry signal)
    entry_reason:          str   = ""    # signal reason — used to classify setup type
    gap_pct:               float = 0.0   # gap % at entry — used for gap-bucket slicing


class ORBStrategy:
    """
    Opening Range Breakout strategy.

    Rules:
    - After 09:45 ET (ORB window closed), monitor for close above ORB high (long)
      or close below ORB low (short — skipped for cash accounts by default).
    - Entry only when price is above VWAP (long bias) and EMA9 > EMA20.
    - Stop = ORB low (long). Target = 2R or 3R.
    - Position size = risk_per_trade_pct % of account / stop_distance.
    - Max 3 trades per day. Max 1 open position per symbol.
    - No new entries after 12:00 ET (configurable).
    """

    def __init__(self) -> None:
        self.account_size:       float = 7111.00
        self.risk_per_trade_pct: float = 1.5       # % of account per trade
        self.orb_minutes:        int   = 15
        self.max_trades_per_day: int   = 10
        self.max_concurrent_positions: int = 5  # Max simultaneous open positions
        self.entry_cutoff_hour:  int   = 12         # No entries after noon ET
        self.min_rel_vol:        float = 2.0
        self.require_vwap_above: bool  = True       # Long only above VWAP
        self.allow_short:        bool  = False      # Disabled for cash account
        self.rr_target:          float = 2.0        # Default R:R target
        self.min_stop_dist:      float = 0.10       # Reject setups with < 10c stop (spread protection)

        # --- High-probability quality filters (target 70-80% win rate) ---
        self.require_orb_above_vwap: bool  = True   # ORB high must exceed VWAP: open range set bullish context
        self.orb_min_range_pct:      float = 0.5    # ORB range must be >= this % of price (flat open filter)
        self.orb_max_range_pct:      float = 8.0    # ORB range must be <= this % of price (chaotic open filter)
        self.orb_max_chase_pct:      float = 1.5    # Entry must be within this % above ORB high (no chasing)
        self.require_pullback:        bool  = True     # Wait for pullback after ORB breakout
        self.orb_pullback_min_pct:    float = 0.20    # Min dip from post-breakout high before entry
        self.orb_pullback_max_pct:    float = 1.50    # Max dip — deeper invalidates the setup
        self.orb_pullback_reclaim_pct: float = 0.10   # Bounce above pullback low to confirm continuation
        self.orb_pullback_max_chase_pct: float = 1.00 # Tighter chase cap on pullback entries
        self.orb_momentum_vol_min:     float = 8.0     # Breakout-bar vol to skip pullback (hella momentum)
        self.orb_momentum_rel_vol_min: float = 5.0     # Scanner rel-vol to skip pullback
        self.orb_fp_min_pole_pct:      float = 0.8     # First-pullback pole minimum when used as entry
        self.orb_fp_max_retrace_pct:   float = 50.0    # Max flag retrace of pole height
        self.orb_max_float:          int   = 0      # Max float for ORB entry (0=no limit); engine sets from scan data

        # --- Strategy enhancement config ---
        self.confirm_close:        bool  = True     # Require a CLOSED candle above ORB high
        self.require_volume_confirm: bool = True    # Require volume spike on breakout bar
        self.min_breakout_rel_vol: float = 1.5      # Breakout bar vol / avg vol threshold
        self.use_rsi_filter:       bool  = True     # Enforce RSI bounds on entry
        self.rsi_overbought:       float = 75.0     # Skip LONG above this RSI
        self.rsi_oversold:         float = 25.0     # Skip SHORT below this RSI
        self.use_atr_stops:        bool  = True     # ATR-based stop instead of ORB low
        self.atr_stop_mult:        float = 1.5      # Stop = entry - atr * mult

        # --- Advanced indicator filters (optional, default OFF) ---
        self.use_macd_filter:      bool  = False    # Require bullish MACD histogram on entry
        self.use_adx_filter:       bool  = False    # Require trend strength (ADX) on entry
        self.adx_min:              float = 20.0      # Minimum ADX to consider a trend tradeable
        self.use_htf_filter:       bool  = False    # Require 5-min EMA alignment (higher timeframe)
        self.use_macd_exit:        bool  = False    # Exit held positions on MACD bearish cross

        self._trades_today:  int = 0
        self._trade_date:    str = ""
        self._open_positions: Dict[str, OpenPosition] = {}
        self._closed_positions: List[OpenPosition] = []
        self._signals_generated: List[TradeSignal] = []

        # Per-symbol ORB state
        self._orb_levels: Dict[str, Dict] = {}        # symbol -> {high, low, mid}
        self._vwap:       Dict[str, float] = {}       # symbol -> vwap
        self._indicators: Dict[str, Dict] = {}        # symbol -> indicator dict from scanner
        self._scan_meta:  Dict[str, Dict] = {}        # symbol -> {rel_vol, gap_pct} from scanner
        self._triggered:  Dict[str, bool] = {}        # symbol -> already triggered today
        self._pullback_state: Dict[str, Dict] = {}  # symbol -> breakout/pullback tracking
        self._last_pillars: Dict[str, Dict] = {}      # symbol -> latest 6-pillar score

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def set_config(
        self,
        account_size: float = 7111.00,
        risk_pct: float = 1.5,
        orb_minutes: int = 15,
        max_trades: int = 10,
        max_concurrent_positions: int = 5,
        entry_cutoff_hour: int = 12,
        rr_target: float = 2.0,
        require_vwap_above: bool = True,
        allow_short: bool = False,
        confirm_close: bool = True,
        require_volume_confirm: bool = True,
        min_breakout_rel_vol: float = 1.5,
        use_rsi_filter: bool = True,
        rsi_overbought: float = 75.0,
        rsi_oversold: float = 25.0,
        use_atr_stops: bool = True,
        atr_stop_mult: float = 1.5,
        use_macd_filter: bool = False,
        use_adx_filter: bool = False,
        adx_min: float = 20.0,
        use_htf_filter: bool = False,
        use_macd_exit: bool = False,
        min_stop_dist: float = 0.10,
        require_orb_above_vwap: bool = True,
        orb_min_range_pct: float = 0.5,
        orb_max_range_pct: float = 8.0,
        orb_max_chase_pct: float = 1.5,
        orb_max_float: int = 0,
        require_pullback: bool = True,
        orb_pullback_min_pct: float = 0.20,
        orb_pullback_max_pct: float = 1.50,
        orb_pullback_reclaim_pct: float = 0.10,
        orb_pullback_max_chase_pct: float = 1.00,
        orb_momentum_vol_min: float = 8.0,
        orb_momentum_rel_vol_min: float = 5.0,
        orb_fp_min_pole_pct: float = 0.8,
        orb_fp_max_retrace_pct: float = 50.0,
    ) -> None:
        self.account_size             = account_size
        self.risk_per_trade_pct       = risk_pct
        self.orb_minutes              = orb_minutes
        self.max_trades_per_day       = max_trades
        self.max_concurrent_positions = max_concurrent_positions
        self.entry_cutoff_hour        = entry_cutoff_hour
        self.rr_target                = rr_target
        self.require_vwap_above       = require_vwap_above
        self.allow_short              = allow_short
        self.confirm_close            = confirm_close
        self.require_volume_confirm   = require_volume_confirm
        self.min_breakout_rel_vol     = min_breakout_rel_vol
        self.use_rsi_filter           = use_rsi_filter
        self.rsi_overbought           = rsi_overbought
        self.rsi_oversold             = rsi_oversold
        self.use_atr_stops            = use_atr_stops
        self.atr_stop_mult            = atr_stop_mult
        self.use_macd_filter          = use_macd_filter
        self.use_adx_filter           = use_adx_filter
        self.adx_min                  = adx_min
        self.use_htf_filter           = use_htf_filter
        self.use_macd_exit            = use_macd_exit
        self.min_stop_dist            = min_stop_dist
        self.require_orb_above_vwap   = require_orb_above_vwap
        self.orb_min_range_pct        = orb_min_range_pct
        self.orb_max_range_pct        = orb_max_range_pct
        self.orb_max_chase_pct        = orb_max_chase_pct
        self.orb_max_float            = orb_max_float
        self.require_pullback          = require_pullback
        self.orb_pullback_min_pct      = orb_pullback_min_pct
        self.orb_pullback_max_pct      = orb_pullback_max_pct
        self.orb_pullback_reclaim_pct  = orb_pullback_reclaim_pct
        self.orb_pullback_max_chase_pct = orb_pullback_max_chase_pct
        self.orb_momentum_vol_min      = orb_momentum_vol_min
        self.orb_momentum_rel_vol_min  = orb_momentum_rel_vol_min
        self.orb_fp_min_pole_pct       = orb_fp_min_pole_pct
        self.orb_fp_max_retrace_pct    = orb_fp_max_retrace_pct

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def reset_day(self) -> None:
        today = _now_et().date().isoformat()
        if self._trade_date != today:
            self._trade_date     = today
            self._trades_today   = 0
            self._orb_levels     = {}
            self._vwap           = {}
            self._indicators     = {}
            self._scan_meta      = {}
            self._triggered      = {}
            self._pullback_state = {}
            log_message(f"[ORB] Daily state reset for {today}.")

    # ------------------------------------------------------------------
    # ORB level injection (called by trading engine after 09:45 ET)
    # ------------------------------------------------------------------

    def set_orb_levels(self, symbol: str, levels: Dict) -> None:
        self._orb_levels[symbol] = levels
        log_message(f"[ORB] {symbol} levels — H:{levels['high']} L:{levels['low']} M:{levels['mid']}")

    def set_vwap(self, symbol: str, vwap: float) -> None:
        self._vwap[symbol] = vwap

    # ------------------------------------------------------------------
    # Indicator + scan-metadata injection (fed by the engine each poll,
    # computed from REAL 1-minute candle closes — not tick samples)
    # ------------------------------------------------------------------

    def set_indicators(self, symbol: str, indicators: Dict) -> None:
        """Store EMA9/EMA20/RSI/ATR/last_closed_close/breakout_vol_ratio for a symbol."""
        if indicators:
            self._indicators[symbol] = indicators
            if indicators.get("vwap"):
                self._vwap[symbol] = indicators["vwap"]

    def set_scan_meta(self, symbol: str, rel_vol: float, gap_pct: float, float_shares: int = 0) -> None:
        """Store scanner metadata (rel_vol, gap_pct, float_shares) for entry filters."""
        self._scan_meta[symbol] = {"rel_vol": rel_vol, "gap_pct": gap_pct, "float_shares": float_shares}

    def has_orb_levels(self, symbol: str) -> bool:
        return symbol in self._orb_levels

    # ------------------------------------------------------------------
    # Indicator accessors
    # ------------------------------------------------------------------

    def get_ema9(self, symbol: str) -> Optional[float]:
        return self._indicators.get(symbol, {}).get("ema9")

    def get_ema20(self, symbol: str) -> Optional[float]:
        return self._indicators.get(symbol, {}).get("ema20")

    def get_rsi(self, symbol: str) -> Optional[float]:
        return self._indicators.get(symbol, {}).get("rsi")

    def get_atr(self, symbol: str) -> Optional[float]:
        return self._indicators.get(symbol, {}).get("atr")

    # ------------------------------------------------------------------
    # Position size
    # ------------------------------------------------------------------

    def calc_position_size(self, entry: float, stop: float) -> tuple:
        """Returns (shares, risk_dollars, stop_distance).

        Position sizing is risk-based (1R = risk_per_trade_pct % of account),
        but is capped so that a single trade never consumes more than
        account_size / max_concurrent_positions dollars.  This ensures the
        account can carry up to max_concurrent open positions simultaneously
        without exceeding its total capital, even on stocks with very tight stops.
        """
        stop_distance = abs(entry - stop)
        if stop_distance < 0.01:
            stop_distance = 0.01
        risk_dollars = self.account_size * (self.risk_per_trade_pct / 100)
        shares = max(1, int(risk_dollars / stop_distance))

        # Capital cap: each trade uses at most 25% of account value so the
        # account can hold 4 concurrent positions fully deployed (4 × 25% = 100%).
        if entry > 0:
            max_cost = self.account_size * 0.25
            max_shares_by_capital = max(1, int(max_cost / entry))
            shares = min(shares, max_shares_by_capital)

        actual_risk = shares * stop_distance
        return shares, round(actual_risk, 2), round(stop_distance, 4)


    def _momentum_bypass(self, vol_ratio: float, rel_vol: float) -> bool:
        """Allow immediate breakout entry only on extreme volume + momentum."""
        return (
            vol_ratio >= self.orb_momentum_vol_min
            and rel_vol >= self.orb_momentum_rel_vol_min
        )

    def _update_pullback_state(self, symbol: str, current_price: float, orb_high: float) -> Dict:
        st = self._pullback_state.setdefault(symbol, {
            "saw_breakout": False,
            "session_high": 0.0,
            "pullback_low": None,
        })
        if current_price > orb_high:
            st["saw_breakout"] = True
        if st["saw_breakout"]:
            st["session_high"] = max(st["session_high"], current_price)
            high = st["session_high"]
            if current_price < high:
                prev = st.get("pullback_low")
                st["pullback_low"] = current_price if prev is None else min(prev, current_price)
        return st

    def _pullback_entry_ready(
        self,
        symbol: str,
        current_price: float,
        orb_high: float,
        candles: Optional[List],
    ) -> tuple:
        """Return (ready, reason). Pullback after ORB breakout, then reclaim."""
        from scanner import StockScanner

        st = self._update_pullback_state(symbol, current_price, orb_high)
        if not st["saw_breakout"]:
            return False, "waiting for ORB breakout"

        if candles:
            pattern = StockScanner.detect_first_pullback(
                candles,
                min_pole_pct=self.orb_fp_min_pole_pct,
                max_retrace_pct=self.orb_fp_max_retrace_pct,
            )
            if pattern and current_price > orb_high:
                return True, (
                    f"first_pullback pole={pattern['pole_pct']:.1f}% "
                    f"reds={pattern['red_candles']}"
                )

        high = st["session_high"]
        pb_low = st.get("pullback_low")
        if high <= 0 or pb_low is None:
            return False, "waiting for pullback after breakout"

        if pb_low < orb_high:
            return False, f"pullback lost ORB high ({pb_low:.2f} < {orb_high:.2f})"

        pullback_pct = (high - pb_low) / high * 100
        if pullback_pct < self.orb_pullback_min_pct:
            return False, (
                f"pullback too shallow ({pullback_pct:.2f}% < {self.orb_pullback_min_pct}%)"
            )
        if pullback_pct > self.orb_pullback_max_pct:
            return False, (
                f"pullback too deep ({pullback_pct:.2f}% > {self.orb_pullback_max_pct}%)"
            )

        reclaim_px = round(pb_low * (1 + self.orb_pullback_reclaim_pct / 100), 2)
        if round(current_price, 2) < reclaim_px:
            return False, f"waiting for reclaim ({current_price:.2f} < {reclaim_px:.2f})"

        return True, f"pullback reclaim {pullback_pct:.2f}% from high"

    # ------------------------------------------------------------------
    # Signal evaluation
    # ------------------------------------------------------------------

    def evaluate(self, symbol: str, current_price: float) -> Optional[TradeSignal]:
        """
        Call this each poll with the latest price.
        Returns a TradeSignal if an ORB breakout entry is valid, else None.

        Confirmation stack (all must pass for a LONG):
          1. Breakout: last CLOSED candle close > ORB high (not just an intrabar wick)
          2. VWAP:     price above VWAP (trend bias)
          3. EMA:      EMA9 > EMA20 (momentum, from real candle closes)
          4. RSI:      RSI <= rsi_overbought (not chasing a parabolic move)
          5. Volume:   breakout bar volume >= min_breakout_rel_vol x average
        """
        import datetime
        self.reset_day()

        now_et = _now_et()

        # Cutoff check
        if now_et.hour >= self.entry_cutoff_hour:
            return None

        # Max trades check
        if self._trades_today >= self.max_trades_per_day:
            return None

        # Max concurrent positions check
        if len(self._open_positions) >= self.max_concurrent_positions:
            return None

        # Already triggered this symbol today
        if self._triggered.get(symbol):
            return None

        # Already have open position in this symbol
        if symbol in self._open_positions:
            return None

        # ORB levels must be set (only available after 09:45)
        levels = self._orb_levels.get(symbol)
        if not levels:
            return None

        orb_high = levels["high"]
        orb_low  = levels["low"]
        vwap     = self._vwap.get(symbol)

        ind   = self._indicators.get(symbol, {})
        ema9  = ind.get("ema9")
        ema20 = ind.get("ema20")
        rsi   = ind.get("rsi")
        atr   = ind.get("atr")
        last_closed = ind.get("last_closed_close", current_price)
        vol_ratio   = ind.get("breakout_vol_ratio", 0.0)

        meta    = self._scan_meta.get(symbol, {})
        rel_vol = float(meta.get("rel_vol", 0.0) or 0.0)
        gap_pct = float(meta.get("gap_pct", 0.0) or 0.0)

        # Breakout reference: closed candle if confirmation enabled, else live price
        long_break_ref  = last_closed if self.confirm_close else current_price
        short_break_ref = last_closed if self.confirm_close else current_price

        # --- LONG: 6 trigger pillars, trade only at 95% (6/6) ---
        macd_hist = ind.get("macd_hist")
        adx = ind.get("adx")
        htf_uptrend = ind.get("htf_uptrend")
        candles = ind.get("candles")
        float_shares = int(meta.get("float_shares", 0) or 0)
        momentum = self._momentum_bypass(vol_ratio, rel_vol)

        breakout_ok = current_price > orb_high and long_break_ref > orb_high

        vwap_ok = True
        if self.require_vwap_above and vwap:
            vwap_ok = current_price >= vwap
        if self.require_orb_above_vwap and vwap:
            vwap_ok = vwap_ok and orb_high >= vwap

        trend_ok = True
        if ema9 is not None and ema20 is not None:
            trend_ok = ema9 >= ema20
        if self.use_macd_filter and macd_hist is not None:
            trend_ok = trend_ok and macd_hist > 0
        if self.use_adx_filter and adx is not None:
            trend_ok = trend_ok and adx >= self.adx_min
        if self.use_htf_filter and htf_uptrend is False:
            trend_ok = False

        vol_ok = True
        if self.require_volume_confirm:
            vol_ok = bool(vol_ratio and vol_ratio >= self.min_breakout_rel_vol)

        range_ok = True
        orb_range_pct = 0.0
        if current_price > 0:
            orb_range_pct = (orb_high - orb_low) / current_price * 100
            if self.orb_min_range_pct > 0 and orb_range_pct < self.orb_min_range_pct:
                range_ok = False
            if self.orb_max_range_pct > 0 and orb_range_pct > self.orb_max_range_pct:
                range_ok = False
        if self.orb_max_float > 0 and float_shares > self.orb_max_float:
            range_ok = False
        chase_cap = self.orb_max_chase_pct if momentum else self.orb_pullback_max_chase_pct
        if chase_cap > 0 and orb_high > 0 and current_price > orb_high * (1 + chase_cap / 100):
            range_ok = False

        timing_ok = True
        pb_reason = ""
        if self.use_rsi_filter and rsi is not None and rsi > self.rsi_overbought:
            timing_ok = False
            pb_reason = f"RSI {rsi} > {self.rsi_overbought}"
        if timing_ok and self.require_pullback and not momentum:
            pb_ready, pb_reason = self._pullback_entry_ready(
                symbol, current_price, orb_high, candles
            )
            timing_ok = bool(pb_ready)
        elif momentum:
            pb_reason = f"momentum bypass vol={vol_ratio}x rel={rel_vol}x"

        packed = pack_pillars([
            pillar("breakout", "ORB breakout", breakout_ok,
                   f"{current_price:.2f} vs high {orb_high}"),
            pillar("vwap", "VWAP bias", vwap_ok,
                   f"vwap={vwap:.4f}" if vwap else "n/a"),
            pillar("trend", "Trend (EMA/MACD/HTF)", trend_ok,
                   f"ema9={ema9} ema20={ema20}"),
            pillar("volume", "Breakout volume", vol_ok, f"{vol_ratio or 0:.1f}x"),
            pillar("range", "Range / chase quality", range_ok, f"{orb_range_pct:.2f}%"),
            pillar("timing", "Pullback or momentum", timing_ok, pb_reason),
        ])
        self._last_pillars[symbol] = packed

        if not packed["ready"]:
            if breakout_ok:
                misses = failed_names(packed)
                log_message(
                    f"[ORB] {symbol} {packed['passed']}/{packed['total']} pillars "
                    f"({packed['score_pct']}%) — need 95%. Missing: {misses}"
                )
            return None

        if momentum:
            log_message(
                f"[ORB] {symbol} momentum bypass — vol={vol_ratio}x rel_vol={rel_vol}x "
                f"(min {self.orb_momentum_vol_min}x / {self.orb_momentum_rel_vol_min}x)"
            )

        if True:  # LONG entry — all 6 pillars met
            entry = current_price
            # ATR-based stop (tighter, consistent risk) or ORB-low fallback
            if self.use_atr_stops and atr and atr > 0:
                atr_stop = round(entry - atr * self.atr_stop_mult, 4)
                # Never risk more than the ORB low; use the closer of the two
                stop = max(atr_stop, orb_low) if atr_stop < entry else orb_low
            else:
                stop = orb_low
            shares, risk_dollars, stop_dist = self.calc_position_size(entry, stop)

            # Minimum stop distance: reject when spread alone could stop us out
            if stop_dist < self.min_stop_dist:
                log_message(f"[ORB] {symbol} LONG skipped — stop dist ${stop_dist:.4f} < min ${self.min_stop_dist:.2f}")
                return None

            target_r2 = round(entry + stop_dist * 2, 4)
            target_r3 = round(entry + stop_dist * 3, 4)

            signal = TradeSignal(
                symbol=symbol,
                direction="LONG",
                entry_price=entry,
                stop_price=stop,
                target_r2=target_r2,
                target_r3=target_r3,
                shares=shares,
                risk_dollars=risk_dollars,
                orb_high=orb_high,
                orb_low=orb_low,
                vwap=vwap,
                rel_vol=rel_vol,
                gap_pct=gap_pct,
                reason=(
                    f"ORB LONG {'momentum ' if momentum else 'pullback '}breakout above {orb_high} "
                    f"pillars={packed['passed']}/{packed['total']} ({packed['score_pct']}%) "
                    f"(RSI={rsi} vol={vol_ratio}x rel={rel_vol}x)"
                ),
            )
            self._signals_generated.append(signal)
            log_message(
                f"[ORB] SIGNAL: LONG {symbol} entry={entry} stop={stop} "
                f"target={target_r2} shares={shares} risk=${risk_dollars} "
                f"rsi={rsi} vol={vol_ratio}x"
            )
            return signal

        # --- SHORT breakout (disabled by default for cash accounts) ---
        if self.allow_short and current_price < orb_low and short_break_ref < orb_low:
            if self.require_vwap_above and vwap and current_price > vwap:
                return None
            if ema9 is not None and ema20 is not None and ema9 > ema20:
                return None
            # RSI oversold filter (don't short an already-washed-out name)
            if self.use_rsi_filter and rsi is not None and rsi < self.rsi_oversold:
                log_message(f"[ORB] {symbol} SHORT skipped — RSI {rsi} < {self.rsi_oversold}")
                return None
            if self.require_volume_confirm and vol_ratio and vol_ratio < self.min_breakout_rel_vol:
                return None

            entry = current_price
            if self.use_atr_stops and atr and atr > 0:
                atr_stop = round(entry + atr * self.atr_stop_mult, 4)
                stop = min(atr_stop, orb_high) if atr_stop > entry else orb_high
            else:
                stop = orb_high
            shares, risk_dollars, stop_dist = self.calc_position_size(entry, stop)
            target_r2 = round(entry - stop_dist * 2, 4)
            target_r3 = round(entry - stop_dist * 3, 4)

            signal = TradeSignal(
                symbol=symbol,
                direction="SHORT",
                entry_price=entry,
                stop_price=stop,
                target_r2=target_r2,
                target_r3=target_r3,
                shares=shares,
                risk_dollars=risk_dollars,
                orb_high=orb_high,
                orb_low=orb_low,
                vwap=vwap,
                rel_vol=rel_vol,
                gap_pct=gap_pct,
                reason=f"ORB SHORT breakdown below {orb_low} (RSI={rsi} vol={vol_ratio}x)",
            )
            self._signals_generated.append(signal)
            log_message(
                f"[ORB] SIGNAL: SHORT {symbol} entry={entry} stop={stop} "
                f"target={target_r2} shares={shares} risk=${risk_dollars}"
            )
            return signal

        return None

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------

    def record_entry(self, signal: TradeSignal, order_id: str, status: str = "open") -> OpenPosition:
        pos = OpenPosition(
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            shares=signal.shares,
            stop_price=signal.stop_price,
            target_r2=signal.target_r2,
            target_r3=signal.target_r3,
            risk_dollars=signal.risk_dollars,
            entry_order_id=order_id,
            original_shares=signal.shares,
            entry_reason=signal.reason,
            gap_pct=signal.gap_pct,
            status=status,
        )
        self._open_positions[signal.symbol] = pos
        self._triggered[signal.symbol] = True
        self._trades_today += 1
        return pos

    def mark_filled(self, symbol: str, fill_price: Optional[float] = None) -> Optional[OpenPosition]:
        """Promote a PENDING entry to HELD once the broker confirms the fill."""
        pos = self._open_positions.get(symbol)
        if not pos or pos.status != "pending":
            return None
        if fill_price and fill_price > 0:
            pos.entry_price = round(fill_price, 4)
        pos.status     = "open"
        pos.entry_time = time.time()
        log_message(f"[ORB] FILLED {symbol} @ {pos.entry_price} — now HELD ({pos.shares} sh)")
        return pos

    def drop_pending(self, symbol: str) -> Optional[OpenPosition]:
        """Remove a PENDING entry that was never filled (limit expired/cancelled)."""
        pos = self._open_positions.get(symbol)
        if not pos or pos.status != "pending":
            return None
        self._open_positions.pop(symbol, None)
        self._triggered.pop(symbol, None)   # allow a fresh attempt later
        if self._trades_today > 0:
            self._trades_today -= 1          # the entry never became a real trade
        log_message(f"[ORB] DROPPED unfilled entry for {symbol}")
        return pos

    def record_partial_exit(self, symbol: str, exit_price: float, qty: int) -> float:
        """Book a partial scale-out. Reduces share count, locks realized PnL,
        moves the stop to breakeven. Returns the realized PnL for this slice."""
        pos = self._open_positions.get(symbol)
        if not pos or qty <= 0:
            return 0.0
        qty = min(qty, pos.shares)
        if pos.direction == "LONG":
            slice_pnl = (exit_price - pos.entry_price) * qty
        else:
            slice_pnl = (pos.entry_price - exit_price) * qty
        pos.shares        -= qty
        pos.realized_pnl  += slice_pnl
        pos.partial_exit_done = True
        pos.breakeven_active  = True
        pos.stop_price        = pos.entry_price  # move stop to breakeven on the runner
        log_message(
            f"[ORB] PARTIAL EXIT {symbol} {qty}sh @ {exit_price} "
            f"(realized ${slice_pnl:.2f}) — stop → breakeven, {pos.shares}sh left"
        )
        return round(slice_pnl, 2)

    def record_exit(self, symbol: str, exit_price: float, reason: str = "target") -> Optional[OpenPosition]:
        pos = self._open_positions.pop(symbol, None)
        if not pos:
            return None
        pos.exit_price = exit_price
        pos.exit_time  = time.time()
        pos.status     = reason
        if pos.direction == "LONG":
            pos.pnl = (exit_price - pos.entry_price) * pos.shares + pos.realized_pnl
        else:
            pos.pnl = (pos.entry_price - exit_price) * pos.shares + pos.realized_pnl
        self._closed_positions.append(pos)
        log_message(
            f"[ORB] EXIT {symbol} @ {exit_price} ({reason}) "
            f"PnL: ${pos.pnl:.2f}"
        )
        return pos

    def get_open_positions(self) -> List[OpenPosition]:
        """Held positions only (entry filled, bracket active)."""
        return [p for p in self._open_positions.values() if p.status == "open"]

    def get_pending_positions(self) -> List[OpenPosition]:
        """Entry limit orders placed but not yet filled."""
        return [p for p in self._open_positions.values() if p.status == "pending"]

    def get_active_positions(self) -> List[OpenPosition]:
        """Everything currently committed (pending + held) — for persistence/concurrency."""
        return list(self._open_positions.values())

    def get_macd_hist(self, symbol: str) -> Optional[float]:
        return self._indicators.get(symbol, {}).get("macd_hist")

    def get_closed_positions(self) -> List[OpenPosition]:
        """All closed positions (historical — used by analytics)."""
        return list(self._closed_positions)

    def get_today_closed_positions(self) -> List[OpenPosition]:
        """Positions closed during today’s ET session only.
        Resets automatically at midnight ET via reset_day().
        Used for the ‘Sold / Closed Today’ table and Day P&L.
        """
        from utils import now_et as _now_et
        today = _now_et().date().isoformat()
        result = []
        for p in self._closed_positions:
            if p.exit_time:
                try:
                    exit_date = datetime.datetime.fromtimestamp(
                        float(p.exit_time)
                    ).strftime("%Y-%m-%d")
                    if exit_date == today:
                        result.append(p)
                except Exception:
                    pass
        return result

    def get_day_pnl(self) -> float:
        """P&L from positions closed TODAY only, plus any locked-in partial-exit
        profit on still-open positions.  Resets to 0 at midnight ET.
        """
        today_closed_pnl = sum(p.pnl for p in self.get_today_closed_positions())
        open_realized    = sum(p.realized_pnl for p in self._open_positions.values())
        return round(today_closed_pnl + open_realized, 2)

    def get_trades_today(self) -> int:
        return self._trades_today

    def get_signals_today(self) -> List[TradeSignal]:
        return list(self._signals_generated)
