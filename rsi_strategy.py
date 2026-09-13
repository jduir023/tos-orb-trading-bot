"""
rsi_strategy.py
RSI Mean Reversion Strategy — Phase 1.
Inherits from StrategyBase. Uses daily RSI to identify oversold dips
within confirmed 2-week uptrends, then manages the position via
RSI-level exits and an optional trailing stop.
"""
from __future__ import annotations
import time
from typing import Any, Dict, List, Optional

from strategy_base import StrategyBase
from orb_strategy import TradeSignal
from rsi_scanner import RSIScanner
from utils import log_message, now_et as _now_et


class RSIStrategy(StrategyBase):
    name        = "RSI Mean Reversion"
    strategy_id = "rsi"
    tab_id      = "rsi"
    description = "2-week uptrend stocks with daily RSI \u226420. Exits on RSI recovery 30\u201340."

    def __init__(self, client=None) -> None:
        super().__init__()
        self.client  = client
        self.scanner: Optional[RSIScanner] = RSIScanner(client) if client else None

        # Config — all mirrored as engine attributes (rsi_*)
        self.account_size:          float = 10_000.0
        self.risk_pct:              float = 1.0
        self.rsi_entry_threshold:   float = 20.0
        self.rsi_partial_exit:      float = 30.0
        self.rsi_runner_exit:       float = 40.0
        self.initial_stop_pct:      float = 3.0
        self.trailing_stop_pct:     float = 5.0
        self.trail_trigger_pct:     float = 2.0
        self.max_hold_days:         int   = 10
        self.scan_interval_sec:     float = 300.0
        self.min_avg_vol:           int   = 300_000
        self.min_price:             float = 2.0
        self.max_price:             float = 500.0
        self.max_trades_per_day:    int   = 3
        self.max_concurrent:        int   = 3

        # Per-day state
        self._candidates:   List[Dict]       = []
        self._last_scan:    float            = 0.0
        self._triggered:    Dict[str, bool]  = {}
        self._trade_date:   str              = ""
        self._trades_today: int              = 0

        # RSI-specific position state (separate from ORB shared book)
        # symbol -> {entry, stop, stop_initial, peak, trailing_active, partial_done, entry_time}
        self._rsi_pos_state: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # StrategyBase interface
    # ------------------------------------------------------------------

    def set_config(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        if self.client and self.scanner is None:
            self.scanner = RSIScanner(self.client)
        if self.scanner:
            self.scanner.min_avg_vol = self.min_avg_vol
            self.scanner.min_price   = self.min_price
            self.scanner.max_price   = self.max_price
            self.scanner.rsi_entry   = self.rsi_entry_threshold
            self.scanner.rsi_watch   = max(self.rsi_entry_threshold + 20.0, 40.0)
            self.scanner.timeframe   = getattr(self, "rsi_timeframe", "daily")

    def reset_day(self) -> None:
        today = _now_et().date().isoformat()
        if self._trade_date != today:
            self._trade_date    = today
            self._trades_today  = 0
            self._triggered     = {}
            log_message("[RSI] Daily state reset.")

    def evaluate(
        self,
        symbol:        str,
        candidate:     Optional[Dict] = None,
        current_price: float          = 0.0,
        **kwargs,
    ) -> Optional[TradeSignal]:
        if not self.enabled:
            return None
        self.reset_day()

        if not candidate:
            candidate = next((c for c in self._candidates if c["symbol"] == symbol), None)
        if not candidate or not candidate.get("ready"):
            return None
        if self._triggered.get(symbol):
            return None
        if self._trades_today >= self.max_trades_per_day:
            return None
        if len(self._rsi_pos_state) >= self.max_concurrent:
            return None

        entry = current_price if current_price > 0 else candidate["last"]
        if entry <= 0:
            return None

        stop      = round(entry * (1.0 - self.initial_stop_pct / 100.0), 4)
        risk_dist = entry - stop
        if risk_dist <= 0:
            return None

        risk_dollars = self.account_size * (self.risk_pct / 100.0)
        shares       = max(1, int(risk_dollars / risk_dist))
        shares       = min(shares, max(1, int(self.account_size * 0.25 / entry)))

        # Indicative targets (RSI-level exits are the real gates)
        target_partial = round(entry + risk_dist * 2.0, 4)
        target_runner  = round(entry + risk_dist * 3.0, 4)

        self._triggered[symbol]    = True
        self._trades_today        += 1
        self._rsi_pos_state[symbol] = {
            "entry":            entry,
            "stop":             stop,
            "stop_initial":     stop,
            "peak":             entry,
            "partial_done":     False,
            "trailing_active":  False,
            "entry_time":       time.time(),
        }

        reason = (
            f"RSI MeanRev {symbol} entry={entry} stop={stop} "
            f"rsi={candidate.get('rsi')} pillars={candidate.get('passed')}/{candidate.get('total')} "
            f"({candidate.get('score_pct')}%)"
        )
        log_message(f"[RSI] SIGNAL: {reason}")
        return TradeSignal(
            symbol=symbol,
            direction="LONG",
            entry_price=entry,
            stop_price=stop,
            target_r2=target_partial,
            target_r3=target_runner,
            shares=shares,
            risk_dollars=round(shares * risk_dist, 2),
            orb_high=target_partial,
            orb_low=stop,
            vwap=None,
            rel_vol=0,
            gap_pct=0,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # RSI-specific position management
    # ------------------------------------------------------------------

    def manage_position(
        self,
        symbol:     str,
        price:      float,
        daily_rsi:  Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Called by the engine each poll for every RSI-tagged position.
        Returns an action dict or None.
          {"action": "close",   "reason": str}
          {"action": "partial", "shares": int, "reason": str}
        """
        ps = self._rsi_pos_state.get(symbol)
        if not ps:
            return None

        if price > ps["peak"]:
            ps["peak"] = price

        # Hard stop
        if price <= ps["stop"]:
            return {"action": "close", "reason": "stop_hit"}

        # Max hold days
        held_days = (time.time() - ps["entry_time"]) / 86400
        if held_days >= self.max_hold_days:
            return {"action": "close", "reason": "max_hold"}

        gain_pct = (price - ps["entry"]) / ps["entry"] * 100

        # Trailing stop upgrade
        if gain_pct >= self.trail_trigger_pct and not ps["trailing_active"]:
            trail = round(price * (1.0 - self.trailing_stop_pct / 100.0), 4)
            if trail > ps["stop"]:
                ps["stop"]            = trail
                ps["trailing_active"] = True
                log_message(f"[RSI] {symbol} trailing stop → ${trail:.4f}")

        if ps["trailing_active"]:
            new_trail = round(ps["peak"] * (1.0 - self.trailing_stop_pct / 100.0), 4)
            if new_trail > ps["stop"]:
                ps["stop"] = new_trail
            if price <= ps["stop"]:
                return {"action": "close", "reason": "trailing_stop"}

        # RSI-level exits (daily RSI)
        if daily_rsi is not None:
            if not ps["partial_done"] and daily_rsi >= self.rsi_partial_exit:
                ps["partial_done"] = True
                ps["stop"]         = ps["entry"]   # move stop to breakeven
                return {"action": "partial", "reason": "rsi_partial"}
            if ps["partial_done"] and daily_rsi >= self.rsi_runner_exit:
                return {"action": "close", "reason": "rsi_runner"}

        return None

    def record_partial(self, symbol: str) -> None:
        ps = self._rsi_pos_state.get(symbol)
        if ps:
            ps["partial_done"] = True

    def record_close(self, symbol: str) -> None:
        self._rsi_pos_state.pop(symbol, None)

    # ------------------------------------------------------------------
    # Scanner integration
    # ------------------------------------------------------------------

    def scan_candidates(self, watchlist: List[str] = None, max_workers: int = 8) -> List[Dict]:
        if not self.scanner:
            return []
        universe: List[str] = list(watchlist or [])
        if hasattr(self.client, "get_movers"):
            for direction in ("up", "down"):
                try:
                    universe.extend(self.client.get_movers(direction=direction))
                except Exception:
                    pass
        seen: set = set()
        symbols = [s for s in universe if not (s in seen or seen.add(s))]  # type: ignore
        self._candidates = self.scanner.scan(symbols, max_workers=max_workers)
        self._last_scan  = time.time()
        return self._candidates

    def get_daily_rsi(self, symbol: str) -> Optional[float]:
        if not self.scanner:
            return None
        return self.scanner.get_cached_rsi(symbol)

    # ------------------------------------------------------------------
    # Tab data
    # ------------------------------------------------------------------

    def get_tab_data(self) -> Dict[str, Any]:
        data = super().get_tab_data()
        data["candidates"] = self._candidates
        data["last_scan"]  = self._last_scan
        data["open_count"] = len(self._rsi_pos_state)
        data["config"] = {
            "rsi_entry_threshold": self.rsi_entry_threshold,
            "rsi_partial_exit":    self.rsi_partial_exit,
            "rsi_runner_exit":     self.rsi_runner_exit,
            "initial_stop_pct":    self.initial_stop_pct,
            "trailing_stop_pct":   self.trailing_stop_pct,
            "trail_trigger_pct":   self.trail_trigger_pct,
            "max_hold_days":       self.max_hold_days,
            "scan_interval_sec":   self.scan_interval_sec,
            "min_avg_vol":         self.min_avg_vol,
            "min_price":           self.min_price,
            "max_price":           self.max_price,
        }
        return data

    def get_candidates(self) -> List[Dict]:
        return list(self._candidates)
