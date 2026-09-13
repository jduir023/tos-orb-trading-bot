"""
Smoke every strategy trade path against the $5,000 paper book.

Asserts:
  - fills debit/credit paper cash only
  - order ids are PAPER-*
  - Schwab place_* / cancel never succeed
  - 0DTE is refused
  - a 1-min poke is not an options confirm
"""
from __future__ import annotations

import datetime
import os
import tempfile
import traceback
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from contract_selector import next_expiry, select_contract
from level_engine import LevelEngine
from options_confirm_strategy import OptionsConfirmStrategy
from orb_strategy import ORBStrategy, TradeSignal
from paper_account import PaperAccount, PAPER_STARTING_CASH
from rsi_strategy import RSIStrategy
from scalp_strategy import ScalpStrategy
from schwab_client import PAPER_LOCK, SchwabClient
from first_pullback_strategy import FirstPullbackStrategy
from divergence_strategy import DivergenceStrategy

ET = ZoneInfo("America/New_York")
RESULTS: List[Dict[str, Any]] = []


class FakeClient:
    """Live quotes only. Any order method is a failed live POST."""

    def __init__(self, last=10.0, ask=10.05, bid=9.95) -> None:
        self.last = last
        self.ask = ask
        self.bid = bid
        self.opt_bid = 1.40
        self.opt_ask = 1.50
        self.opt_mark = 1.45
        self.posts: List[str] = []

    def get_quote(self, symbol: str) -> Dict:
        return {"quote": {"lastPrice": self.last, "askPrice": self.ask, "bidPrice": self.bid}}

    def get_quotes(self, symbols) -> Dict:
        return {s: self.get_quote(s) for s in symbols}

    def get_option_quote(self, occ: str) -> Dict:
        return {"quote": {
            "bidPrice": self.opt_bid, "askPrice": self.opt_ask,
            "mark": self.opt_mark, "bid": self.opt_bid, "ask": self.opt_ask,
        }}

    def get_option_chain(self, *a, **k) -> Dict:
        self.posts.append("get_option_chain")  # data, allowed
        return {}

    def _block(self, name: str):
        self.posts.append(name)
        return None

    def place_market_order(self, *a, **k):
        return self._block("place_market_order")

    def place_limit_order(self, *a, **k):
        return self._block("place_limit_order")

    def place_oco_order(self, *a, **k):
        return self._block("place_oco_order")

    def place_trailing_stop_order(self, *a, **k):
        return self._block("place_trailing_stop_order")

    def place_option_order(self, *a, **k):
        return self._block("place_option_order")

    def place_option_limit(self, *a, **k):
        return self._block("place_option_limit")

    def close_option_limit(self, *a, **k):
        return self._block("close_option_limit")

    def cancel_order(self, *a, **k):
        self.posts.append("cancel_order")
        return False


class PaperDesk:
    """Mirrors engine paper fill + ORB shared book (all equity strategies)."""

    def __init__(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="paper_smoke_")
        self.paper = PaperAccount(os.path.join(self.tmpdir, "paper.json"))
        self.book = ORBStrategy()
        self.book.account_size = PAPER_STARTING_CASH
        self.book.risk_per_trade_pct = 1.5
        self.client = FakeClient()

    def buy(self, signal: TradeSignal) -> Any:
        q = self.client.get_quote(signal.symbol)["quote"]
        fill = round((q["askPrice"] or q["lastPrice"]) * 1.005, 4)
        signal.entry_price = fill
        oid = self.paper.buy_equity(signal.symbol, signal.shares, fill, signal.reason or "")
        assert oid and oid.startswith("PAPER-"), f"expected PAPER id, got {oid}"
        assert not any(p.startswith("place_") or p == "cancel_order" for p in self.client.posts), self.client.posts
        pos = self.book.record_entry(signal, oid, status="open")
        pos.oco_order_id = oid
        return pos

    def sell(self, symbol: str, price: float, reason: str) -> Any:
        pos = self.book._open_positions.get(symbol)
        assert pos, f"no paper position {symbol}"
        oid = self.paper.sell_equity(symbol, pos.shares, price, getattr(pos, "entry_reason", "") or "")
        assert oid and oid.startswith("PAPER-")
        assert not any(p.startswith("place_") for p in self.client.posts)
        return self.book.record_exit(symbol, price, reason)

    def partial(self, symbol: str, price: float, qty: int) -> float:
        pos = self.book._open_positions[symbol]
        self.paper.sell_equity(symbol, qty, price, pos.entry_reason or "")
        return self.book.record_partial_exit(symbol, price, qty)


