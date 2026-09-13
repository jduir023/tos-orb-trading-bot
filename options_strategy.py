"""
Automatic 2–5 DTE options trader.

Entry: UOA candidate at 95% pillars (6/6).
Exit (no confirmation):
  • +3.5% to +5% on the contract → market sell
  • If strong buy volume on the stock, hold until a test/retest of resistance
  • Always sell by +5% (hard cap) or 0 DTE near the close
"""
from __future__ import annotations
import json
import os
import time
from typing import Any, Dict, List, Optional

from utils import log_message, now_et


class OptionsStrategy:
    def __init__(self, client=None, data_dir: str = "saved_data") -> None:
        self.client = client
        self.path = os.path.join(data_dir, "option_positions.json")
        self.enabled = False
        self.min_dte = 2
        self.max_dte = 5
        self.min_profit_pct = 3.5
        self.max_profit_pct = 5.0
        self.strong_rvol = 4.0
        self.dollar_per_trade = 400.0
        self.max_contracts = 2
        self.max_concurrent = 2
        self.max_trades = 6
        self.emergency_loss_pct = -25.0
        self._positions: List[Dict[str, Any]] = []
        self._closed: List[Dict[str, Any]] = []
        self._triggered: Dict[str, float] = {}
        self._trades_today = 0
        self._day = ""
        self._load()

    def set_config(self, **kw) -> None:
        mapping = {
            "enabled": "enabled",
            "options_enabled": "enabled",
            "options_min_dte": "min_dte",
            "options_max_dte": "max_dte",
            "options_min_profit_pct": "min_profit_pct",
            "options_max_profit_pct": "max_profit_pct",
            "options_strong_rvol": "strong_rvol",
            "options_dollar_per_trade": "dollar_per_trade",
            "options_max_contracts": "max_contracts",
            "options_max_concurrent": "max_concurrent",
            "options_max_trades": "max_trades",
        }
        for src, dest in mapping.items():
            if src in kw and kw[src] is not None:
                setattr(self, dest, kw[src])
        self.enabled = bool(self.enabled)

    def reset_day(self) -> None:
        today = now_et().date().isoformat()
        if self._day != today:
            self._day = today
            self._trades_today = 0
            self._triggered = {}
            log_message("[OPT] Daily state reset.")

    def open_positions(self) -> List[Dict[str, Any]]:
        return list(self._positions)

    def closed_today(self) -> List[Dict[str, Any]]:
        return list(self._closed)

    def evaluate_entry(
        self,
        candidate: Dict[str, Any],
        *,
        dry_run: bool,
        auto_trade: bool,
        sr: Optional[Dict[str, Any]] = None,
        und_rvol: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        self.reset_day()
        if not self.enabled or not auto_trade:
            return None
        if not candidate.get("ready"):
            return None
        trade = candidate.get("trade") or {}
        osi = trade.get("osi")
        symbol = (candidate.get("symbol") or "").upper()
        if not osi or not symbol:
            return None
        if symbol in self._triggered:
            return None
        if any(p.get("symbol") == symbol or p.get("osi") == osi for p in self._positions):
            return None
        if len(self._positions) >= self.max_concurrent:
            return None
        if self._trades_today >= self.max_trades:
            return None
        dte = int(trade.get("dte") or 0)
        if dte < self.min_dte or dte > self.max_dte:
            return None
        mark = float(trade.get("mark") or trade.get("ask") or 0)
        if mark < 0.15:
            return None
        qty = max(1, min(self.max_contracts, int(self.dollar_per_trade / (mark * 100))))
        if qty < 1:
            return None

        if dry_run:
            order_id = f"DRY-OPT-{int(time.time())}"
            log_message(f"[OPT] DRY BUY_TO_OPEN {qty} {osi} @ {mark:.2f}")
        else:
            order_id = self.client.place_option_order(
                osi, qty, "BUY_TO_OPEN", limit_price=float(trade.get("ask") or mark) * 1.04
            )
            if not order_id:
                return None

        pos = {
            "symbol": symbol,
            "osi": osi,
            "type": trade.get("type"),
            "strike": trade.get("strike"),
            "expiry": trade.get("expiry"),
            "dte": dte,
            "qty": qty,
            "entry": mark,
            "entry_time": time.time(),
            "order_id": order_id,
            "mark": mark,
            "pnl_pct": 0.0,
            "strong_volume": False,
            "touched_res": False,
            "pulled_back": False,
            "support": (sr or {}).get("support"),
            "resistance": (sr or {}).get("resistance"),
            "status": "open",
            "dry_run": dry_run,
            "und_rvol": und_rvol,
        }
        self._positions.append(pos)
        self._triggered[symbol] = time.time()
        self._trades_today += 1
        self._save()
        log_message(
            f"[OPT] OPEN {symbol} {pos['type']} {pos['strike']} {dte}d  "
            f"{qty}x @ ${mark:.2f}  S={pos['support']} R={pos['resistance']}"
        )
        return pos

    def manage(
        self,
        quotes: Dict[str, Dict],
        *,
        dry_run: bool,
        sr_engine=None,
        rvol_map: Optional[Dict[str, float]] = None,
        strong_rvol: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Update marks and sell without confirmation when exit rules hit."""
        self.reset_day()
        exits: List[Dict[str, Any]] = []
        rvol_map = rvol_map or {}
        strong_cut = float(strong_rvol if strong_rvol is not None else self.strong_rvol)
        still = []
        for pos in self._positions:
            osi = pos["osi"]
            mark = self._mark(osi, quotes, pos)
            entry = float(pos.get("entry") or 0)
            pnl_pct = ((mark - entry) / entry * 100.0) if entry > 0 and mark > 0 else 0.0
            pos["mark"] = mark
            pos["pnl_pct"] = round(pnl_pct, 2)
            und = pos["symbol"]
            px = 0.0
            uq = quotes.get(und) or {}
            uqq = uq.get("quote") or uq
            px = float(uqq.get("lastPrice") or 0)
            rvol = float(rvol_map.get(und) or pos.get("und_rvol") or 0)
            strong = rvol >= strong_cut
            pos["strong_volume"] = strong
            lv = sr_engine.get(und) if sr_engine else None
            if lv:
                pos["support"] = lv.get("support")
                pos["resistance"] = lv.get("resistance")
            res = float(pos.get("resistance") or 0)
            if px > 0 and res > 0:
                near_r = abs(px - res) / res * 100 <= 0.45 or px >= res
                if near_r:
                    if pos.get("pulled_back"):
                        pos["retest"] = True
                    pos["touched_res"] = True
                elif pos.get("touched_res") and res > 0 and (res - px) / res * 100 >= 0.5:
                    pos["pulled_back"] = True

            reason = self._exit_reason(pos, pnl_pct, strong, px)
            if not reason:
                still.append(pos)
                continue
            ok = True
            if dry_run or pos.get("dry_run"):
                log_message(f"[OPT] DRY SELL_TO_CLOSE {pos['qty']} {osi} @ {mark:.2f} ({reason})")
            else:
                oid = self.client.place_option_order(
                    osi, int(pos["qty"]), "SELL_TO_CLOSE", market=True
                )
                if not oid:
                    log_message(f"[OPT] sell failed {osi} — will retry")
                    still.append(pos)
                    ok = False
            if ok:
                pos["status"] = "closed"
                pos["exit"] = mark
                pos["exit_reason"] = reason
                pos["exit_time"] = time.time()
                pos["pnl"] = round((mark - entry) * 100 * int(pos["qty"]), 2)
                self._closed.append(pos)
                exits.append(pos)
                log_message(
                    f"[OPT] CLOSE {und} {osi} {pnl_pct:+.2f}%  {reason}  pnl=${pos['pnl']:.2f}"
                )
        self._positions = still
        if exits:
            self._save()
        else:
            self._save()
        return exits

    def _exit_reason(self, pos: Dict[str, Any], pnl_pct: float, strong: bool, px: float) -> str:
        hm = now_et().hour * 100 + now_et().minute
        dte = int(pos.get("dte") or 0)
        if pnl_pct <= self.emergency_loss_pct:
            return f"emergency stop {pnl_pct:.1f}%"
        if dte <= 0 and hm >= 1500:
            return "0 DTE flatten"
        if pnl_pct >= self.max_profit_pct:
            return f"hard target {self.max_profit_pct:.1f}%"
        if strong:
            if pos.get("retest"):
                return "resistance retest + strong buy volume"
            if pos.get("touched_res") and pnl_pct >= self.min_profit_pct:
                return "resistance test + strong buy volume"
            return ""  # hold through 3.5% while volume is strong
        if pnl_pct >= self.min_profit_pct:
            return f"target {self.min_profit_pct:.1f}%"
        return ""

    def _mark(self, osi: str, quotes: Dict, pos: Dict) -> float:
        q = quotes.get(osi) or {}
        qq = q.get("quote") or q
        mark = float(qq.get("mark") or qq.get("lastPrice") or qq.get("last") or 0)
        if mark > 0:
            return mark
        bid = float(qq.get("bidPrice") or qq.get("bid") or 0)
        ask = float(qq.get("askPrice") or qq.get("ask") or 0)
        if bid and ask:
            return (bid + ask) / 2.0
        if self.client:
            oq = self.client.get_option_quote(osi) or {}
            oqq = oq.get("quote") or oq
            m = float(oqq.get("mark") or oqq.get("lastPrice") or 0)
            if m:
                return m
            b = float(oqq.get("bidPrice") or oqq.get("bid") or 0)
            a = float(oqq.get("askPrice") or oqq.get("ask") or 0)
            if b and a:
                return (b + a) / 2.0
        return float(pos.get("mark") or pos.get("entry") or 0)

    def tab_data(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "min_dte": self.min_dte,
            "max_dte": self.max_dte,
            "min_profit_pct": self.min_profit_pct,
            "max_profit_pct": self.max_profit_pct,
            "strong_rvol": self.strong_rvol,
            "dollar_per_trade": self.dollar_per_trade,
            "max_contracts": self.max_contracts,
            "max_concurrent": self.max_concurrent,
            "max_trades": self.max_trades,
            "open": self._positions,
            "closed": self._closed[-20:],
            "trades_today": self._trades_today,
        }

    def _load(self) -> None:
        try:
            data = json.load(open(self.path, encoding="utf-8"))
            self._positions = list(data.get("open") or [])
            self._closed = list(data.get("closed") or [])
        except Exception:
            self._positions = []
            self._closed = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            json.dump({"open": self._positions, "closed": self._closed[-80:]},
                      open(self.path, "w", encoding="utf-8"), indent=2)
        except Exception as exc:
            log_message(f"[OPT] save error: {exc}")
