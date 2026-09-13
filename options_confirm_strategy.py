"""
opt_confirm — 5-min confirmation options sleeve.

Long call/put only. Next Wed/Fri weekly. Stop on underlying 5-min close.
Default off. Never POST when dry_run.
"""
from __future__ import annotations
import datetime
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from strategy_base import StrategyBase
from contract_selector import listed_strike_ladder, next_expiry, parse_chain_contracts, select_contract
from level_engine import LevelEngine, take_profit_price
from options_universe import OptionsUniverse
from pillars import pack, pillar
from utils import log_message, now_et

_MARK_TTL = 3.0
_TICKET_STRUCT_TTL = 45.0
LADDER_EACH_SIDE = 8


def _occ_to_osi(occ: str) -> str:
    compact = str(occ or "").upper().replace(" ", "")
    m = re.match(r"^([A-Z0-9.\-]{1,6})(\d{6})([CP])(\d{8})$", compact)
    if m:
        root, ymd, cp, strike = m.groups()
        return f"{root:<6}{ymd}{cp}{strike}"
    return str(occ or "").upper()


def _option_px(raw: Optional[Dict[str, Any]]) -> tuple[float, float, float]:
    """bid, ask, mark from a Schwab option quote blob."""
    if not isinstance(raw, dict):
        return 0.0, 0.0, 0.0
    qq = raw.get("quote") if isinstance(raw.get("quote"), dict) else raw
    def _f(*keys: str) -> float:
        for k in keys:
            try:
                v = float(qq.get(k) or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
        return 0.0
    bid = _f("bidPrice", "bid")
    ask = _f("askPrice", "ask")
    last = _f("lastPrice", "last")
    mark = _f("mark")
    if mark <= 0 and bid > 0 and ask > 0:
        mark = (bid + ask) / 2.0
    if mark <= 0:
        mark = last or bid or ask
    return bid, ask, mark


class OptionsConfirmStrategy(StrategyBase):
    name = "Options Confirm"
    strategy_id = "opt_confirm"
    tab_id = "options"
    description = "5-min close through a level, next Wed/Fri weekly. Stop on the stock."

    def __init__(self, client=None, data_dir: str = "saved_data") -> None:
        super().__init__()
        self.client = client
        self.universe = OptionsUniverse(client)
        self.levels = LevelEngine()
        self.path = os.path.join(data_dir, "option_positions.json")
        self.expiry_mode = "wed_then_fri"
        self.max_premium = 150.0
        self.max_contracts = 2
        self.max_concurrent = 1
        self.strike_mode = "atm"
        self.eod_flat = True
        self.last_entry_hhmm = 1558
        self.options_dry_run = True
        self.paper = None
        self._positions: List[Dict[str, Any]] = []
        self._closed: List[Dict[str, Any]] = []
        self._universe: List[Dict[str, Any]] = []
        self._cards: Dict[str, Dict[str, Any]] = {}
        self._last_scan = 0.0
        self._entry_fail_until: Dict[str, float] = {}
        self._mark_ts = 0.0
        self._ticket: Dict[str, Any] = {}
        self._load()

    def set_config(self, **kwargs) -> None:
        mapping = {
            "use_options_confirm": "enabled",
            "options_enabled": "enabled",
            "options_dry_run": "options_dry_run",
            "options_expiry_mode": "expiry_mode",
            "options_max_premium": "max_premium",
            "options_max_contracts": "max_contracts",
            "options_max_concurrent": "max_concurrent",
            "options_strike": "strike_mode",
            "options_eod_flat": "eod_flat",
            "options_last_entry_hhmm": "last_entry_hhmm",
        }
        for src, dest in mapping.items():
            if src in kwargs and kwargs[src] is not None:
                setattr(self, dest, kwargs[src])
        self.enabled = bool(self.enabled)
        self.universe.set_config(**kwargs)
        self.universe.client = self.client

    def reset_day(self) -> None:
        pass

    def evaluate(self, symbol: str, **kwargs) -> Optional[Any]:
        return None  # engine calls scan/manage directly

    def scan_universe(self, candle_fn=None) -> List[Dict[str, Any]]:
        self._universe = self.universe.scan(self.expiry_mode)
        self._last_scan = time.time()
        if candle_fn:
            for row in self._universe:
                if not row.get("tradable"):
                    self._annotate(row, None)
                    continue
                try:
                    candles = candle_fn(row["symbol"]) or []
                except Exception:
                    candles = []
                ev = self.levels.evaluate(row["symbol"], candles, float(row.get("last") or 0))
                if ev:
                    prev = self._cards.get(row["symbol"]) or {}
                    if ev.get("card") and ev.get("card") != prev.get("card"):
                        log_message(f"[OPT-CARD]\n{ev['card']}")
                    self._cards[row["symbol"]] = ev
                self._annotate(row, ev)
        else:
            for row in self._universe:
                self._annotate(row, self._cards.get(row.get("symbol") or ""))
        return self._universe

    def _annotate(self, row: Dict[str, Any], ev: Optional[Dict[str, Any]]) -> None:
        ev = ev or {}
        skip = str(row.get("skip_reason") or "")
        call = row.get("call") or {}
        put = row.get("put") or {}
        weekly = bool(row.get("expiry")) and not skip.startswith("no chain") and "ATM" not in skip
        spread_ok = bool(row.get("tradable")) or (
            float(call.get("spread") or 99) <= 0.15 and float(put.get("spread") or 99) <= 0.15
        )
        oi_ok = float(call.get("oi") or 0) >= 500 and float(put.get("oi") or 0) >= 500
        liquid = "adv" not in skip and "price" not in skip and "no last" not in skip
        box_ok = bool(ev.get("card")) and ev.get("skip") != "need ~6 completed 5-min bars"
        confirm = bool(ev.get("ready") and ev.get("signal"))
        items = [
            pillar("weekly", "Wed/Fri weekly", weekly, row.get("expiry") or skip),
            pillar("spread", "Tight ATM book", bool(row.get("tradable")) or spread_ok,
                   f"{row.get('spread_pct') or 0:.1f}%"),
            pillar("oi", "Open interest", oi_ok,
                   f"c{int(call.get('oi') or 0)}/p{int(put.get('oi') or 0)}"),
            pillar("liquid", "Liquid underlying", liquid, skip or "ok"),
            pillar("box", "5-min box ready", box_ok, ev.get("skip") or "box"),
            pillar("confirm", "5-min close through", confirm, (ev.get("signal") or {}).get("side") or ev.get("skip") or ""),
        ]
        scored = pack(items)
        row.update(scored)
        row["ready"] = bool(row.get("tradable") and confirm)
        if ev:
            row["card"] = ev.get("card")
            row["last_5m_close"] = ev.get("last_5m_close")
            row["call_break"] = ev.get("call_break")
            row["put_break"] = ev.get("put_break")
            row["call_target"] = ev.get("call_target")
            row["put_target"] = ev.get("put_target")
            row["call_level"] = ev.get("call_level")
            row["put_level"] = ev.get("put_level")
            row["inside_box"] = ev.get("inside_box")
            row["level_skip"] = ev.get("skip")
            row["support"] = ev.get("put_break")
            row["resistance"] = ev.get("call_break")

    def open_positions(self) -> List[Dict[str, Any]]:
        return list(self._positions)

    def poll(
        self,
        candle_fn,
        *,
        auto_trade: bool,
        inherit_dry_run: bool,
    ) -> List[Dict[str, Any]]:
        """RTH poll: update cards, maybe enter, manage kills. Returns new events."""
        events: List[Dict[str, Any]] = []
        if not self.enabled:
            return events
        now = now_et()
        hm = now.hour * 100 + now.minute
        in_rth = (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and hm < 1600
        if not in_rth:
            return events
        dry = bool(self.options_dry_run or inherit_dry_run)
        # manage first
        still = []
        for pos in self._positions:
            self._arm_take_profit(pos)
            candles = []
            try:
                candles = candle_fn(pos["symbol"]) or []
            except Exception:
                candles = []
            reason = self.levels.kill(pos["symbol"], candles, pos)
            if reason:
                self._close(pos, reason, dry)
                events.append({"type": "close", "pos": pos, "reason": reason})
            else:
                still.append(pos)
        self._positions = still
        self._refresh_open_marks(force=True)
        self._save()

        if hm >= int(self.last_entry_hhmm):
            return events
        if len(self._positions) >= int(self.max_concurrent):
            return events

        held = {p["symbol"] for p in self._positions}
        for row in self._universe:
            if not row.get("tradable"):
                continue
            sym = row["symbol"]
            if sym in held:
                continue
            candles = []
            try:
                candles = candle_fn(sym) or []
            except Exception:
                continue
            ev = self.levels.evaluate(sym, candles, float(row.get("last") or 0))
            if ev:
                prev = self._cards.get(sym) or {}
                if ev.get("card") and ev.get("card") != prev.get("card"):
                    log_message(f"[OPT-CARD]\n{ev['card']}")
                self._cards[sym] = ev
                self._annotate(row, ev)
            if not ev or not ev.get("ready") or not ev.get("signal"):
                continue
            if time.time() < float(self._entry_fail_until.get(sym) or 0):
                continue
            sig = ev["signal"]
            expiry = next_expiry(self.expiry_mode)
            if not expiry:
                row["entry_skip"] = "no Wed/Fri expiry"
                continue
            contract = select_contract(
                self.client, sym, sig["side"], float(row.get("last") or 0),
                expiry.isoformat(), strike_mode=self.strike_mode,
                max_premium=self.max_premium, max_contracts=self.max_contracts,
            )
            if not contract:
                row["entry_skip"] = (
                    f"{sig['side']} ATM over ${self.max_premium:.0f} premium cap"
                )
                self._entry_fail_until[sym] = time.time() + 20
                log_message(f"[OPT] confirm {sym} {sig['side']} — no buy ({row['entry_skip']})")
                continue
            pos = self._enter(row, ev, sig, contract, dry=dry, auto_trade=auto_trade)
            if pos:
                self.levels.mark_used(sym, sig["side"], sig.get("break") or 0)
                row["entry_skip"] = ""
                events.append({"type": "open", "pos": pos})
                if len(self._positions) >= int(self.max_concurrent):
                    break
                continue
            row["entry_skip"] = row.get("entry_skip") or "paper fill refused"
            self._entry_fail_until[sym] = time.time() + 20
        return events

    def _enter(self, row, ev, sig, contract, *, dry: bool, auto_trade: bool) -> Optional[Dict]:
        occ = contract.get("occ") or contract.get("osi")
        qty = int(contract.get("qty") or 0)
        px = float(contract.get("limit") or contract.get("ask") or 0)
        if not occ or qty < 1 or px <= 0:
            return None
        if not auto_trade:
            log_message(f"[OPT] signal {sig['side']} {qty} {occ} @ {px:.2f} — paper auto-trade off")
            row["entry_skip"] = "paper auto-trade off"
            return None
        cost = qty * px * 100.0
        oid = None
        if self.paper:
            oid = self.paper.buy_option(occ, qty, px, underlying=row["symbol"], strategy_id="opt_confirm")
            if not oid:
                row["entry_skip"] = "paper cash short"
                return None
        else:
            oid = "PAPER-" + uuid.uuid4().hex[:10]
            log_message(f"[OPT] PAPER {sig['side']} {qty} {occ} LIMIT {px:.2f} cost ${cost:.2f} (no broker POST)")
        pos = {
            "symbol": row["symbol"],
            "side": sig["side"],
            "type": sig["side"],
            "occ": occ,
            "osi": occ,
            "strike": contract.get("strike"),
            "expiry": contract.get("expiry"),
            "dte": contract.get("dte"),
            "qty": qty,
            "entry": px,
            "bid": contract.get("bid"),
            "ask": contract.get("ask"),
            "break": sig["break"],
            "kill": sig["kill"],
            "level": sig.get("level") or sig.get("target"),
            "target": sig["target"],
            "card": ev.get("card"),
            "order_id": oid,
            "strategy_id": "opt_confirm",
            "entry_time": time.time(),
            "status": "open",
            "dry_run": True,
            "paper": True,
        }
        self._positions.append(pos)
        self._save()
        log_message(
            f"[OPT] OPEN {pos['symbol']} {pos['side']} {pos['expiry']} "
            f"{pos['strike']} x{qty} @ {px:.2f} id={oid} "
            f"sell 3/4 {pos['target']} (level {pos.get('level')})"
        )
        return pos

    def _arm_take_profit(self, pos: Dict) -> None:
        """Existing fills stored the full next level as target — rebase to 3/4."""
        side = str(pos.get("side") or pos.get("type") or "").upper()
        try:
            brk = float(pos.get("break") or 0)
            nxt = float(pos.get("level") or 0) or float(pos.get("target") or 0)
        except (TypeError, ValueError):
            return
        if brk <= 0 or nxt <= 0:
            return
        if not pos.get("level"):
            pos["level"] = nxt
        pos["target"] = take_profit_price(brk, float(pos.get("level") or nxt), side)

    def _close(self, pos: Dict, reason: str, dry: bool) -> None:
        occ = pos.get("occ") or pos.get("osi")
        qty = int(pos.get("qty") or 1)
        q = self.client.get_option_quote(occ) or {}
        qq = q.get("quote") or q
        bid = float(qq.get("bidPrice") or qq.get("bid") or 0)
        mark = float(qq.get("mark") or 0)
        sell_px = max(bid, (mark - 0.02) if mark else bid) or float(pos.get("entry") or 0)
        if self.paper:
            oid = self.paper.sell_option(occ, qty, sell_px, strategy_id="opt_confirm")
        else:
            oid = "PAPER-X-" + uuid.uuid4().hex[:8]
            log_message(f"[OPT] PAPER SELL_TO_CLOSE {qty} {occ} LIMIT {sell_px:.2f} ({reason}) no POST")
        pos["status"] = "closed"
        pos["exit_reason"] = reason
        pos["exit_order_id"] = oid
        pos["exit_time"] = time.time()
        pos["exit"] = sell_px
        pos["pnl"] = round((sell_px - float(pos.get("entry") or 0)) * qty * 100.0, 2)
        self._closed.append(pos)
        log_message(f"[OPT] CLOSE {pos['symbol']} {occ} {reason}")

    def _refresh_open_marks(self, force: bool = False) -> None:
        """Stamp live Schwab mark + unrealized P/L on open contracts. 2s cache."""
        if not self._positions:
            return
        now = time.time()
        if not force and (now - float(self._mark_ts or 0)) < _MARK_TTL:
            return
        client = self.client
        occs = [_occ_to_osi(p.get("occ") or p.get("osi") or "") for p in self._positions]
        batch: Dict[str, Any] = {}
        if client and occs:
            fn = getattr(client, "get_option_quotes", None)
            try:
                if callable(fn):
                    batch = fn(occs) or {}
                else:
                    for occ in occs:
                        q = client.get_option_quote(occ) or {}
                        if q:
                            batch[str(occ).replace(" ", "")] = q
            except Exception:
                batch = {}
        for pos in self._positions:
            occ = _occ_to_osi(pos.get("occ") or pos.get("osi") or "")
            compact = occ.replace(" ", "")
            raw = batch.get(compact) or batch.get(occ) or {}
            bid, ask, mark = _option_px(raw if isinstance(raw, dict) else {})
            if mark <= 0:
                mark = float(pos.get("mark") or pos.get("entry") or 0)
            entry = float(pos.get("entry") or 0)
            qty = int(pos.get("qty") or 1)
            pos["bid"] = round(bid, 4) if bid else 0.0
            pos["ask"] = round(ask, 4) if ask else 0.0
            pos["mark"] = round(mark, 4)
            pos["pnl"] = round((mark - entry) * qty * 100.0, 2) if entry else 0.0
            pos["pnl_pct"] = round(((mark - entry) / entry) * 100.0, 2) if entry else 0.0
            pos["mark_ts"] = now
        self._mark_ts = now

    def chain_ticket(self, symbol: str, expiry: str = "", rebuild: bool = False) -> Dict[str, Any]:
        """Expiry picker + 8-up / 8-down strike ladder with live bid/ask/mark."""
        sym = str(symbol or "").upper().strip()
        if not sym:
            return {"ok": False, "error": "symbol is required", "expiries": [], "strikes": []}
        exp = str(expiry or "")[:10]
        now = time.time()
        prev = self._ticket if self._ticket.get("symbol") == sym else {}
        same_exp = (not exp) or exp == str(prev.get("expiry") or "")
        fresh = (now - float(prev.get("struct_ts") or 0)) < _TICKET_STRUCT_TTL
        if rebuild or not prev or not same_exp or not fresh:
            built = self._build_ticket(sym, exp)
            if built.get("strikes"):
                self._ticket = built
            elif prev.get("strikes"):
                self._ticket = dict(prev)
                self._ticket["error"] = built.get("error") or prev.get("error")
            else:
                self._ticket = built
        return self._quote_ticket(self._ticket)

    def _build_ticket(self, sym: str, expiry: str) -> Dict[str, Any]:
        today = now_et().date()
        from_d = (today + datetime.timedelta(days=1)).isoformat()
        to_d = (today + datetime.timedelta(days=45)).isoformat()
        raw: Dict[str, Any] = {}
        last = 0.0
        if self.client:
            try:
                raw = self.client.get_option_chain(
                    sym, from_date=from_d, to_date=to_d, strike_count=20, force=True
                ) or {}
            except Exception as exc:
                log_message(f"[OPT] ticket chain {sym}: {exc}")
                raw = {}
        contracts, last = parse_chain_contracts(raw if isinstance(raw, dict) else {})
        under = (raw or {}).get("underlying") or {}
        try:
            last = last or float(under.get("last") or under.get("mark") or 0)
        except (TypeError, ValueError):
            pass
        expiries = sorted({
            str(c.get("expiry") or "")[:10]
            for c in contracts
            if str(c.get("expiry") or "")[:10] > today.isoformat()
        })
        exp = expiry if expiry in expiries else ""
        if not exp:
            nxt = next_expiry(self.expiry_mode)
            guess = nxt.isoformat() if nxt else ""
            exp = guess if guess in expiries else (expiries[0] if expiries else expiry)
        if exp and exp not in {str(c.get("expiry") or "")[:10] for c in contracts} and self.client:
            try:
                extra = self.client.get_option_chain(
                    sym, from_date=exp, to_date=exp, strike_count=20, force=True
                ) or {}
            except Exception:
                extra = {}
            more, last2 = parse_chain_contracts(extra if isinstance(extra, dict) else {})
            last = last2 or last
            contracts.extend(more)
            expiries = sorted(set(expiries) | {
                str(c.get("expiry") or "")[:10] for c in more if str(c.get("expiry") or "")[:10]
            })
        pool = [c for c in contracts if str(c.get("expiry") or "")[:10] == exp]
        ladder = listed_strike_ladder((c.get("strike") for c in pool), last, LADDER_EACH_SIDE)
        by: Dict[float, Dict[str, Dict[str, Any]]] = {}
        for c in pool:
            try:
                sk = float(c.get("strike") or 0)
            except (TypeError, ValueError):
                continue
            by.setdefault(sk, {})[str(c.get("type") or "").upper()] = c
        nearest = min(ladder, key=lambda s: abs(s - last)) if ladder and last else None
        strikes = []
        for sk in sorted(ladder, reverse=True):
            call = (by.get(sk) or {}).get("CALL") or {}
            put = (by.get(sk) or {}).get("PUT") or {}
            strikes.append({
                "strike": sk,
                "atm": nearest is not None and sk == nearest,
                "call_occ": call.get("occ") or call.get("osi") or "",
                "put_occ": put.get("occ") or put.get("osi") or "",
                "call": {"bid": call.get("bid") or 0, "ask": call.get("ask") or 0, "mark": call.get("mid") or 0},
                "put": {"bid": put.get("bid") or 0, "ask": put.get("ask") or 0, "mark": put.get("mid") or 0},
            })
        return {
            "ok": bool(strikes),
            "symbol": sym,
            "last": last,
            "expiry": exp,
            "expiries": expiries,
            "strikes": strikes,
            "struct_ts": time.time(),
            "error": None if strikes else "no strikes for that expiry",
        }

    def _quote_ticket(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(ticket or {})
        strikes = [dict(s) for s in (out.get("strikes") or [])]
        occs = []
        for s in strikes:
            if s.get("call_occ"):
                occs.append(s["call_occ"])
            if s.get("put_occ"):
                occs.append(s["put_occ"])
        batch: Dict[str, Any] = {}
        fn = getattr(self.client, "get_option_quotes", None) if self.client else None
        if callable(fn) and occs:
            try:
                batch = fn(occs) or {}
            except Exception as exc:
                log_message(f"[OPT] ticket quotes: {exc}")
                batch = {}
        def _leg(occ: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
            compact = str(occ or "").replace(" ", "").upper()
            raw = batch.get(compact) or {}
            bid, ask, mark = _option_px(raw if isinstance(raw, dict) else {})
            if mark <= 0 and fallback:
                bid = bid or float(fallback.get("bid") or 0)
                ask = ask or float(fallback.get("ask") or 0)
                mark = float(fallback.get("mark") or 0) or ((bid + ask) / 2.0 if bid and ask else bid or ask)
            return {"bid": round(bid, 4), "ask": round(ask, 4), "mark": round(mark, 4), "occ": occ}
        for s in strikes:
            s["call"] = _leg(s.get("call_occ") or "", s.get("call") or {})
            s["put"] = _leg(s.get("put_occ") or "", s.get("put") or {})
        out["strikes"] = strikes
        out["quote_ts"] = time.time()
        if self.client:
            try:
                q = self.client.get_quote(out.get("symbol") or "") or {}
                qq = q.get("quote") if isinstance(q.get("quote"), dict) else q
                last = float((qq or {}).get("lastPrice") or (qq or {}).get("mark") or 0)
                if last:
                    out["last"] = last
            except Exception:
                pass
        return out

    def get_tab_data(self) -> Dict[str, Any]:
        for pos in self._positions:
            self._arm_take_profit(pos)
        self._refresh_open_marks()
        data = super().get_tab_data()
        data.update({
            "strategy_id": self.strategy_id,
            "dry_run": self.options_dry_run,
            "expiry_mode": self.expiry_mode,
            "expiry": (next_expiry(self.expiry_mode) or None),
            "max_premium": self.max_premium,
            "max_contracts": self.max_contracts,
            "max_concurrent": self.max_concurrent,
            "strike": self.strike_mode,
            "eod_flat": self.eod_flat,
            "last_entry_hhmm": self.last_entry_hhmm,
            "last_scan": self._last_scan,
            "universe": self._universe,
            "cards": list(self._cards.values()),
            "open": self._positions,
            "closed": self._closed[-20:],
            "watchlist": self.universe.watchlist,
            "open_count": len(self._positions),
            "trades_today": len(self._closed),
            "candidates": self._universe,
        })
        if data["expiry"]:
            data["expiry"] = data["expiry"].isoformat() if hasattr(data["expiry"], "isoformat") else str(data["expiry"])
        return data

    def tab_data(self) -> Dict[str, Any]:
        return self.get_tab_data()

    def get_candidates(self) -> List[Dict[str, Any]]:
        return list(self._universe)

    def _load(self) -> None:
        try:
            raw = json.load(open(self.path, encoding="utf-8"))
            self._positions = list(raw.get("open") or [])
            self._closed = list(raw.get("closed") or [])
            for pos in self._positions:
                self._arm_take_profit(pos)
        except Exception:
            self._positions = []
            self._closed = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            json.dump({"open": self._positions, "closed": self._closed[-80:]},
                      open(self.path, "w", encoding="utf-8"), indent=2)
        except Exception as exc:
            log_message(f"[OPT] save: {exc}")
