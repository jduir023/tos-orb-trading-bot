"""
Pick next Wed/Fri weekly (never 0DTE), ATM/ITM1 strike, size by premium cap.
"""
from __future__ import annotations
import datetime
import re
from typing import Any, Dict, List, Optional, Tuple

from utils import log_message, now_et

OCC_RE = re.compile(
    r"^(?P<root>\S+)\s*(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$"
)


def parse_occ_symbol(occ: str) -> Optional[Dict[str, Any]]:
    s = (occ or "").replace(" ", "")
    # Allow padded root: AAPL  260902C00220000
    m = re.match(r"^([A-Z0-9.\-]{1,6})\s*(\d{6})([CP])(\d{8})$", (occ or "").upper())
    if not m:
        compact = re.sub(r"\s+", "", (occ or "").upper())
        m = re.match(r"^([A-Z0-9.\-]{1,6})(\d{6})([CP])(\d{8})$", compact)
    if not m:
        return None
    root, ymd, cp, strike_s = m.group(1), m.group(2), m.group(3), m.group(4)
    yy, mm, dd = int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])
    year = 2000 + yy
    try:
        expiry = datetime.date(year, mm, dd)
    except ValueError:
        return None
    return {
        "root": root,
        "expiry": expiry.isoformat(),
        "cp": "CALL" if cp == "C" else "PUT",
        "strike": int(strike_s) / 1000.0,
        "occ": occ,
    }


def next_expiry(mode: str = "wed_then_fri", today: Optional[datetime.date] = None) -> Optional[datetime.date]:
    """Next Wednesday or Friday at least 1 calendar day away. Never today."""
    today = today or now_et().date()
    wed = _next_dow(today, 2)
    fri = _next_dow(today, 4)
    if mode == "fri_only":
        return fri
    # wed_then_fri: prefer the nearer Wed if it is valid, else Fri
    if wed <= fri:
        return wed
    return fri


def _next_dow(today: datetime.date, weekday: int) -> datetime.date:
    delta = (weekday - today.weekday()) % 7
    if delta == 0:
        delta = 7
    d = today + datetime.timedelta(days=delta)
    if (d - today).days < 1:
        d += datetime.timedelta(days=7)
    return d


