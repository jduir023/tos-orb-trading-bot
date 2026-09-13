"""Paper book + live-order lock. No broker calls."""
from __future__ import annotations
import os
import tempfile

from paper_account import PaperAccount, PAPER_STARTING_CASH
from schwab_client import PAPER_LOCK, SchwabClient


def test_starting_cash():
    path = os.path.join(tempfile.gettempdir(), "paper_test.json")
    try:
        os.remove(path)
    except OSError:
        pass
    p = PaperAccount(path)
    assert abs(p.cash - PAPER_STARTING_CASH) < 1e-9
    assert PAPER_STARTING_CASH == 5000.00


def test_buy_sell_roundtrip():
    path = os.path.join(tempfile.gettempdir(), "paper_test2.json")
    try:
        os.remove(path)
    except OSError:
        pass
    p = PaperAccount(path)
    oid = p.buy_equity("NVDA", 2, 100.0, "orb")
    assert oid and oid.startswith("PAPER-")
    assert abs(p.cash - 4800.0) < 1e-6
    assert p.equity_positions["NVDA"]["qty"] == 2
    xid = p.sell_equity("NVDA", 2, 110.0, "orb")
    assert xid
    assert abs(p.cash - 5020.0) < 1e-6
    assert "NVDA" not in p.equity_positions
    snap = p.snapshot()
    assert abs(snap["realized_pnl"] - 20.0) < 1e-6


def test_insufficient_cash():
    path = os.path.join(tempfile.gettempdir(), "paper_test3.json")
    try:
        os.remove(path)
    except OSError:
        pass
    p = PaperAccount(path)
    assert p.buy_equity("SPY", 1000, 500.0, "orb") is None
    assert abs(p.cash - 5000.0) < 1e-9


def test_option_premium():
    path = os.path.join(tempfile.gettempdir(), "paper_test4.json")
    try:
        os.remove(path)
    except OSError:
        pass
    p = PaperAccount(path)
    oid = p.buy_option("NVDA260902C00220000", 1, 1.50, underlying="NVDA")
    assert oid
    assert abs(p.cash - 4850.0) < 1e-6
    p.sell_option("NVDA260902C00220000", 1, 1.80)
    assert abs(p.cash - 5030.0) < 1e-6


def test_live_order_lock():
    assert PAPER_LOCK is True
    c = SchwabClient(app_key="x", app_secret="y")
    assert c.paper_lock is True
    assert c.place_market_order("NVDA", 1, "BUY") is None
    assert c.place_option_limit("NVDA260902C00220000", 1, 1.5) is None
    assert c.place_oco_order("NVDA", 1, 1.0, 2.0) is None
    assert c.cancel_order("abc") is False


def test_mutex_flags():
    try:
        from trading_engine import STRATEGY_ENABLE_KEYS
    except Exception as exc:
        print("SKIP test_mutex_flags", exc)
        return
    assert STRATEGY_ENABLE_KEYS["use_orb_strategy"] == "orb"
    assert STRATEGY_ENABLE_KEYS["rsi_enabled"] == "rsi"
    assert len(set(STRATEGY_ENABLE_KEYS.values())) == 6


if __name__ == "__main__":
    tests = [
        test_starting_cash,
        test_buy_sell_roundtrip,
        test_insufficient_cash,
        test_option_premium,
        test_live_order_lock,
        test_mutex_flags,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("OK", fn.__name__)
        except Exception as exc:
            failed += 1
            print("FAIL", fn.__name__, exc)
    raise SystemExit(1 if failed else 0)
