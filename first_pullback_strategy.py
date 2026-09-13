"""
first_pullback_strategy.py
Ross Cameron 'First Pullback' momentum strategy — signal generator only.

Pattern (1-min chart):
  POLE  — ≥2 consecutive green candles, net move ≥ 1% of price
  FLAG  — 1-4 red candles on lower volume, must hold ≥ 50% of pole height
  ENTRY — first candle to CLOSE above the flag's high (new high after pullback)
  STOP  — low of the flag
  TARGET— 2:1 R:R minimum (entry + 2 × stop distance)
  MACD  — histogram must be positive (optional, default ON)
  TIME  — 09:30–11:30 ET on 1-min chart (configurable)

This class generates TradeSignal objects only.  Position tracking and
order execution are handled by the TradingEngine → ORBStrategy pipeline,
so First Pullback signals go through exactly the same fill / bracket /
trailing-stop / partial-exit machinery as ORB signals.
"""

import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from scanner import StockScanner
from orb_strategy import TradeSignal
from utils import log_message, now_et as _now_et
from pillars import pillar, pack as pack_pillars, failed_names

_ET = ZoneInfo("America/New_York")


class FirstPullbackStrategy:
    """
    Signal generator for Ross Cameron's First Pullback setup.

    Call evaluate() on every poll tick for each active symbol.
    Returns a TradeSignal when all conditions are met, else None.
    Signals are de-duplicated per symbol per day via _triggered.
    """

    def __init__(self) -> None:
        # --- Config (mirrors ORBStrategy conventions) ---
        self.enabled:          bool  = False
        self.account_size:     float = 7111.00
        self.risk_pct:         float = 1.5
        self.rr_target:        float = 2.0    # minimum R:R (Ross uses 2:1)
        self.time_limit_hhmm:  int   = 1130   # no new FP entries after 11:30 ET
        self.use_macd_filter:  bool  = True   # MACD histogram must be > 0
        self.use_rsi_filter:   bool  = True   # RSI must be below overbought
        self.rsi_overbought:   float = 75.0
        self.max_stop_cents:   float = 0.30   # reject setups with > 30-cent stop
        self.min_pole_pct:     float = 1.0    # pole must move ≥ this % of price
        self.max_retrace_pct:  float = 50.0
        self.min_stop_dist:    float = 0.10   # Reject setups with < 10c stop distance   # flag cannot retrace > this % of pole

        # --- Per-day state ---
        self._triggered:  Dict[str, bool] = {}
        self._trade_date: str = ""

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def set_config(
        self,
        enabled:         bool  = False,
        account_size:    float = 7111.00,
        risk_pct:        float = 1.5,
        rr_target:       float = 2.0,
        time_limit_hhmm: int   = 1130,
        use_macd_filter: bool  = True,
        use_rsi_filter:  bool  = True,
        rsi_overbought:  float = 75.0,
        max_stop_cents:  float = 0.30,
        min_stop_dist:   float = 0.10,
        min_pole_pct:    float = 1.0,
        max_retrace_pct: float = 50.0,
    ) -> None:
        self.enabled         = enabled
        self.account_size    = account_size
        self.risk_pct        = risk_pct
        self.rr_target       = rr_target
        self.time_limit_hhmm = time_limit_hhmm
        self.use_macd_filter = use_macd_filter
        self.use_rsi_filter  = use_rsi_filter
        self.rsi_overbought  = rsi_overbought
        self.max_stop_cents  = max_stop_cents
        self.min_stop_dist   = min_stop_dist
        self.min_pole_pct    = min_pole_pct
        self.max_retrace_pct = max_retrace_pct

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def reset_day(self) -> None:
        today = _now_et().date().isoformat()
        if self._trade_date != today:
            self._trade_date = today
            self._triggered  = {}

    # ------------------------------------------------------------------
    # Signal evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        symbol:         str,
        indicators:     Dict,
        open_positions: Dict,
    ) -> Optional[TradeSignal]:
        """
        Evaluate one symbol for a First Pullback entry signal.

        Args:
            symbol:         Ticker symbol.
            indicators:     Dict from scanner.get_indicators() — must include "candles".
            open_positions: Dict of currently open/pending positions (symbol → OpenPosition).
                            Passed in by the engine from strategy._open_positions.

        Returns a TradeSignal or None.
        """
        if not self.enabled:
            return None

        # Already triggered for this symbol today
        if self._triggered.get(symbol):
            return None

        # Already have an open or pending position in this symbol (ORB or prior FP)
        if symbol in open_positions:
            return None

        # Time gate: 09:30–time_limit in regular session;
        #            4:00 AM–time_limit in pre-market (extended hours)
        now_et = _now_et()
        hm = now_et.hour * 100 + now_et.minute
        premarket_ok = hm >= 400 and hm < 930  # 4:00-9:30 AM
        regular_ok   = hm >= 930 and hm < self.time_limit_hhmm
        if not (premarket_ok or regular_ok):
            return None

        candles = indicators.get("candles")
        if not candles or len(candles) < 5:
            return None

        pattern = StockScanner.detect_first_pullback(
            candles,
            min_pole_pct=self.min_pole_pct,
            max_retrace_pct=self.max_retrace_pct,
        )
        macd_hist = indicators.get("macd_hist")
        rsi = indicators.get("rsi")
        macd_ok = (not self.use_macd_filter) or (macd_hist is not None and macd_hist > 0)
        rsi_ok = (not self.use_rsi_filter) or rsi is None or rsi <= self.rsi_overbought
        retrace_ok = True
        if pattern:
            retrace_ok = float(pattern.get("retrace_pct") or 0) <= self.max_retrace_pct

        entry = pattern["flag_high"] if pattern else 0
        stop  = pattern["flag_low"] if pattern else 0
        risk  = round(entry - stop, 4) if pattern else 0
        stop_ok = bool(pattern and risk >= self.min_stop_dist and risk <= self.max_stop_cents)

        packed = pack_pillars([
            pillar("pattern", "Pole + flag", bool(pattern)),
            pillar("macd", "MACD bullish", macd_ok, str(macd_hist)),
            pillar("rsi", "RSI not overbought", rsi_ok, str(rsi)),
            pillar("stop", "Stop in band", stop_ok, f"{risk:.2f}" if pattern else ""),
            pillar("window", "Time window", True),
            pillar("retrace", "Flag retrace", retrace_ok),
        ])
        if not packed["ready"]:
            log_message(
                f"[FP] {symbol} {packed['passed']}/{packed['total']} pillars "
                f"({packed['score_pct']}%) — need 95%. Missing: {failed_names(packed)}"
            )
            return None

        # Position sizing: identical formula to ORBStrategy, with capital cap
        risk_dollars = self.account_size * (self.risk_pct / 100.0)
        shares       = max(1, int(risk_dollars / risk))
        # Cap: no single trade > 25% of account (same cap as ORB)
        if entry > 0:
            max_cost = self.account_size * 0.25
            shares   = min(shares, max(1, int(max_cost / entry)))
        rr           = max(self.rr_target, 2.0)          # never less than 2:1
        target_r2    = round(entry + risk * rr,  4)
        target_r3    = round(entry + risk * 3.0, 4)

        # Mark triggered — one FP signal per symbol per day (prevents signal spam)
        self._triggered[symbol] = True

        log_message(
            f"[FP] SIGNAL: LONG {symbol} entry={entry} stop={stop} "
            f"target={target_r2} shares={shares} risk=${risk_dollars:.2f} "
            f"pole={pattern['pole_pct']}% reds={pattern['red_candles']} "
            f"pillars={packed['passed']}/{packed['total']}"
        )

        return TradeSignal(
            symbol=symbol,
            direction="LONG",
            entry_price=entry,
            stop_price=stop,
            target_r2=target_r2,
            target_r3=target_r3,
            shares=shares,
            risk_dollars=risk_dollars,
            orb_high=pattern["pole_top"],     # reused field: pole top = natural target
            orb_low=pattern["flag_low"],      # reused field: flag low = stop anchor
            vwap=indicators.get("vwap"),
            rel_vol=float(indicators.get("breakout_vol_ratio") or 0.0),
            gap_pct=0.0,
            reason=(
                f"first_pullback pole={pattern['pole_pct']}% "
                f"reds={pattern['red_candles']} retrace50={pattern['fifty_pct']}"
            ),
        )
