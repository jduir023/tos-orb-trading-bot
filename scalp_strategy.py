"""
scalp_strategy.py  v2 — immediate-entry, dollar-per-trade capital sizing

dollar_per_trade = total CAPITAL deployed (shares = floor(amount / price))
stop_pct = 1.5% below entry (tight, semi-fixed as requested)
target   = entry + stop_distance × rr_ratio  (default 2:1)
immediate_mode: on start, enters immediately without waiting for breakout signal
Works in pre-market, regular session, and after-hours.
"""

import time
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from orb_strategy import TradeSignal
from utils import log_message, now_et as _now_et

_ET = ZoneInfo("America/New_York")


class ScalpStrategy:

    def __init__(self) -> None:
        self.enabled: bool = False
        self.symbol:  str  = ""

        # Capital & risk
        self.dollar_per_trade:  float = 200.0
        self.session_budget:    float = 500.0

        # Exit
        self.target_pct:   float = 0.50
        self.stop_pct:     float = 1.50    # 1.5% tight stop as requested
        self.use_rr_ratio: bool  = True
        self.rr_ratio:     float = 2.0

        # Entry mode
        self.immediate_mode:  bool = True   # buy immediately on arm, no conditions
        self.screener_mode:   bool = False  # use dip-to-support screener results for entry

        # Optional conditions (used only when immediate_mode=False)
        self.breakout_bars:    int   = 5
        self.candle_minutes:   int   = 1
        self.min_vol_mult:     float = 1.5
        self.rsi_min:          float = 40.0
        self.rsi_max:          float = 70.0
        self.require_vwap:     bool  = True
        self.require_ema:      bool  = True

        # Timing
        self.cooldown_sec:      int  = 60
        self.max_trades:        int  = 20
        self.entry_cutoff_hhmm: int  = 1555

        # Session state
        self._session_pnl:       float = 0.0
        self._trades_today:      int   = 0
        self._last_trade_ts:     float = 0.0
        self._session_date:      str   = ""
        self._in_trade:          bool  = False
        self._immediate_pending: bool  = False

    # ------------------------------------------------------------------
    def set_config(
        self,
        enabled=False, symbol="",
        dollar_per_trade=200.0, session_budget=500.0,
        target_pct=0.50, stop_pct=1.50,
        use_rr_ratio=True, rr_ratio=2.0,
        immediate_mode=True,
        screener_mode=False,
        breakout_bars=5, candle_minutes=1, min_vol_mult=1.5,
        rsi_min=40.0, rsi_max=70.0,
        require_vwap=True, require_ema=True,
        cooldown_sec=60, max_trades=20, entry_cutoff_hhmm=1555,
    ):
        self.enabled            = enabled
        self.symbol             = symbol.upper().strip() if symbol else ""
        self.dollar_per_trade   = max(1.0, dollar_per_trade)
        self.session_budget     = max(1.0, session_budget)
        self.target_pct         = target_pct
        self.stop_pct           = stop_pct
        self.use_rr_ratio       = use_rr_ratio
        self.rr_ratio           = rr_ratio
        self.immediate_mode     = immediate_mode
        self.screener_mode      = screener_mode
        self.breakout_bars      = max(2, breakout_bars)
        self.candle_minutes     = max(1, int(candle_minutes))
        self.min_vol_mult       = min_vol_mult
        self.rsi_min            = rsi_min
        self.rsi_max            = rsi_max
        self.require_vwap       = require_vwap
        self.require_ema        = require_ema
        self.cooldown_sec       = max(0, cooldown_sec)
        self.max_trades         = max(1, max_trades)
        self.entry_cutoff_hhmm  = entry_cutoff_hhmm

    # ------------------------------------------------------------------
    def _reset_if_new_day(self):
        today = _now_et().date().isoformat()
        if self._session_date != today:
            self._session_pnl   = 0.0
            self._trades_today  = 0
            self._last_trade_ts = 0.0
            self._in_trade      = False
            self._session_date  = today

    def arm_immediate_entry(self):
        """Engine calls this on start() when scalp is enabled."""
        if self.enabled and self.symbol:
            self._immediate_pending = True
            log_message(f"[SCALP] Armed — will enter {self.symbol} on next poll")

    def mark_trade_closed(self, pnl: float):
        self._session_pnl  += pnl
        self._trades_today += 1
        self._in_trade      = False
        self._last_trade_ts = time.time()
        log_message(
            f"[SCALP] Closed trade #{self._trades_today} pnl=${pnl:+.2f} "
            f"session=${self._session_pnl:+.2f} / budget=${self.session_budget:.2f}"
        )

    def get_session_stats(self) -> dict:
        remaining = max(0.0, self.session_budget + self._session_pnl)
        return {
            "session_pnl":      round(self._session_pnl, 2),
            "trades_today":     self._trades_today,
            "budget_remaining": round(remaining, 2),
            "session_budget":   self.session_budget,
            "dollar_per_trade": self.dollar_per_trade,
            "exhausted":        self._session_pnl <= -self.session_budget,
            "in_trade":         self._in_trade,
            "symbol":           self.symbol,
        }

    # ------------------------------------------------------------------
    def _calc_position(
        self, limit_price: float, account_size: float
    ) -> Tuple[int, float, float, float]:
        """
        Returns (shares, stop_price, target_price, risk_dollars).
        shares = floor(dollar_per_trade / limit_price), capped at 25% of account.
        """
        if limit_price <= 0:
            return 0, 0.0, 0.0, 0.0
        shares = max(1, int(self.dollar_per_trade / limit_price))
        shares = min(shares, max(1, int(account_size * 0.25 / limit_price)))

        stop_dist  = limit_price * (self.stop_pct / 100.0)
        stop_price = round(limit_price - stop_dist, 4)
        target     = (round(limit_price + stop_dist * self.rr_ratio, 4)
                      if self.use_rr_ratio
                      else round(limit_price * (1 + self.target_pct / 100.0), 4))
        risk       = round(shares * stop_dist, 2)
        return shares, stop_price, target, risk

    def _build_signal(
        self, symbol, entry, stop, target, shares, risk, vwap, rel_vol, reason
    ) -> TradeSignal:
        return TradeSignal(
            symbol=symbol, direction="LONG",
            entry_price=entry, stop_price=stop,
            target_r2=target,
            target_r3=round(entry + (entry - stop) * max(self.rr_ratio, 2) * 1.5, 4),
            shares=shares, risk_dollars=risk,
            orb_high=target, orb_low=stop,
            vwap=vwap, rel_vol=rel_vol, gap_pct=0.0,
            reason=reason,
        )

    @staticmethod
    def _resample(candles: list, minutes: int) -> list:
        if minutes <= 1 or not candles:
            return candles
        from collections import defaultdict
        iv = minutes * 60 * 1000
        groups: dict = defaultdict(list)
        for c in candles:
            groups[(c.get("datetime", 0) // iv) * iv].append(c)
        result = []
        for key in sorted(groups):
            b = groups[key]
            result.append({"datetime": b[0].get("datetime", 0),
                           "open": b[0]["open"], "high": max(x["high"] for x in b),
                           "low": min(x["low"] for x in b), "close": b[-1]["close"],
                           "volume": sum(x.get("volume", 0) for x in b)})
        return result

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def evaluate_screener_result(
        self,
        candidate:      dict,
        open_positions: dict,
        account_size:   float = 7111.0,
    ) -> "Optional[TradeSignal]":
        """
        Generate a trade signal from a screener candidate.

        Entry:  candidate price (current market price)
        Stop:   1.5% below entry (fixed)
        Target: 0.3% below the intraday session high (previous resistance)
        The screener guarantees daily_uptrend + swing_low_touch already passed.
        """
        self._reset_if_new_day()

        symbol = candidate.get("symbol", "")
        price  = float(candidate.get("price") or 0)
        if not symbol or price <= 0:
            return None
        if not candidate.get("ready"):
            log_message(
                f"[SCALP-SCREEN] {symbol} skipped — "
                f"{candidate.get('passed', 0)}/{candidate.get('total', 6)} pillars "
                f"({candidate.get('score_pct', 0)}%), need 95%"
            )
            return None

        # Global guards
        if symbol in open_positions:
            return None
        if self._session_pnl <= -self.session_budget:
            return None
        if self._trades_today >= self.max_trades:
            return None
        if time.time() - self._last_trade_ts < self.cooldown_sec:
            return None
        now = _now_et()
        if now.hour * 100 + now.minute >= self.entry_cutoff_hhmm:
            return None

        stop_dist  = price * (self.stop_pct / 100.0)  # one stop: engine scalp_stop_pct
        stop_price = round(price - stop_dist, 4)

        # Target = just below the intraday session high (0.3% buffer)
        session_high = float(candidate.get("session_high") or 0)
        if session_high > price:
            target = round(session_high * 0.997, 4)
        else:
            # Fallback: 2:1 R:R when session high is not above entry
            target = round(price + stop_dist * 2.0, 4)

        # Minimum viable trade: target must yield at least 1:1 R:R
        if target - price < stop_dist:
            log_message(
                f"[SCALP-SCREEN] {symbol} skipped — target gap {target - price:.4f} "
                f"< stop {stop_dist:.4f} (not enough room)"
            )
            return None

        shares = max(1, int(self.dollar_per_trade / price))
        shares = min(shares, max(1, int(account_size * 0.25 / price)))
        risk   = round(shares * stop_dist, 2)

        sup   = candidate.get("support")
        score = candidate.get("score", 0)
        reason = (
            f"SCALP SWING-LOW {symbol} @ {price:.4f} "
            f"stop={stop_price:.4f}(-{self.stop_pct}%) tgt={target:.4f} "
            f"sh={shares} support={sup} "
            f"pillars={candidate.get('passed')}/{candidate.get('total')} "
            f"({candidate.get('score_pct')}%)"
        )
        log_message(
            f"[SCALP] SWING-LOW ENTRY {symbol} @ {price:.4f} "
            f"stop={stop_price:.4f} tgt={target:.4f} sh={shares} risk=${risk:.2f}"
        )
        self._in_trade = True
        return self._build_signal(
            symbol, price, stop_price, target, shares, risk,
            candidate.get("vwap"), 0.0, reason,
        )

    def evaluate(
        self,
        symbol:         str,
        indicators:     dict,
        open_positions: dict,
        current_price:  float,
        account_size:   float = 7111.0,
        ask_price:      float = 0.0,
    ) -> Optional[TradeSignal]:
        if not self.enabled or symbol != self.symbol:
            return None

        self._reset_if_new_day()

        # Guards
        if self._in_trade or symbol in open_positions:
            return None
        if self._session_pnl <= -self.session_budget:
            return None
        if self._trades_today >= self.max_trades:
            return None
        if time.time() - self._last_trade_ts < self.cooldown_sec:
            return None
        now = _now_et()
        hm  = now.hour * 100 + now.minute
        if hm >= self.entry_cutoff_hhmm:
            return None

        limit = ask_price if ask_price > 0 else current_price
        shares, stop, target, risk = self._calc_position(limit, account_size)
        if shares <= 0:
            return None

        vwap    = indicators.get("vwap")
        rel_vol = float(indicators.get("breakout_vol_ratio") or 0.0)

        # ── IMMEDIATE mode ────────────────────────────────────────────────
        if self.immediate_mode:
            if not self._immediate_pending:
                return None   # waiting for arm_immediate_entry() to be called
            self._immediate_pending = False
            self._in_trade = True
            reason = (f"SCALP IMMEDIATE {symbol} @ {limit:.4f} "
                      f"stop={stop:.4f}(-{self.stop_pct}%) tgt={target:.4f} "
                      f"sh={shares} capital=${round(shares*limit,2):.2f}")
            log_message(f"[SCALP] IMMEDIATE BUY {symbol} @ {limit:.4f} "
                        f"stop={stop:.4f} tgt={target:.4f} sh={shares} risk=${risk:.2f}")
            return self._build_signal(symbol, limit, stop, target, shares, risk, vwap, rel_vol, reason)

        # ── SIGNAL mode (breakout conditions) ────────────────────────────
        candles = self._resample(indicators.get("candles", []), self.candle_minutes)
        ema9, ema20 = indicators.get("ema9"), indicators.get("ema20")
        rsi  = indicators.get("rsi")
        if len(candles) < self.breakout_bars + 2:
            return None
        if self.require_vwap and vwap and current_price < vwap:
            return None
        if self.require_ema and (ema9 is None or ema20 is None or ema9 <= ema20):
            return None
        if rsi is not None and not (self.rsi_min <= rsi <= self.rsi_max):
            return None
        highs = [c["high"] for c in candles[-(self.breakout_bars + 1):-1]]
        if not highs or current_price <= max(highs):
            return None

        self._in_trade = True
        reason = (f"SCALP BREAKOUT {symbol} @ {limit:.4f} "
                  f"stop={stop:.4f} tgt={target:.4f} sh={shares}")
        log_message(f"[SCALP] SIGNAL {symbol} @ {limit:.4f} stop={stop:.4f} tgt={target:.4f}")
        return self._build_signal(symbol, limit, stop, target, shares, risk, vwap, rel_vol, reason)
