"""
Liquid weekly-options universe. Not the under-$10 equity ORB scanner.
Ranks tradability (spread, OI, weekly expiry), not today's % change.
"""
from __future__ import annotations
import time
from typing import Any, Dict, List, Optional

from contract_selector import next_expiry, parse_chain_contracts, atm_pair
from utils import log_message

CORE_UNDERLYINGS = [
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "AMD", "GOOGL", "AVGO",
    "MRVL", "MU", "INTC",
    "CRWD", "WDAY", "ADSK", "AFRM",
    "NFLX", "CRM", "ORCL",
    "JPM", "BAC", "XOM",
    "GLD", "TLT",
]
MEGA = {"SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA"}
ETF_EXEMPT = {"SPY", "QQQ", "IWM", "GLD", "TLT"}


class OptionsUniverse:
    def __init__(self, client=None) -> None:
        self.client = client
        self.watchlist: List[str] = []
        self.max_names = 40
        self.min_price = 20.0
        self.max_price = 600.0
        self.min_adv = 5_000_000
        self.max_spread_pct = 8.0
        self.max_spread_abs = 0.15
        self.min_mid = 0.40
        self.min_oi = 500
        self.min_opt_vol = 100
        self._results: List[Dict[str, Any]] = []
        self._last_ts = 0.0

    def set_config(self, **kw) -> None:
        if "options_watchlist" in kw and kw["options_watchlist"] is not None:
            wl = kw["options_watchlist"]
            if isinstance(wl, str):
                wl = [x.strip().upper() for x in wl.split(",") if x.strip()]
            self.watchlist = [str(x).upper() for x in wl]
        for k in ("max_spread_pct", "max_spread_abs", "min_mid", "min_oi"):
            if k in kw and kw[k] is not None:
                setattr(self, k, kw[k])

    def symbols(self) -> List[str]:
        seen = []
        for s in CORE_UNDERLYINGS + list(self.watchlist):
            u = (s or "").upper().strip()
            if u and u not in seen:
                seen.append(u)
            if len(seen) >= self.max_names:
                break
        return seen

    def get_results(self) -> List[Dict[str, Any]]:
        return list(self._results)

    def scan(self, expiry_mode: str = "wed_then_fri") -> List[Dict[str, Any]]:
        names = self.symbols()
        quotes = {}
        try:
            quotes = self.client.get_quotes(names) if self.client else {}
        except Exception as exc:
            log_message(f"[OPT-UNIV] quotes: {exc}")
        if not quotes and self._results:
            log_message("[OPT-UNIV] quotes empty — keeping last universe")
            return list(self._results)
        out: List[Dict[str, Any]] = []
        expiry = next_expiry(expiry_mode)
        if not expiry:
            log_message("[OPT-UNIV] no Wed/Fri expiry >= 1 day out")
            self._results = []
            return []
        exp_s = expiry.isoformat()
        prev = {r.get("symbol"): r for r in self._results if isinstance(r, dict) and r.get("symbol")}
        for sym in names:
            q = quotes.get(sym) or {}
            qq = q.get("quote") or {}
            fund = q.get("fundamental") or {}
            last = float(qq.get("lastPrice") or 0)
            adv = float(fund.get("vol10DayAvg") or qq.get("totalVolume") or 0)
            row = self._gate(sym, last, adv, exp_s, prev.get(sym))
            if row:
                out.append(row)
        out.sort(key=lambda r: (int(not r.get("tradable")), float(r.get("spread_pct") or 99)))
        self._results = out
        self._last_ts = time.time()
        ok = sum(1 for r in out if r.get("tradable"))
        log_message(f"[OPT-UNIV] {ok} tradable / {len(names)} scanned, expiry {exp_s}")
        return out

    def _gate(self, sym: str, last: float, adv: float, exp_s: str, prev: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        skip = None
        if last <= 0:
            skip = "no last"
        elif sym not in ETF_EXEMPT and not (self.min_price <= last <= self.max_price):
            skip = f"price {last:.2f} outside {self.min_price}-{self.max_price}"
        elif adv and adv < self.min_adv and sym not in MEGA:
            skip = f"adv {adv:.0f} < {self.min_adv}"
        if skip:
            return {
                "symbol": sym, "last": last, "expiry": exp_s, "tradable": False,
                "skip_reason": skip, "call": None, "put": None,
            }
        if not self.client:
            return {"symbol": sym, "last": last, "expiry": exp_s, "tradable": False,
                    "skip_reason": "no client", "call": None, "put": None}
        raw = self.client.get_option_chain(sym, from_date=exp_s, to_date=exp_s, strike_count=8)
        if not raw:
            if isinstance(prev, dict) and prev.get("call"):
                keep = dict(prev)
                if last:
                    keep["last"] = last
                return keep
            return {"symbol": sym, "last": last, "expiry": exp_s, "tradable": False,
                    "skip_reason": "no chain", "call": None, "put": None}
        contracts, und_last = parse_chain_contracts(raw)
        last = und_last or last
        pair = atm_pair(contracts, last, exp_s)
        if not pair:
            log_message(f"[OPT-UNIV] SKIP {sym}: no ATM weekly")
            return {"symbol": sym, "last": last, "expiry": exp_s, "tradable": False,
                    "skip_reason": "no ATM weekly", "call": None, "put": None}
        call, put = pair
        call_ok, call_why = self._contract_ok(call, sym)
        put_ok, put_why = self._contract_ok(put, sym)
        tradable = call_ok and put_ok
        reason = None if tradable else f"call:{call_why} put:{put_why}"
        if not tradable:
            log_message(f"[OPT-UNIV] SKIP {sym}: {reason}")
        return {
            "symbol": sym,
            "last": last,
            "expiry": exp_s,
            "atm_strike": call.get("strike") if call else None,
            "call": call,
            "put": put,
            "tradable": tradable,
            "skip_reason": reason,
            "spread_pct": max(call.get("spread_pct") or 0, put.get("spread_pct") or 0),
        }

    def _contract_ok(self, c: Optional[Dict], sym: str) -> tuple:
        if not c:
            return False, "missing"
        mid = float(c.get("mid") or 0)
        spr = float(c.get("spread") or 99)
        spct = float(c.get("spread_pct") or 99)
        oi = float(c.get("oi") or 0)
        vol = float(c.get("volume") or 0)
        if mid < self.min_mid:
            return False, f"mid {mid:.2f}"
        if spr > self.max_spread_abs or spct > self.max_spread_pct:
            return False, f"spread {spr:.2f}/{spct:.1f}%"
        if oi < self.min_oi:
            return False, f"oi {oi:.0f}"
        if vol < self.min_opt_vol and sym not in MEGA:
            return False, f"opt vol {vol:.0f}"
        return True, "ok"
