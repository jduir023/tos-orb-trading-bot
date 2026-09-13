"""
Local paper book. Live Schwab data for marks; never posts to the broker.

Starting cash is $5,000. Positions, cash, and fills persist in
saved_data/paper_account.json — not config.json.
"""
from __future__ import annotations
import datetime
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from utils import log_message, now_et


def _iso(ts: Optional[float] = None) -> str:
    t = float(ts if ts is not None else time.time())
    return datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

PAPER_STARTING_CASH = 5000.00


def _today() -> str:
    return now_et().date().isoformat()


class PaperAccount:
    def __init__(self, path: str, starting_cash: float = PAPER_STARTING_CASH) -> None:
        self.path = path
        self.starting_cash = float(starting_cash)
        self.cash = float(starting_cash)
        self.equity_positions: Dict[str, Dict[str, Any]] = {}
        self.option_positions: Dict[str, Dict[str, Any]] = {}
        self.fills: List[Dict[str, Any]] = []
        self.closed_journal: List[Dict[str, Any]] = []
        self.realized_pnl = 0.0
        self._lock = threading.Lock()
        self._load()

    def reset(self, starting_cash: float = PAPER_STARTING_CASH) -> None:
        with self._lock:
            self.starting_cash = float(starting_cash)
            self.cash = float(starting_cash)
            self.equity_positions = {}
            self.option_positions = {}
            self.fills = []
            self.realized_pnl = 0.0
            # Keep closed_journal — paper calendar/journal history survives a cash reset.
            self._save_unlocked()
        log_message(f"[PAPER] book reset to ${self.starting_cash:,.2f} (journal history kept)")

    def can_debit(self, dollars: float) -> bool:
        return dollars > 0 and self.cash + 1e-9 >= dollars

    def buy_equity(self, symbol: str, qty: int, price: float, strategy_id: str = "") -> Optional[str]:
        symbol = (symbol or "").upper()
        qty = int(qty)
        price = float(price)
        if not symbol or qty < 1 or price <= 0:
            return None
        cost = qty * price
        with self._lock:
            if self.cash + 1e-9 < cost:
                log_message(f"[PAPER] skip BUY {qty} {symbol} @ {price:.4f} — cash ${self.cash:.2f} < ${cost:.2f}")
                return None
            self.cash -= cost
            pos = self.equity_positions.get(symbol)
            now = time.time()
            oid = "PAPER-" + uuid.uuid4().hex[:10]
            if pos:
                new_qty = pos["qty"] + qty
                pos["avg"] = (pos["avg"] * pos["qty"] + cost) / new_qty
                pos["qty"] = new_qty
            else:
                self.equity_positions[symbol] = {
                    "symbol": symbol, "qty": qty, "avg": price, "strategy_id": strategy_id,
                    "entry_time": now, "entry_oid": oid,
                }
            self._fill("BUY", symbol, qty, price, oid, strategy_id, multiplier=1, cost=cost)
            self._save_unlocked()
            log_message(f"[PAPER] BUY {qty} {symbol} @ {price:.4f} cost ${cost:.2f} cash ${self.cash:.2f} id={oid}")
            return oid

    def sell_equity(self, symbol: str, qty: int, price: float, strategy_id: str = "") -> Optional[str]:
        symbol = (symbol or "").upper()
        qty = int(qty)
        price = float(price)
        if not symbol or qty < 1 or price <= 0:
            return None
        with self._lock:
            pos = self.equity_positions.get(symbol)
            if not pos or pos["qty"] < 1:
                log_message(f"[PAPER] skip SELL {symbol} — no paper shares")
                return None
            qty = min(qty, int(pos["qty"]))
            proceeds = qty * price
            pnl = (price - float(pos["avg"])) * qty
            self.cash += proceeds
            self.realized_pnl += pnl
            pos["qty"] -= qty
            if pos["qty"] <= 0:
                self.equity_positions.pop(symbol, None)
            oid = "PAPER-X-" + uuid.uuid4().hex[:8]
            self._fill("SELL", symbol, qty, price, oid, strategy_id, multiplier=1, cost=-proceeds, pnl=pnl)
            self._record_closed(
                symbol=symbol, qty=qty, entry=float(pos["avg"]), exit_px=price, pnl=pnl,
                entry_time=pos.get("entry_time") or time.time(), exit_time=time.time(),
                entry_oid=pos.get("entry_oid") or "", exit_oid=oid,
                strategy_id=strategy_id or pos.get("strategy_id") or "",
                asset="EQUITY",
            )
            self._save_unlocked()
            log_message(f"[PAPER] SELL {qty} {symbol} @ {price:.4f} pnl ${pnl:.2f} cash ${self.cash:.2f} id={oid}")
            return oid

    def buy_option(self, occ: str, qty: int, price: float, underlying: str = "", strategy_id: str = "opt_confirm") -> Optional[str]:
        occ = (occ or "").replace(" ", "")
        qty = int(qty)
        price = float(price)
        if not occ or qty < 1 or price <= 0:
            return None
        cost = qty * price * 100.0
        with self._lock:
            if self.cash + 1e-9 < cost:
                log_message(f"[PAPER] skip BTO {qty} {occ} @ {price:.2f} — cash ${self.cash:.2f} < ${cost:.2f}")
                return None
            self.cash -= cost
            pos = self.option_positions.get(occ)
            now = time.time()
            oid = "PAPER-" + uuid.uuid4().hex[:10]
            if pos:
                new_qty = pos["qty"] + qty
                pos["avg"] = (pos["avg"] * pos["qty"] + price * qty) / new_qty
                pos["qty"] = new_qty
            else:
                self.option_positions[occ] = {
                    "occ": occ, "symbol": (underlying or "").upper(), "qty": qty,
                    "avg": price, "strategy_id": strategy_id,
                    "entry_time": now, "entry_oid": oid,
                }
            self._fill("BUY_TO_OPEN", occ, qty, price, oid, strategy_id, multiplier=100, cost=cost)
            self._save_unlocked()
            log_message(f"[PAPER] BTO {qty} {occ} @ {price:.2f} cost ${cost:.2f} cash ${self.cash:.2f} id={oid}")
            return oid

    def sell_option(self, occ: str, qty: int, price: float, strategy_id: str = "opt_confirm") -> Optional[str]:
        occ = (occ or "").replace(" ", "")
        qty = int(qty)
        price = float(price)
        if not occ or qty < 1 or price <= 0:
            return None
        with self._lock:
            pos = self.option_positions.get(occ)
            if not pos or pos["qty"] < 1:
                log_message(f"[PAPER] skip STC {occ} — no paper contracts")
                return None
            qty = min(qty, int(pos["qty"]))
            proceeds = qty * price * 100.0
            pnl = (price - float(pos["avg"])) * qty * 100.0
            self.cash += proceeds
            self.realized_pnl += pnl
            pos["qty"] -= qty
            if pos["qty"] <= 0:
                self.option_positions.pop(occ, None)
            oid = "PAPER-X-" + uuid.uuid4().hex[:8]
            und = (pos.get("symbol") or "").upper()
            self._fill("SELL_TO_CLOSE", occ, qty, price, oid, strategy_id, multiplier=100, cost=-proceeds, pnl=pnl)
            self._record_closed(
                symbol=und or occ, qty=qty, entry=float(pos["avg"]), exit_px=price, pnl=pnl,
                entry_time=pos.get("entry_time") or time.time(), exit_time=time.time(),
                entry_oid=pos.get("entry_oid") or "", exit_oid=oid,
                strategy_id=strategy_id or pos.get("strategy_id") or "opt_confirm",
                asset="OPTION", occ=occ,
            )
            self._save_unlocked()
            log_message(f"[PAPER] STC {qty} {occ} @ {price:.2f} pnl ${pnl:.2f} cash ${self.cash:.2f} id={oid}")
            return oid

    def snapshot(self, marks: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        marks = marks or {}
        with self._lock:
            unreal = 0.0
            holdings = []
            for sym, pos in self.equity_positions.items():
                mark = float(marks.get(sym, pos["avg"]) or pos["avg"])
                mv = mark * pos["qty"]
                u = (mark - pos["avg"]) * pos["qty"]
                unreal += u
                holdings.append({**pos, "mark": mark, "unrealized": round(u, 2), "market_value": round(mv, 2), "asset": "EQUITY"})
            for occ, pos in self.option_positions.items():
                mark = float(marks.get(occ, pos["avg"]) or pos["avg"])
                mv = mark * pos["qty"] * 100.0
                u = (mark - pos["avg"]) * pos["qty"] * 100.0
                unreal += u
                holdings.append({**pos, "mark": mark, "unrealized": round(u, 2), "market_value": round(mv, 2), "asset": "OPTION"})
            day_real = sum(float(f.get("pnl") or 0) for f in self.fills if f.get("day") == _today() and f.get("pnl") is not None)
            cash = self.cash
            start = self.starting_cash
            realized = self.realized_pnl
        equity = cash + sum(h["market_value"] for h in holdings)
        return {
            "paper": True,
            "starting_cash": round(start, 2),
            "cash": round(cash, 2),
            "cash_available": round(cash, 2),
            "equity": round(equity, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unreal, 2),
            "day_pnl": round(day_real + unreal, 2),
            "day_realized": round(day_real, 2),
            "holdings": holdings,
            "fills_today": sum(1 for f in self.fills if f.get("day") == _today()),
        }

    def _record_closed(
        self, *, symbol, qty, entry, exit_px, pnl, entry_time, exit_time,
        entry_oid, exit_oid, strategy_id, asset, occ="",
    ) -> None:
        entry = float(entry)
        exit_px = float(exit_px)
        qty = int(qty)
        pnl = float(pnl)
        pnl_pct = round((exit_px / entry - 1) * 100, 2) if entry else 0.0
        if asset == "OPTION" and entry:
            pnl_pct = round((exit_px - entry) / entry * 100, 2)
        self.closed_journal.append({
            "symbol": symbol,
            "occ": occ,
            "asset": asset,
            "direction": "LONG",
            "qty": qty,
            "entry_price": round(entry, 4),
            "exit_price": round(exit_px, 4),
            "pnl": round(pnl, 2),
            "pnl_pct": pnl_pct,
            "entry_time": _iso(entry_time),
            "exit_time": _iso(exit_time),
            "winner": pnl > 0,
            "entry_order_ids": [entry_oid] if entry_oid else [],
            "exit_order_id": exit_oid,
            "strategy_id": strategy_id,
            "entry_reason": strategy_id or "",
            "source": "Paper",
            "paper": True,
        })
        self.closed_journal = self.closed_journal[-2000:]

    def journal_rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.closed_journal)

    def _rebuild_closed_from_fills(self) -> None:
        from collections import defaultdict, deque
        lots: Dict[str, deque] = defaultdict(deque)
        for f in self.fills:
            side = f.get("side") or ""
            key = f.get("symbol") or ""
            if side in ("BUY", "BUY_TO_OPEN"):
                lots[key].append(dict(f))
            elif side in ("SELL", "SELL_TO_CLOSE") and key:
                remaining = int(f.get("qty") or 0)
                while remaining > 0 and lots[key]:
                    lot = lots[key][0]
                    take = min(remaining, int(lot.get("qty") or 0))
                    if take <= 0:
                        lots[key].popleft()
                        continue
                    mult = float(lot.get("multiplier") or f.get("multiplier") or 1)
                    entry = float(lot.get("price") or 0)
                    exit_px = float(f.get("price") or 0)
                    pnl = (exit_px - entry) * take * mult
                    asset = "OPTION" if mult >= 100 or "TO_" in side else "EQUITY"
                    self._record_closed(
                        symbol=key, qty=take, entry=entry, exit_px=exit_px, pnl=pnl,
                        entry_time=lot.get("time"), exit_time=f.get("time"),
                        entry_oid=lot.get("order_id") or "", exit_oid=f.get("order_id") or "",
                        strategy_id=lot.get("strategy_id") or f.get("strategy_id") or "",
                        asset=asset, occ=key if asset == "OPTION" else "",
                    )
                    remaining -= take
                    lot["qty"] = int(lot.get("qty") or 0) - take
                    if int(lot.get("qty") or 0) <= 0:
                        lots[key].popleft()

    def _fill(self, side, symbol, qty, price, oid, strategy_id, multiplier, cost, pnl=None) -> None:
        self.fills.append({
            "time": time.time(),
            "day": _today(),
            "side": side,
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "order_id": oid,
            "strategy_id": strategy_id,
            "multiplier": multiplier,
            "cost": cost,
            "pnl": pnl,
            "source": "paper",
        })
        self.fills = self.fills[-500:]

    def _load(self) -> None:
        try:
            raw = json.load(open(self.path, encoding="utf-8"))
        except Exception:
            return
        self.starting_cash = float(raw.get("starting_cash") or PAPER_STARTING_CASH)
        self.cash = float(raw.get("cash") if raw.get("cash") is not None else self.starting_cash)
        self.equity_positions = dict(raw.get("equity_positions") or {})
        self.option_positions = dict(raw.get("option_positions") or {})
        self.fills = list(raw.get("fills") or [])
        self.closed_journal = list(raw.get("closed_journal") or [])
        self.realized_pnl = float(raw.get("realized_pnl") or 0)
        if not self.closed_journal and self.fills:
            self._rebuild_closed_from_fills()

    def _save_unlocked(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            json.dump({
                "starting_cash": self.starting_cash,
                "cash": round(self.cash, 4),
                "realized_pnl": round(self.realized_pnl, 4),
                "equity_positions": self.equity_positions,
                "option_positions": self.option_positions,
                "fills": self.fills[-500:],
                "closed_journal": self.closed_journal[-2000:],
            }, open(self.path, "w", encoding="utf-8"), indent=2)
        except Exception as exc:
            log_message(f"[PAPER] save: {exc}")