def parse_chain_contracts(raw: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
    under = raw.get("underlying") or {}
    last = float(under.get("last") or under.get("mark") or under.get("close") or 0)
    rows: List[Dict[str, Any]] = []
    for kind, key in (("CALL", "callExpDateMap"), ("PUT", "putExpDateMap")):
        exp_map = raw.get(key) or {}
        if not isinstance(exp_map, dict):
            continue
        for exp_key, strikes in exp_map.items():
            exp_date, _, dte_s = str(exp_key).partition(":")
            exp_date = str(exp_date)[:10]
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
                    bid = float(c.get("bid") or 0)
                    ask = float(c.get("ask") or 0)
                    mark = float(c.get("mark") or 0)
                    if mark <= 0 and bid > 0 and ask > 0:
                        mark = (bid + ask) / 2.0
                    spread = (ask - bid) if ask > bid else 99.0
                    spct = (spread / mark * 100.0) if mark > 0 else 99.0
                    dte_c = c.get("daysToExpiration")
                    if dte_c is None:
                        dte_c = dte
                    rows.append({
                        "occ": c.get("symbol") or "",
                        "osi": c.get("symbol") or "",
                        "type": kind,
                        "strike": float(c.get("strikePrice") or 0),
                        "expiry": exp_date,
                        "dte": int(dte_c or 0),
                        "bid": bid,
                        "ask": ask,
                        "mid": mark,
                        "spread": round(spread, 4),
                        "spread_pct": round(spct, 2),
                        "oi": float(c.get("openInterest") or 0),
                        "volume": float(c.get("totalVolume") or 0),
                        "multiplier": int(c.get("multiplier") or 100),
                    })
    return rows, last


def listed_strike_ladder(strikes, last: float, n: int = 8) -> List[float]:
    """Up to n listed strikes strictly below last, any exact ATM, n strictly above."""
    uniq: List[float] = []
    seen = set()
    try:
        spot = float(last or 0)
    except (TypeError, ValueError):
        spot = 0.0
    for raw in strikes or []:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val <= 0 or val in seen:
            continue
        seen.add(val)
        uniq.append(val)
    uniq.sort()
    if not uniq:
        return []
    side = max(int(n or 0), 0)
    if spot <= 0:
        return uniq
    below = [s for s in uniq if s < spot]
    atm = [s for s in uniq if s == spot]
    above = [s for s in uniq if s > spot]
    return below[-side:] + atm + above[:side]


def atm_pair(contracts: List[Dict], last: float, expiry: str) -> Optional[Tuple[Dict, Dict]]:
    calls = [c for c in contracts if c["type"] == "CALL" and c["expiry"] == expiry]
    puts = [c for c in contracts if c["type"] == "PUT" and c["expiry"] == expiry]
    if not calls or not puts or last <= 0:
        return None

    def nearest(rows):
        return min(rows, key=lambda c: abs(c["strike"] - last))

    return nearest(calls), nearest(puts)


def select_contract(
    client,
    symbol: str,
    side: str,
    last: float,
    expiry: str,
    strike_mode: str = "atm",
    max_premium: float = 150.0,
    max_contracts: int = 2,
    max_spread_pct: float = 8.0,
    max_spread_abs: float = 0.15,
    min_mid: float = 0.40,
) -> Optional[Dict[str, Any]]:
    """Return {occ, strike, expiry, dte, bid, ask, mid, qty} or None."""
    if not expiry or not side or last <= 0:
        return None
    today = now_et().date().isoformat()
    if expiry <= today:
        log_message(f"[OPT-SEL] {symbol} refuse 0DTE/today {expiry}")
        return None
    raw = client.get_option_chain(symbol, from_date=expiry, to_date=expiry, strike_count=12)
    contracts, und = parse_chain_contracts(raw or {})
    last = und or last
    side = side.upper()
    kind = "CALL" if side in ("CALL", "LONG", "C") else "PUT"
    pool = [c for c in contracts if c["type"] == kind and c["expiry"] == expiry and c.get("multiplier", 100) == 100]
    if not pool:
        log_message(f"[OPT-SEL] {symbol} no {kind}s for {expiry}")
        return None
    if strike_mode == "itm1":
        if kind == "CALL":
            itm = [c for c in pool if c["strike"] <= last]
            pick = max(itm, key=lambda c: c["strike"]) if itm else min(pool, key=lambda c: abs(c["strike"] - last))
        else:
            itm = [c for c in pool if c["strike"] >= last]
            pick = min(itm, key=lambda c: c["strike"]) if itm else min(pool, key=lambda c: abs(c["strike"] - last))
    else:
        pick = min(pool, key=lambda c: abs(c["strike"] - last))
    if pick["dte"] <= 0 or pick["expiry"] <= today:
        log_message(f"[OPT-SEL] {symbol} 0DTE blocked")
        return None
    mid = pick["mid"]
    if mid < min_mid or pick["spread"] > max_spread_abs or pick["spread_pct"] > max_spread_pct:
        log_message(
            f"[OPT-SEL] {symbol} {pick['occ']} fail spread/mid "
            f"mid={mid:.2f} spr={pick['spread']:.2f}"
        )
        return None
    ask = pick["ask"] or mid
    qty = int(max_premium // (ask * 100))
    qty = max(0, min(int(max_contracts), qty))
    if qty < 1:
        log_message(f"[OPT-SEL] {symbol} premium cap ${max_premium} < 1 contract @ {ask:.2f}")
        return None
    buy_px = min(ask, mid + 0.02) if mid else ask
    pick = dict(pick)
    pick["qty"] = qty
    pick["limit"] = round(buy_px, 2)
    return pick
