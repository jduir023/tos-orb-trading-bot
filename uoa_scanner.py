"""
Unusual options activity scanner.

Looks at ALL expiries/strikes on high-volume underlyings, then scores a
2–5 DTE contract to actually trade (theta-safe window).
"""
from __future__ import annotations
import datetime
from typing import Any, Dict, List, Optional, Tuple

from pillars import pillar, apply as apply_pillars
from utils import log_message, now_et


def flatten_chain(raw: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
    under = raw.get("underlying") or {}
    last = float(under.get("last") or under.get("mark") or under.get("close") or 0)
    rows: List[Dict[str, Any]] = []
    for kind, key in (("CALL", "callExpDateMap"), ("PUT", "putExpDateMap")):
        exp_map = raw.get(key) or {}
        if not isinstance(exp_map, dict):
            continue
        for exp_key, strikes in exp_map.items():
            exp_date, _, dte_s = str(exp_key).partition(":")
            try:
                dte = int(float(dte_s)) if dte_s else 0
            except Exception:
                dte = 0
            if not isinstance(strikes, dict):
                continue
            for _sk, contracts in strikes.items():
                for c in contracts or []:
                    if not isinstance(c, dict):
                        continue
                    vol = float(c.get("totalVolume") or 0)
                    oi = float(c.get("openInterest") or 0)
                    bid = float(c.get("bid") or 0)
                    ask = float(c.get("ask") or 0)
                    mark = float(c.get("mark") or 0)
                    if mark <= 0 and bid > 0 and ask > 0:
                        mark = (bid + ask) / 2.0
                    dte_c = int(c.get("daysToExpiration") if c.get("daysToExpiration") is not None else dte)
                    rows.append({
                        "osi": c.get("symbol") or "",
                        "type": kind,
                        "strike": float(c.get("strikePrice") or 0),
                        "expiry": str(exp_date)[:10],
                        "dte": dte_c,
                        "volume": vol,
                        "oi": oi,
                        "vol_oi": (vol / oi) if oi > 0 else (vol if vol > 0 else 0.0),
                        "bid": bid,
                        "ask": ask,
                        "mark": mark,
                        "delta": c.get("delta"),
                        "underlying": last,
                        "notional": vol * mark * 100,
                    })
    return rows, last


class UOAScanner:
    def __init__(self, client=None) -> None:
        self.client = client
        self.min_dte = 2
        self.max_dte = 5
        self.min_vol_oi = 3.0
        self.min_opt_volume = 200
        self.min_und_rvol = 2.0
        self.max_spread_pct = 12.0
        self.min_mark = 0.15
        self._results: List[Dict[str, Any]] = []
        self._last_ts: float = 0.0

    def set_config(self, **kw) -> None:
        for k, v in kw.items():
            if hasattr(self, k) and v is not None:
                setattr(self, k, v)

    def get_results(self) -> List[Dict[str, Any]]:
        return list(self._results)

    def scan(self, underlyings: List[Dict[str, Any]], max_n: int = 12) -> List[Dict[str, Any]]:
        if not self.client or not underlyings:
            self._results = []
            return []
        today = now_et().date()
        from_d = today.isoformat()
        to_d = (today + datetime.timedelta(days=45)).isoformat()
        out: List[Dict[str, Any]] = []
        for u in underlyings[:max_n]:
            sym = (u.get("symbol") or "").upper()
            if not sym:
                continue
            try:
                raw = self.client.get_option_chain(sym, from_date=from_d, to_date=to_d)
                if not raw:
                    continue
                row = self._score(sym, u, raw)
                if row:
                    out.append(row)
            except Exception as exc:
                log_message(f"[UOA] {sym}: {exc}")
        out.sort(key=lambda r: (int(bool(r.get("ready"))), float(r.get("score_pct") or 0),
                                 float(r.get("best_vol_oi") or 0)), reverse=True)
        self._results = out
        import time as _t
        self._last_ts = _t.time()
        ready_n = sum(1 for r in out if r.get("ready"))
        log_message(f"[UOA] {ready_n} ready / {len(out)} names (2–5 DTE window)")
        return out

    def _score(self, symbol: str, und: Dict[str, Any], raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        contracts, last = flatten_chain(raw)
        if not contracts:
            return None
        last = last or float(und.get("last") or und.get("price") or 0)
        unusual = [c for c in contracts
                   if c["volume"] >= self.min_opt_volume and c["vol_oi"] >= self.min_vol_oi]
        call_notional = sum(c["notional"] for c in unusual if c["type"] == "CALL")
        put_notional = sum(c["notional"] for c in unusual if c["type"] == "PUT")
        direction = "CALL" if call_notional >= put_notional else "PUT"
        best_uoa = max(unusual, key=lambda c: c["vol_oi"]) if unusual else None

        window = [c for c in contracts if self.min_dte <= int(c["dte"]) <= self.max_dte and c["type"] == direction]
        trade = self._pick_contract(window, last, direction)

        rvol = float(und.get("realtime_rvol") or und.get("rel_vol") or 0)
        und_vol_ok = rvol >= self.min_und_rvol
        unusual_ok = bool(unusual)
        dte_ok = bool(trade)
        spread_ok = False
        size_ok = False
        flow_ok = False
        if trade:
            mark = trade["mark"] or ((trade["bid"] + trade["ask"]) / 2 if trade["ask"] else 0)
            spr = ((trade["ask"] - trade["bid"]) / mark * 100) if mark > 0 and trade["ask"] > trade["bid"] else 99
            spread_ok = spr <= self.max_spread_pct and mark >= self.min_mark
            size_ok = trade["volume"] >= 50 or trade["oi"] >= 100
            # directional: calls when UOA is call-heavy, puts when put-heavy
            flow_ok = (direction == "CALL" and call_notional >= put_notional) or (
                direction == "PUT" and put_notional > call_notional
            )
            if unusual:
                flow_ok = flow_ok and (call_notional + put_notional) > 0

        items = [
            pillar("und_vol", "Underlying volume", und_vol_ok, f"{rvol:.1f}x"),
            pillar("unusual", "Unusual options activity", unusual_ok,
                   f"{len(unusual)} hits, best {best_uoa['vol_oi']:.1f}x" if best_uoa else "none"),
            pillar("dte", "2–5 DTE contract", dte_ok,
                   f"{trade['dte']}d {trade['type']} {trade['strike']}" if trade else "no window"),
            pillar("spread", "Tight option spread", spread_ok),
            pillar("flow", "Directional flow", flow_ok, direction),
            pillar("size", "Contract liquidity", size_ok),
        ]
        row: Dict[str, Any] = {
            "symbol": symbol,
            "last": last,
            "price": last,
            "direction": direction,
            "call_notional": round(call_notional, 0),
            "put_notional": round(put_notional, 0),
            "unusual_count": len(unusual),
            "best_vol_oi": round(best_uoa["vol_oi"], 2) if best_uoa else 0,
            "best_uoa": {
                "osi": best_uoa["osi"], "type": best_uoa["type"], "dte": best_uoa["dte"],
                "strike": best_uoa["strike"], "vol_oi": round(best_uoa["vol_oi"], 2),
                "volume": best_uoa["volume"], "expiry": best_uoa["expiry"],
            } if best_uoa else None,
            "trade": {
                "osi": trade["osi"], "type": trade["type"], "dte": trade["dte"],
                "strike": trade["strike"], "expiry": trade["expiry"],
                "bid": trade["bid"], "ask": trade["ask"], "mark": trade["mark"],
                "volume": trade["volume"], "oi": trade["oi"],
            } if trade else None,
            "rel_vol": rvol,
        }
        apply_pillars(row, items)
        return row

    def _pick_contract(self, window: List[Dict[str, Any]], last: float, direction: str) -> Optional[Dict[str, Any]]:
        if not window or last <= 0:
            return None
        liquid = []
        for c in window:
            mark = c["mark"] or ((c["bid"] + c["ask"]) / 2 if c["ask"] else 0)
            if mark < self.min_mark:
                continue
            if c["bid"] <= 0 or c["ask"] <= 0:
                continue
            liquid.append(c)
        if not liquid:
            return None
        # Prefer slightly OTM, then highest volume among 2–5 DTE
        def rank(c):
            otm = 0
            if direction == "CALL":
                otm = 1 if c["strike"] >= last else 0
                dist = abs(c["strike"] - last) / last
            else:
                otm = 1 if c["strike"] <= last else 0
                dist = abs(c["strike"] - last) / last
            unusual = 1 if c["vol_oi"] >= self.min_vol_oi else 0
            return (unusual, otm, c["volume"], -dist)
        liquid.sort(key=rank, reverse=True)
        return liquid[0]