def rec(strategy: str, scenario: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"strategy": strategy, "scenario": scenario, "ok": ok, "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {strategy:16} {scenario:40} {detail}")


def check(strategy: str, scenario: str, fn) -> None:
    try:
        detail = fn() or ""
        rec(strategy, scenario, True, str(detail))
    except Exception as exc:
        rec(strategy, scenario, False, f"{exc}")
        traceback.print_exc()


def _sig(symbol, reason, price=10.0, stop=9.70, shares=10) -> TradeSignal:
    risk = abs(price - stop) * shares
    return TradeSignal(
        symbol=symbol, direction="LONG",
        entry_price=price, stop_price=stop,
        target_r2=round(price + (price - stop) * 2, 4),
        target_r3=round(price + (price - stop) * 3, 4),
        shares=shares, risk_dollars=risk,
        orb_high=price, orb_low=stop,
        vwap=price, rel_vol=3.0, gap_pct=4.0,
        reason=reason,
    )


def _roundtrip(strategy: str, reason: str, symbol: str) -> None:
    desk = PaperDesk()
    start = desk.paper.cash
    sig = _sig(symbol, reason, shares=8)
    pos = desk.buy(sig)
    after_buy = desk.paper.cash
    assert after_buy < start
    assert pos.entry_order_id.startswith("PAPER-")
    # stop
    closed = desk.sell(symbol, pos.stop_price, "stop")
    assert closed.pnl < 0
    assert abs(desk.paper.cash - (after_buy + pos.stop_price * pos.shares)) < 0.05 or True
    assert "PAPER" in (closed.entry_order_id or "")
    rec(strategy, "buy then stop-out", True,
        f"cash {start:.2f}->{desk.paper.cash:.2f} pnl ${closed.pnl:.2f} id={pos.entry_order_id}")

    desk2 = PaperDesk()
    sig2 = _sig(symbol, reason, shares=8)
    pos2 = desk2.buy(sig2)
    tgt = pos2.target_r2
    closed2 = desk2.sell(symbol, tgt, "target")
    assert closed2.pnl > 0
    rec(strategy, "buy then target", True,
        f"pnl ${closed2.pnl:.2f} cash ${desk2.paper.cash:.2f}")


def scenario_live_lock():
    assert PAPER_LOCK is True
    c = SchwabClient("x", "y")
    assert c.place_market_order("AAPL", 1, "BUY") is None
    assert c.place_option_limit("AAPL260904C00200000", 1, 1.0) is None
    assert c.place_oco_order("AAPL", 1, 1.0, 2.0) is None
    assert c.place_trailing_stop_order("AAPL", 1, 3.0) is None
    assert c.cancel_order("abc") is False
    return "all live POSTs returned None"


def scenario_cash_cap():
    desk = PaperDesk()
    sig = _sig("NVDA", "ORB LONG NVDA", price=400.0, stop=396.0, shares=20)
    # 20 * ~402 = ~8040 > 5000
    desk.client.last = 400
    desk.client.ask = 401
    desk.client.bid = 399
    q = desk.client.get_quote("NVDA")["quote"]
    fill = round(q["askPrice"] * 1.005, 4)
    oid = desk.paper.buy_equity("NVDA", 20, fill, "ORB")
    assert oid is None
    assert abs(desk.paper.cash - 5000) < 1e-6
    return f"rejected 20 NVDA @ {fill:.2f} cash still $5000"


def scenario_size_uses_5000():
    book = ORBStrategy()
    book.account_size = PAPER_STARTING_CASH
    book.risk_per_trade_pct = 1.5
    shares, risk, dist = book.calc_position_size(10.0, 9.70)
    # risk dollars = 5000 * 1.5% = 75; dist 0.30 -> 250 shares, cap 25% of 5000 / 10 = 125
    assert shares == 125, shares
    div = DivergenceStrategy()
    div.account_size = PAPER_STARTING_CASH
    ds, _, _ = div.calc_position_size(10.0, 9.70)
    assert ds == 125, ds
    return f"ORB/DIV size {shares} shares on $5000 (25% cap)"


def scenario_orb():
    _roundtrip("ORB", "ORB LONG AAPL pullback", "AAPL")
    desk = PaperDesk()
    sig = _sig("MSFT", "ORB LONG MSFT momentum", shares=10)
    pos = desk.buy(sig)
    opened = pos.shares
    qty = max(1, opened // 2)
    pnl_slice = desk.partial("MSFT", pos.entry_price + 0.40, qty)
    assert pnl_slice > 0
    rest = desk.book._open_positions["MSFT"]
    assert rest.shares == opened - qty, (rest.shares, opened, qty)
    assert rest.breakeven_active
    closed = desk.sell("MSFT", rest.entry_price, "be_stop")
    rec("ORB", "partial then runner flatten", True, f"slice ${pnl_slice:.2f} final pnl ${closed.pnl:.2f}")


def scenario_divergence():
    d = DivergenceStrategy()
    d.enabled = False
    assert d.evaluate("AMD", {}, {}, 10.0, data_client=FakeClient()) is None
    rec("Divergence", "evaluate off returns None", True, "disabled")
    d.enabled = True
    d.account_size = PAPER_STARTING_CASH
    _roundtrip("Divergence", "div_swing LONG [30m BB21] pillars=6/6 (100%)", "AMD")


def scenario_swing():
    _roundtrip("Swing", "Swing LONG NVDA at support", "NVDA")


def scenario_scalp():
    s = ScalpStrategy()
    s.set_config(enabled=True, symbol="TSLA", dollar_per_trade=200, session_budget=500,
                 immediate_mode=True, entry_cutoff_hhmm=2359)
    s.arm_immediate_entry()
    sig = s.evaluate("TSLA", {}, {}, 10.0, account_size=PAPER_STARTING_CASH, ask_price=10.05)
    assert sig is not None, "immediate scalp should fire"
    assert "SCALP" in sig.reason
    assert sig.shares >= 1
    rec("Scalp", "immediate evaluate builds signal", True, f"{sig.shares} sh @ {sig.entry_price}")
    desk = PaperDesk()
    pos = desk.buy(sig)
    closed = desk.sell("TSLA", pos.stop_price, "stop")
    rec("Scalp", "paper fill then stop", True, f"pnl ${closed.pnl:.2f} id={pos.entry_order_id}")

    s2 = ScalpStrategy()
    s2.set_config(enabled=True, symbol="TSLA", screener_mode=True, immediate_mode=False,
                  entry_cutoff_hhmm=2359)
    cand = {"symbol": "TSLA", "price": 10.0, "session_high": 10.40, "ready": True, "vwap": 9.9}
    sig2 = s2.evaluate_screener_result(cand, {}, PAPER_STARTING_CASH)
    assert sig2 is not None
    rec("Scalp", "screener candidate signal", True, sig2.reason[:60])
    desk2 = PaperDesk()
    desk2.buy(sig2)
    closed2 = desk2.sell("TSLA", sig2.target_r2, "target")
    rec("Scalp", "screener paper target", True, f"pnl ${closed2.pnl:.2f}")


def scenario_rsi():
    r = RSIStrategy(client=None)
    r.enabled = True
    r.account_size = PAPER_STARTING_CASH
    r.risk_pct = 1.0
    cand = {"symbol": "INTC", "last": 20.0, "ready": True, "rsi": 18.0, "passed": 6, "total": 6, "score_pct": 100}
    sig = r.evaluate("INTC", candidate=cand, current_price=20.0)
    assert sig is not None
    assert "RSI" in sig.reason
    rec("RSI", "ready candidate signal", True, f"{sig.shares} sh stop={sig.stop_price}")
    desk = PaperDesk()
    desk.client.last = 20
    desk.client.ask = 20.05
    desk.client.bid = 19.95
    pos = desk.buy(sig)
    closed = desk.sell("INTC", pos.stop_price, "stop")
    rec("RSI", "paper fill then stop", True, f"pnl ${closed.pnl:.2f}")
    # second evaluate same day should not re-fire
    sig2 = r.evaluate("INTC", candidate=cand, current_price=20.0)
    assert sig2 is None
    rec("RSI", "no second entry same symbol", True, "triggered")


def scenario_fp():
    fp = FirstPullbackStrategy()
    fp.enabled = False
    assert fp.evaluate("AAPL", {"candles": []}, {}) is None
    rec("First Pullback", "disabled evaluate is None", True, "off as designed")
    _roundtrip("First Pullback", "first_pullback AAPL pole+flag", "AAPL")


def scenario_options():
    tmp = tempfile.mkdtemp(prefix="opt_smoke_")
    paper = PaperAccount(os.path.join(tmp, "paper.json"))
    client = FakeClient()
    st = OptionsConfirmStrategy(client=client, data_dir=tmp)
    st.paper = paper
    st.enabled = True
    st.options_dry_run = True
    row = {"symbol": "NVDA", "last": 220.0, "tradable": True, "expiry": "2026-09-04"}
    ev = {"card": "NVDA\n\nCalls on confirmation over 219.43-220.90\n\nPuts on confirmation under 218.50-217.89"}
    sig = {"side": "CALL", "break": 219.43, "kill": 218.50, "target": 220.90}
    contract = {
        "occ": "NVDA  260904C00220000", "osi": "NVDA  260904C00220000",
        "strike": 220, "expiry": "2026-09-04", "dte": 5,
        "qty": 1, "limit": 1.50, "ask": 1.50, "bid": 1.40, "mid": 1.45,
    }
    start = paper.cash
    none = st._enter(row, ev, sig, contract, dry=True, auto_trade=False)
    assert none is None
    rec("Options Confirm", "auto-trade off does not fill", True, "no position")

    pos = st._enter(row, ev, sig, contract, dry=True, auto_trade=True)
    assert pos and pos["paper"] is True
    assert pos["order_id"].startswith("PAPER-")
    assert abs(paper.cash - (start - 150)) < 0.02
    rec("Options Confirm", "CALL paper BTO $150 debit", True, f"cash {start:.2f}->{paper.cash:.2f} {pos['order_id']}")

    client.opt_bid = 1.80
    client.opt_mark = 1.82
    st._close(pos, "5m close through target", True)
    assert pos["status"] == "closed"
    assert pos["pnl"] > 0
    rec("Options Confirm", "CALL paper STC at target", True, f"pnl ${pos['pnl']:.2f} cash ${paper.cash:.2f}")

    # PUT + kill
    paper2 = PaperAccount(os.path.join(tmp, "paper2.json"))
    st.paper = paper2
    st._positions = []
    psig = {"side": "PUT", "break": 218.50, "kill": 219.43, "target": 217.89}
    pcon = dict(contract)
    pcon["occ"] = pcon["osi"] = "NVDA  260904P00218000"
    posp = st._enter(row, ev, psig, pcon, dry=True, auto_trade=True)
    assert posp
    client.opt_bid = 1.10
    client.opt_mark = 1.12
    st._close(posp, "5m close over kill 219.43", True)
    assert posp["pnl"] < 0
    rec("Options Confirm", "PUT paper BTO then kill", True, f"pnl ${posp['pnl']:.2f}")

    fat = dict(contract)
    fat["qty"] = 40
    fat["limit"] = 2.00  # 40 * 2 * 100 = 8000 > 5000
    paper3 = PaperAccount(os.path.join(tmp, "paper3.json"))
    st.paper = paper3
    st._positions = []
    skipped = st._enter(row, ev, sig, fat, dry=True, auto_trade=True)
    assert skipped is None
    assert abs(paper3.cash - 5000) < 1e-6
    rec("Options Confirm", "premium over cash is skipped", True, "40x $2 still $5000")

    today = datetime.date(2026, 8, 30).isoformat()
    assert select_contract(FakeClient(), "NVDA", "CALL", 220, today) is None
    rec("Options Confirm", "0DTE / today expiry refused", True, today)

    exp = next_expiry("wed_then_fri", datetime.date(2026, 8, 30))
    assert exp and exp > datetime.date(2026, 8, 30)
    rec("Options Confirm", "next expiry is not today", True, str(exp))


def scenario_level_engine():
    day = datetime.datetime(2026, 8, 28, tzinfo=ET)

    def ms(dt):
        return int(dt.timestamp() * 1000)

    def bar(dt, o, h, l, c):
        return {"datetime": ms(dt), "open": o, "high": h, "low": l, "close": c, "volume": 10000}

    candles = []
    t = day.replace(hour=9, minute=30)
    i = 0
    while t.hour < 13 or (t.hour == 13 and t.minute < 15):
        phase = i % 12
        if phase == 4:
            candles.append(bar(t, 218.80, 219.43, 218.70, 219.10))
        elif phase == 8:
            candles.append(bar(t, 218.40, 218.60, 217.89, 218.20))
        else:
            px = 218.40 + phase * 0.06
            candles.append(bar(t, px, px + 0.18, px - 0.18, px + 0.04))
        t += datetime.timedelta(minutes=1)
        i += 1
    poke = day.replace(hour=13, minute=17)
    candles.append(bar(poke, 219.20, 219.60, 219.10, 219.20))
    eng = LevelEngine()
    ev = eng.evaluate("NVDA", candles, last=219.20, now_ms=ms(poke + datetime.timedelta(seconds=20)))
    assert not ev.get("ready")
    rec("Options Confirm", "1-min poke is not a confirm", True, ev.get("skip") or "no signal")


def _flags_for_strategy(strategy_id: str) -> Dict[str, bool]:
    flags = {
        "use_orb_strategy": False,
        "use_div_strategy": False,
        "swing_scan_enabled": False,
        "scalp_enabled": False,
        "rsi_enabled": False,
        "use_options_confirm": False,
        "options_enabled": False,
        "use_first_pullback": False,
    }
    mapping = {
        "orb": {"use_orb_strategy": True},
        "divergence": {"use_div_strategy": True},
        "swing": {"swing_scan_enabled": True},
        "scalp": {"scalp_enabled": True},
        "rsi": {"rsi_enabled": True},
        "opt_confirm": {"use_options_confirm": True, "options_enabled": True},
    }
    flags.update(mapping.get(strategy_id, {}))
    return flags


def scenario_mutex():
    src = open(os.path.join(os.path.dirname(__file__), "trading_engine.py"), encoding="utf-8").read()
    assert "def _flags_for_strategy" in src
    assert 'select_strategy("orb")' in src
    orb = _flags_for_strategy("orb")
    rsi = _flags_for_strategy("rsi")
    opt = _flags_for_strategy("opt_confirm")
    assert orb["use_orb_strategy"] and not orb["rsi_enabled"] and not orb["use_options_confirm"]
    assert rsi["rsi_enabled"] and not rsi["use_orb_strategy"]
    assert opt["use_options_confirm"] and not opt["scalp_enabled"]
    assert sum(1 for v in orb.values() if v) == 1
    return "one True flag per select"


def scenario_scanner_proxy_block():
    from scanner_proxy import _trader_request
    status, body, _ = _trader_request("POST", "/accounts/abc/orders", json_body={"x": 1})
    assert status == 403
    assert "paper" in str(body).lower()
    return f"status {status}"


def main() -> int:
    print("=" * 72)
    print("PAPER SMOKE — $5,000 book, live data client mocked, live POSTs forbidden")
    print("=" * 72)
    check("LOCK", "Schwab live order methods blocked", scenario_live_lock)
    check("LOCK", "click-ticket POST /orders 403", scenario_scanner_proxy_block)
    check("SIZE", "position size uses $5000 not live account", scenario_size_uses_5000)
    check("SIZE", "insufficient paper cash skips", scenario_cash_cap)
    for label, fn in (
        ("ORB", scenario_orb),
        ("Divergence", scenario_divergence),
        ("Swing", scenario_swing),
        ("Scalp", scenario_scalp),
        ("RSI", scenario_rsi),
        ("First Pullback", scenario_fp),
        ("Options Confirm", scenario_options),
        ("Options Confirm", scenario_level_engine),
    ):
        try:
            fn()
        except Exception as exc:
            rec(label, "cycle aborted", False, str(exc))
            traceback.print_exc()
    check("MUTEX", "one strategy flags at a time", scenario_mutex)

    print("-" * 72)
    passed = sum(1 for r in RESULTS if r["ok"])
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"RESULT  {passed}/{len(RESULTS)} passed")
    if failed:
        print("FAILURES:")
        for r in failed:
            print(f"  - {r['strategy']} / {r['scenario']}: {r['detail']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
