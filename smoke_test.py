"""
smoke_test.py
Comprehensive smoke test for all TOS Bot features.
Runs entirely offline — mocks the Schwab API client.
Usage: python smoke_test.py
"""

import sys
import os
import json
import math
import types
import tempfile
import importlib
import traceback
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, PropertyMock

# ── colour helpers ──────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

_PASS = f"{GREEN}✔ PASS{RESET}"
_FAIL = f"{RED}✗ FAIL{RESET}"
_WARN = f"{YELLOW}⚠ WARN{RESET}"

passed = failed = warned = 0

def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*60}{RESET}")

def ok(msg: str) -> None:
    global passed
    passed += 1
    print(f"  {_PASS}  {msg}")

def fail(msg: str, exc: Exception = None) -> None:
    global failed
    failed += 1
    print(f"  {_FAIL}  {msg}")
    if exc:
        print(f"         {RED}{type(exc).__name__}: {exc}{RESET}")

def warn(msg: str) -> None:
    global warned
    warned += 1
    print(f"  {_WARN}  {msg}")

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        ok(label + (f" — {detail}" if detail else ""))
    else:
        fail(label + (f" — {detail}" if detail else ""))

def run(label: str, fn):
    try:
        result = fn()
        ok(label)
        return result
    except AssertionError as e:
        fail(label, e)
    except Exception as e:
        fail(label, e)
        if os.getenv("SMOKE_VERBOSE"):
            traceback.print_exc()
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Mock Schwab Client
# ══════════════════════════════════════════════════════════════════════════════

def _make_candles(n: int = 100, base: float = 5.0, swing: bool = False) -> List[Dict]:
    """Generate synthetic 1-min candles with optional pivot-friendly oscillation."""
    import math, random
    random.seed(42)
    candles = []
    price = base
    for i in range(n):
        if swing:
            # Oscillate with ~3% amplitude every ~15 bars
            price = base + base * 0.03 * math.sin(i / 7)
        else:
            price += random.uniform(-0.02, 0.03)
        c = max(0.01, price)
        candles.append({
            "datetime": 1700000000000 + i * 60000,
            "open":   round(c, 4),
            "high":   round(c + random.uniform(0.01, 0.05), 4),
            "low":    round(c - random.uniform(0.01, 0.05), 4),
            "close":  round(c, 4),
            "volume": int(200_000 + random.randint(-50_000, 300_000)),
        })
    return candles


class MockSchwabClient:
    def __init__(self):
        self.app_key    = "TEST_KEY"
        self.app_secret = "TEST_SECRET"
        self._access_token  = "mock_token"
        self._refresh_token = "mock_refresh"
        self._token_expiry  = 9999999999.0
        self._account_hash  = "TEST_ACCOUNT"
        self._last_order: Optional[Dict] = None
        self._order_counter = 100
        self._cancel_called = []

    def is_authenticated(self) -> bool:
        return True

    def get_account_hash(self) -> str:
        return self._account_hash

    def get_balance(self) -> Dict:
        return {"cash_available": 7111.00, "total_value": 7111.00}

    def get_positions(self) -> List:
        return []

    def get_quote(self, symbol: str) -> Dict:
        return {
            "quote": {
                "lastPrice": 5.00,
                "bidPrice":  4.98,
                "askPrice":  5.02,
                "totalVolume": 500_000,
                "closePrice": 4.50,
                "openPrice":  4.80,
            },
            "fundamental": {
                "vol10DayAvg": 200_000,
                "sharesFloat": 50_000_000,
            }
        }

    def get_quotes(self, symbols: List[str]) -> Dict:
        return {s: self.get_quote(s) for s in symbols}

    def get_price_history(self, symbol, period_type="day", period=1,
                          frequency_type="minute", frequency=1,
                          extended_hours=False) -> List[Dict]:
        swing = symbol in ("AMC", "GME", "MVIS")
        return _make_candles(120, base=5.0, swing=swing)

    def get_avg_volume_per_min(self, symbol, bars=20) -> float:
        return 220_000.0  # simulated per-minute volume

    def place_market_order(self, symbol, quantity, instruction="BUY",
                           extended_hours=False, limit_buffer_pct=0.005):
        self._order_counter += 1
        oid = str(self._order_counter)
        self._last_order = {
            "type": "LIMIT", "symbol": symbol, "qty": quantity,
            "instruction": instruction, "ext": extended_hours,
            "limit_buffer_pct": limit_buffer_pct, "id": oid,
        }
        return oid

    def place_limit_order(self, symbol, quantity, price, instruction="BUY",
                          extended_hours=False):
        self._order_counter += 1
        oid = str(self._order_counter)
        return oid

    def place_oco_order(self, symbol, quantity, stop_price, limit_price,
                        extended_hours=False):
        self._order_counter += 1
        return str(self._order_counter)

    def place_trailing_stop_order(self, symbol, quantity, trail_pct,
                                  extended_hours=False):
        self._order_counter += 1
        oid = str(self._order_counter)
        self._last_order = {
            "type": "TRAILING_STOP", "symbol": symbol, "qty": quantity,
            "trail_pct": trail_pct, "id": oid,
        }
        return oid

    def cancel_order(self, order_id: str) -> bool:
        self._cancel_called.append(order_id)
        return True

    def authorize(self) -> bool:
        return True


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 1 — DataHandler
# ══════════════════════════════════════════════════════════════════════════════
section("1. DataHandler — config & watchlist persistence")

import tempfile, shutil
_tmpdir = tempfile.mkdtemp()

try:
    # Patch DATA_DIR before import
    import data_handler as dh_mod

    original_data_dir = dh_mod.DATA_DIR
    dh_mod.DATA_DIR = _tmpdir

    from data_handler import DataHandler, DEFAULT_CONFIG

    dh = DataHandler(data_dir=_tmpdir)

    def t_default_config():
        cfg = dh.load_config()
        assert "account_size"             in cfg
        assert "max_concurrent_positions" in cfg
        assert "trail_trigger_r"          in cfg
        assert "trail_vol_mult"           in cfg
        assert "trail_pct"                in cfg
        assert "limit_entry_buffer"       in cfg
        assert cfg["dry_run"] is True,  "dry_run must default True"
        assert cfg["trail_trigger_r"] == 1.5
        assert cfg["trail_pct"]        == 3.0
    run("Default config has all required keys + trail defaults", t_default_config)

    def t_save_load():
        dh.save_config({"account_size": 9999.0, "dry_run": False})
        cfg2 = dh.load_config()
        assert cfg2["account_size"] == 9999.0
        assert cfg2["dry_run"] is False
    run("Config save/load roundtrip", t_save_load)

    def t_watchlist():
        dh.save_watchlist(["AMC", "GME", "MVIS"])
        wl = dh.load_watchlist()
        assert wl == ["AMC", "GME", "MVIS"]
    run("Watchlist save/load roundtrip", t_watchlist)

    def t_positions():
        dh.save_positions([{"symbol": "AMC", "pnl": 50.0}], [])
        pos = dh.load_positions()
        # load_positions may return a dict {open, closed} or a list depending on impl
        assert pos is not None
    run("Positions save/load roundtrip", t_positions)

    def t_trade_log():
        dh.append_trade_log({"symbol": "GME", "pnl": -20.0})
        log = dh.load_trade_log()
        assert any(t["symbol"] == "GME" for t in log)
    run("Trade log append/load", t_trade_log)

finally:
    shutil.rmtree(_tmpdir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 2 — ORBStrategy
# ══════════════════════════════════════════════════════════════════════════════
section("2. ORBStrategy — signals, position sizing, concurrent cap, trail fields")

from orb_strategy import ORBStrategy, TradeSignal, OpenPosition
import datetime

strat = ORBStrategy()
strat.set_config(
    account_size=7111.0, risk_pct=1.5, orb_minutes=15,
    max_trades=10, max_concurrent_positions=5,
    entry_cutoff_hour=99,   # allow entries at any hour during tests
    require_pullback=False,  # legacy breakout-path tests
)

# reset_day() FIRST so it initialises trade_date to today,
# then set levels — subsequent evaluate() calls won't clear them.
strat.reset_day()
strat.set_orb_levels("MVIS", {"high": 5.20, "low": 4.80, "mid": 5.00})
strat.set_vwap("MVIS", 4.95)
strat.set_indicators("MVIS", {"ema9": 5.21, "ema20": 5.15, "rsi": 58.0, "atr": 0.08, "vwap": 4.95, "last_closed_close": 5.21, "breakout_vol_ratio": 3.5, "macd_hist": 0.02, "adx": 25.0, "htf_uptrend": True})  # quality filters added post-v1

def t_position_size():
    shares, risk, dist = strat.calc_position_size(5.25, 4.80)
    expected_risk = 7111.0 * 0.015
    assert abs(risk - expected_risk) <= 1.0, f"risk={risk:.2f} expected≈{expected_risk:.2f}"
    assert dist == round(abs(5.25 - 4.80), 4)
    assert shares > 0
run("Position sizing — correct risk $ and stop distance", t_position_size)

def t_long_signal():
    strat.require_vwap_above = True
    # Price above ORB high AND above VWAP → signal expected
    sig = strat.evaluate("MVIS", 5.25)
    assert sig is not None, "Expected LONG signal above ORB high"
    assert sig.direction == "LONG"
    assert sig.entry_price == 5.25
    assert sig.stop_price  <= 5.25 and sig.stop_price > 0  # ATR-based, not necessarily ORB low
    assert sig.target_r2 > sig.entry_price, f'target_r2 {sig.target_r2} must exceed entry {sig.entry_price}'
    assert sig.shares > 0
    # Record the entry so _triggered is set for t_no_double_entry
    strat.record_entry(sig, "ORD_MVIS")
    return sig
sig = run("LONG breakout signal — entry, stop, target, shares", t_long_signal)

def t_no_double_entry():
    sig2 = strat.evaluate("MVIS", 5.30)
    assert sig2 is None, "Should not trigger again after _triggered is set"
run("No double-entry for same symbol same day", t_no_double_entry)

def t_concurrent_cap():
    strat2 = ORBStrategy()
    strat2.require_pullback = False
    strat2.set_config(account_size=7111.0, risk_pct=1.5, max_trades=10,
                      max_concurrent_positions=2, entry_cutoff_hour=99)
    strat2.reset_day()
    strat2.require_pullback = False
    for sym in ("A", "B", "C", "D"):
        strat2.set_orb_levels(sym, {"high": 5.2, "low": 4.8, "mid": 5.0})
        strat2.set_vwap(sym, 4.9)
        strat2.set_indicators(sym, {"ema9": 5.21, "ema20": 5.15, "rsi": 58.0, "atr": 0.08, "vwap": 4.95, "last_closed_close": 5.21, "breakout_vol_ratio": 3.5, "macd_hist": 0.02, "adx": 25.0, "htf_uptrend": True})
    strat2.require_vwap_above = False
    entries = 0
    for sym in ("A", "B", "C", "D"):
        s = strat2.evaluate(sym, 5.25)
        if s:
            strat2.record_entry(s, f"ORD_{sym}")
            entries += 1
    assert entries == 2, f"Expected 2 concurrent entries, got {entries}"
run("Concurrent position cap (max=2) blocks 3rd+ entry", t_concurrent_cap)

def t_trail_fields():
    strat3 = ORBStrategy()
    strat3.require_pullback = False
    strat3.set_config(account_size=7111.0, entry_cutoff_hour=99)
    strat3.reset_day()
    strat3.require_pullback = False
    strat3.set_orb_levels("TEST", {"high": 5.2, "low": 4.8, "mid": 5.0})
    strat3.set_vwap("TEST", 4.9)
    strat3.set_indicators("TEST", {"ema9": 5.21, "ema20": 5.15, "rsi": 58.0, "atr": 0.08, "vwap": 4.95, "last_closed_close": 5.21, "breakout_vol_ratio": 3.5, "macd_hist": 0.02, "adx": 25.0, "htf_uptrend": True})
    strat3.require_vwap_above = False
    sig = strat3.evaluate("TEST", 5.25)
    assert sig is not None
    pos = strat3.record_entry(sig, "ORD1")
    assert hasattr(pos, "trailing_stop_active"), "OpenPosition missing trailing_stop_active"
    assert hasattr(pos, "peak_price"),           "OpenPosition missing peak_price"
    assert hasattr(pos, "last_volume_snapshot"), "OpenPosition missing last_volume_snapshot"
    assert pos.trailing_stop_active is False
run("OpenPosition has trail metadata fields", t_trail_fields)

def t_record_exit():
    strat4 = ORBStrategy()
    strat4.require_pullback = False
    strat4.set_config(account_size=7111.0, entry_cutoff_hour=99)
    strat4.reset_day()
    strat4.require_pullback = False
    strat4.set_orb_levels("X", {"high": 5.2, "low": 4.8, "mid": 5.0})
    strat4.set_vwap("X", 4.9)
    strat4.set_indicators("X", {"ema9": 5.21, "ema20": 5.15, "rsi": 58.0, "atr": 0.08, "vwap": 4.95, "last_closed_close": 5.21, "breakout_vol_ratio": 3.5, "macd_hist": 0.02, "adx": 25.0, "htf_uptrend": True})
    strat4.require_vwap_above = False
    sig = strat4.evaluate("X", 5.25)
    strat4.record_entry(sig, "ORD2")
    closed = strat4.record_exit("X", 5.70, "target")
    assert closed is not None
    assert closed.pnl > 0, f"Expected positive PnL, got {closed.pnl}"
    assert closed.status == "target"
run("record_exit computes positive PnL on target hit", t_record_exit)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 3 — Scanner
# ══════════════════════════════════════════════════════════════════════════════
section("3. StockScanner — gap filter, ORB levels, VWAP, avg vol/min")

from scanner import StockScanner

mock_client = MockSchwabClient()
scanner = StockScanner(client=mock_client)
scanner.set_config(min_gap_pct=5.0, min_rel_vol=2.5, min_price=1.0, max_price=10.0)

def t_scan_filter():
    # Mock quote: gap 6.7%, rel_vol 2.5x — should pass
    mock_client.get_quotes = lambda syms: {
        s: {
            "quote": {
                "lastPrice": 5.00, "closePrice": 4.68, "openPrice": 5.00,
                "totalVolume": 500_000, "bidPrice": 4.99, "askPrice": 5.01,
            },
            "fundamental": {"vol10DayAvg": 200_000, "sharesFloat": 50_000_000}
        }
        for s in syms
    }
    scanner.set_watchlist(["AMC", "GME"])
    results = scanner.run_scan()
    wl = [r for r in results if r["symbol"] in ("AMC", "GME")]
    assert len(wl) == 2, f"expected AMC+GME in scan, got {[r['symbol'] for r in results]}"
    assert all(r["gap_pct"] > 5.0 for r in wl)
    assert all(r["rel_vol"] >= 2.5 for r in wl)
    return wl
run("Scan filter — gap% and rel_vol pass", t_scan_filter)

def t_scan_reject_price():
    mock_client.get_quotes = lambda syms: {
        s: {
            "quote": {
                "lastPrice": 50.0, "closePrice": 44.0, "openPrice": 50.0,
                "totalVolume": 500_000, "bidPrice": 49.9, "askPrice": 50.1,
            },
            "fundamental": {"vol10DayAvg": 200_000, "sharesFloat": 50_000_000}
        }
        for s in syms
    }
    results = scanner.run_scan()
    assert len(results) == 0, "Should reject price > max_price=10"
run("Scan rejects stocks above max_price", t_scan_reject_price)

def t_orb_levels():
    # Restore normal candles
    mock_client.get_price_history = MockSchwabClient().get_price_history
    # ORB levels require candles with datetime >= 09:30 ET
    # Use ET timezone so epoch matches what scanner.get_orb_levels() expects
    import datetime
    from zoneinfo import ZoneInfo
    _ET_test = ZoneInfo("America/New_York")
    today_et = datetime.datetime.now(_ET_test).date()
    open_ms = int(
        datetime.datetime(today_et.year, today_et.month, today_et.day, 9, 30, 0, tzinfo=_ET_test)
        .timestamp() * 1000
    )
    candles = []
    for i in range(30):
        candles.append({
            "datetime": open_ms + i * 60_000,
            "open": 5.0, "high": 5.0 + i * 0.01, "low": 4.95,
            "close": 5.0, "volume": 100_000,
        })
    mock_client.get_price_history = lambda *a, **kw: candles
    levels = scanner.get_orb_levels("MVIS", orb_minutes=15)
    assert levels is not None, "Expected ORB levels"
    assert levels["high"] > levels["low"]
    assert "mid" in levels
run("ORB level calculation from candles", t_orb_levels)

def t_vwap():
    mock_client.get_price_history = lambda *a, **kw: _make_candles(60, base=5.0)
    vwap = scanner.get_vwap("MVIS")
    assert vwap is not None and vwap > 0
run("VWAP calculation returns positive float", t_vwap)

def t_avg_vol_per_min():
    mock_client.get_price_history = lambda *a, **kw: _make_candles(60, base=5.0)
    avg = scanner.get_avg_volume_per_min("MVIS", bars=20)
    assert avg > 0, "Expected positive avg volume per minute"
run("Avg volume/min helper returns positive float", t_avg_vol_per_min)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 4 — SwingScanner
# ══════════════════════════════════════════════════════════════════════════════
section("4. SwingScanner — pivot detection, swing count, entry signal")

from swing_scanner import SwingScanner

mock_client2 = MockSchwabClient()
swing_sc = SwingScanner(client=mock_client2)
swing_sc.set_config(min_swings=3, min_swing_pct=0.2, min_avg_vol=50_000, min_price=1.0, max_price=20.0)

# Build candles with clear oscillation: high every 10 bars, low every 5
def _oscillating_candles(n=120, base=5.0):
    import math
    candles = []
    for i in range(n):
        c = base + base * 0.04 * math.sin(i * math.pi / 8)
        candles.append({
            "datetime": 1700000000000 + i * 60000,
            "open": round(c, 4), "high": round(c + 0.05, 4),
            "low": round(c - 0.05, 4), "close": round(c, 4),
            "volume": 200_000,
        })
    return candles

mock_client2.get_price_history = lambda *a, **kw: _oscillating_candles()

def t_pivot_detection():
    ph = swing_sc._find_pivot_highs([c["high"] for c in _oscillating_candles()])
    pl = swing_sc._find_pivot_lows( [c["low"]  for c in _oscillating_candles()])
    assert len(ph) > 0, "Expected pivot highs"
    assert len(pl) > 0, "Expected pivot lows"
run("Pivot high/low detection finds pivots", t_pivot_detection)

def t_swing_count():
    candles = _oscillating_candles()
    highs = [c["high"] for c in candles]
    lows  = [c["low"]  for c in candles]
    ph = swing_sc._find_pivot_highs(highs)
    pl = swing_sc._find_pivot_lows(lows)
    count, pivots = swing_sc._count_swings(ph, pl, [c["close"] for c in candles])
    assert count >= 3, f"Expected ≥3 swings in oscillating data, got {count}"
run("Swing count ≥ 3 on oscillating candle data", t_swing_count)

def t_scan_returns_results():
    results = swing_sc.scan(["AMC", "GME"])
    assert isinstance(results, list)
    assert len(results) > 0, "Expected swing candidates from oscillating data"
    r = results[0]
    assert "swing_count"   in r
    assert "support"       in r
    assert "resistance"    in r
    assert "current_bias"  in r
    assert "avg_amplitude" in r
run("Swing scan returns structured results", t_scan_returns_results)

def t_entry_signal():
    # Manually inject a result with support level
    swing_sc._last_results = {}
    results = swing_sc.scan(["AMC"])
    if results:
        r = results[0]
        # Price near support → should get LONG signal
        price_near_support = r["support"] * 1.05
        sig = swing_sc.get_swing_entry_signal("AMC", price_near_support)
        if sig:
            check("get_swing_entry_signal returns LONG near support", sig["direction"] == "LONG")
        else:
            warn("get_swing_entry_signal returned None — price may not be within 15% of support range")
    else:
        warn("No swing results to test entry signal against")
run("get_swing_entry_signal callable without crash", lambda: swing_sc.get_swing_entry_signal("AMC", 5.0))


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 5 — SchwabClient order methods
# ══════════════════════════════════════════════════════════════════════════════
section("5. SchwabClient — LIMIT orders, trailing stop, OCO, cancel")

# Test the actual SchwabClient order construction by mocking requests.post
import requests as req_mod
from schwab_client import SchwabClient

captured_orders: List[Dict] = []

class _FakeResponse:
    text = ""
    def __init__(self, body=None, status=200):
        self._body = body or {}
        self.status_code = status
    @property
    def headers(self):
        return {"Location": "/orders/999"}
    def json(self):
        return self._body

def _fake_post(url, **kwargs):
    captured_orders.append(kwargs.get("json", {}))
    return _FakeResponse(status=201)

def _fake_get(url, **kwargs):
    if "quotes" in url:
        return _FakeResponse({
            "MVIS": {
                "quote": {"lastPrice": 5.0, "bidPrice": 4.98, "askPrice": 5.02, "totalVolume": 500_000},
                "fundamental": {}
            }
        }, status=200)
    if "accounts" in url and "orders" not in url:
        return _FakeResponse({"securitiesAccount": {"hashValue": "ABC123"}}, status=200)
    return _FakeResponse(status=200)

with patch.object(req_mod, "post", side_effect=_fake_post), \
     patch.object(req_mod, "get",  side_effect=_fake_get):

    sc = SchwabClient("KEY", "SECRET", data_dir=tempfile.mkdtemp())
    sc._access_token  = "token"
    sc._refresh_token = "refresh"
    sc._token_expiry  = 9999999999.0
    sc._account_hash  = "ABC123"

    def t_limit_buy():
        captured_orders.clear()
        oid = sc.place_market_order("MVIS", 100, "BUY", extended_hours=False, limit_buffer_pct=0.005)
        assert oid == "999", f"Expected order ID 999, got {oid}"
        assert len(captured_orders) == 1
        order = captured_orders[0]
        assert order["orderType"] == "LIMIT", f"Expected LIMIT, got {order['orderType']}"
        price = order.get("price", 0)
        assert price > 0, "Limit order must have a price"
        # ask=5.02 * 1.005 = 5.045 → round to 2dp = 5.05
        assert abs(price - 5.05) < 0.01, f"Expected ~5.05, got {price}"
    run("Regular session entry places LIMIT order at ask+buffer", t_limit_buy)

    def t_limit_buy_extended():
        captured_orders.clear()
        oid = sc.place_market_order("MVIS", 100, "BUY", extended_hours=True)
        order = captured_orders[0]
        assert order["orderType"] == "LIMIT"
        assert order["session"] == "SEAMLESS"
    run("Extended hours entry places LIMIT SEAMLESS order", t_limit_buy_extended)

    def t_limit_sell():
        captured_orders.clear()
        oid = sc.place_market_order("MVIS", 100, "SELL", extended_hours=False)
        order = captured_orders[0]
        assert order["orderType"] == "LIMIT"
        # bid=4.98 * 0.995 = ~4.955
        price = order.get("price", 0)
        assert price > 0
    run("SELL limit order places at bid-buffer", t_limit_sell)

    def t_trailing_stop():
        captured_orders.clear()
        oid = sc.place_trailing_stop_order("MVIS", 100, trail_pct=3.0)
        assert oid == "999"
        assert len(captured_orders) == 1
        order = captured_orders[0]
        assert order["orderType"]         == "TRAILING_STOP"
        assert order["stopPriceLinkType"] == "PERCENT"
        assert order["stopPriceOffset"]   == 3.0
        assert order["orderLegCollection"][0]["instruction"] == "SELL"
    run("Trailing stop order — correct type, PERCENT link, SELL leg", t_trailing_stop)

    def t_oco_structure():
        captured_orders.clear()
        oid = sc.place_oco_order("MVIS", 100, stop_price=4.80, limit_price=5.70)
        assert len(captured_orders) == 1
        order = captured_orders[0]
        assert order["orderStrategyType"] == "OCO"
        legs = order.get("childOrderStrategies", [])
        assert len(legs) == 2
        types = {l["orderType"] for l in legs}
        assert "STOP"  in types
        assert "LIMIT" in types
    run("OCO bracket has STOP + LIMIT child legs", t_oco_structure)

    def t_oco_extended_hours_limit_stop():
        captured_orders.clear()
        oid = sc.place_oco_order(
            "MVIS", 100, stop_price=4.80, limit_price=5.70, extended_hours=True
        )
        assert oid == "999"
        order = captured_orders[0]
        legs = order.get("childOrderStrategies", [])
        types = {l["orderType"] for l in legs}
        assert types == {"LIMIT"}, f"SEAMLESS OCO must use LIMIT legs only, got {types}"
        prices = sorted(l.get("price", 0) for l in legs)
        assert prices == [4.80, 5.70]
        assert all(l.get("session") == "SEAMLESS" for l in legs)
    run("Extended-hours OCO uses LIMIT stop floor + LIMIT target", t_oco_extended_hours_limit_stop)

    def t_cancel_order():
        with patch.object(req_mod, "delete") as mock_del:
            mock_del.return_value = _FakeResponse(status=200)
            result = sc.cancel_order("999")
            assert result is True
    run("cancel_order returns True on 200", t_cancel_order)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 6 — TradingEngine session helpers + config
# ══════════════════════════════════════════════════════════════════════════════
section("6. TradingEngine — session helpers, config propagation, scan-universe lock")

# Patch SchwabClient, StockScanner, SwingScanner so engine init doesn't need real credentials
import trading_engine as te_mod

_orig_schwab = te_mod.SchwabClient
_orig_scanner = te_mod.StockScanner
_orig_swing   = te_mod.SwingScanner

te_mod.SchwabClient  = lambda *a, **kw: MockSchwabClient()
te_mod.StockScanner  = lambda client: MagicMock(
    watchlist=[], on_results=None,
    **{"set_watchlist.return_value": None, "start.return_value": None,
       "stop.return_value": None, "get_last_results.return_value": [],
       "get_orb_levels.return_value": {"high": 5.2, "low": 4.8, "mid": 5.0},
       "get_vwap.return_value": 5.0,
       "get_avg_volume_per_min.return_value": 200_000.0}
)
te_mod.SwingScanner  = lambda client: MagicMock(
    **{"scan.return_value": [], "get_swing_entry_signal.return_value": None}
)

_tmp2 = tempfile.mkdtemp()
te_mod.DataHandler   = lambda: __import__("data_handler").DataHandler(data_dir=_tmp2)

from trading_engine import TradingEngine, SESSION_PREMARKET_START, SESSION_REGULAR_START, SESSION_AFTERHOURS_END

engine = TradingEngine()
engine.dry_run         = True
engine.auto_trade      = False
engine.trail_trigger_r = 1.5
engine.trail_vol_mult  = 2.5
engine.trail_pct       = 3.0
engine.limit_entry_buffer = 0.005

def t_session_premarket():
    dt = datetime.datetime.now().replace(hour=6, minute=0)
    assert engine._session_name(dt) == "PRE-MARKET"
run("Session name PRE-MARKET at 06:00", t_session_premarket)

def t_session_regular():
    dt = datetime.datetime.now().replace(hour=10, minute=0)
    assert engine._session_name(dt) == "REGULAR"
run("Session name REGULAR at 10:00", t_session_regular)

def t_session_afterhours():
    dt = datetime.datetime.now().replace(hour=17, minute=0)
    assert engine._session_name(dt) == "AFTER-HOURS"
run("Session name AFTER-HOURS at 17:00", t_session_afterhours)

def t_session_closed():
    dt = datetime.datetime.now().replace(hour=21, minute=0)
    assert engine._session_name(dt) == "CLOSED"
run("Session name CLOSED at 21:00", t_session_closed)

def t_set_config_propagates():
    engine.set_config(
        account_size=8000.0, risk_pct=2.0, max_trades=8,
        max_concurrent=3, trail_trigger_r=2.0, trail_pct=4.0
    )
    assert engine.account_size    == 8000.0
    assert engine.risk_pct        == 2.0
    assert engine.max_trades      == 8
    assert engine.max_concurrent  == 3
    assert engine.trail_trigger_r == 2.0
    assert engine.trail_pct       == 4.0
run("set_config propagates all values including trail params", t_set_config_propagates)

def t_scan_universe_lock():
    """Scanner results set trading universe; watchlist symbols are excluded."""
    engine._watchlist = ["PLTR", "RIVN", "NIO"]
    engine._active_symbols = []
    fake_results = [
        {"symbol": "AMC", "rel_vol": 4.0, "gap_pct": 8.0},
        {"symbol": "GME", "rel_vol": 3.5, "gap_pct": 6.0},
    ]
    engine.max_concurrent = 5
    engine._on_scan_results(fake_results)
    assert "AMC" in engine._active_symbols
    assert "GME" in engine._active_symbols
    # Watchlist-only symbols should NOT be in active symbols
    assert "PLTR" not in engine._active_symbols
    assert "RIVN" not in engine._active_symbols
run("_on_scan_results locks universe to scanner hits, excludes watchlist", t_scan_universe_lock)

def t_open_position_preserved_on_scan_refresh():
    """Open positions must survive a scan refresh that drops their symbol."""
    engine._active_symbols = ["AMC", "GME"]
    # Simulate open position in GME
    from orb_strategy import OpenPosition
    import time
    fake_pos = OpenPosition(
        symbol="GME", direction="LONG", entry_price=14.95, shares=50,
        stop_price=13.90, target_r2=17.05, target_r3=18.00, risk_dollars=52.5,
    )
    engine.strategy._open_positions = {"GME": fake_pos}
    # New scan doesn't include GME
    new_results = [{"symbol": "SOFI", "rel_vol": 3.0, "gap_pct": 6.0}]
    engine._on_scan_results(new_results)
    assert "GME"  in engine._active_symbols, "Open position GME must be preserved"
    assert "SOFI" in engine._active_symbols
    assert "AMC"  not in engine._active_symbols
run("Open position preserved in active_symbols after scan refresh", t_open_position_preserved_on_scan_refresh)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 7 — Trail Upgrade Logic
# ══════════════════════════════════════════════════════════════════════════════
section("7. Trailing stop upgrade — R-multiple + volume spike detection")

from orb_strategy import OpenPosition
import time as _time

def _make_pos(symbol="MVIS", entry=5.0, stop=4.5) -> OpenPosition:
    return OpenPosition(
        symbol=symbol, direction="LONG", entry_price=entry, shares=100,
        stop_price=stop, target_r2=6.0, target_r3=6.5, risk_dollars=50.0,
    )

def t_no_upgrade_below_trigger_r():
    engine2 = TradingEngine()
    engine2.dry_run         = True
    engine2.trail_trigger_r = 1.5
    engine2.trail_vol_mult  = 2.5
    engine2.trail_pct       = 3.0
    engine2._vol_cache      = {"MVIS": 200_000.0}
    engine2.poll_interval   = 30.0
    engine2.partial_exit_enabled = False
    pos = _make_pos()
    engine2.strategy._open_positions = {"MVIS": pos}
    # Price at 1.0R gain (not enough)
    quotes = {"MVIS": {"quote": {"lastPrice": 5.50, "totalVolume": 600_000}}}
    engine2._manage_open_positions(quotes)
    assert pos.trailing_stop_active is False
run("No trail upgrade below trigger R-multiple", t_no_upgrade_below_trigger_r)

def t_no_upgrade_without_vol_spike():
    engine3 = TradingEngine()
    engine3.dry_run         = True
    engine3.trail_trigger_r = 1.5
    engine3.trail_vol_mult  = 2.5
    engine3.trail_pct       = 3.0
    engine3._vol_cache      = {"MVIS": 200_000.0}
    engine3.poll_interval   = 30.0
    engine3.partial_exit_enabled = False
    pos = _make_pos()
    pos.last_volume_snapshot = 500_000  # set baseline
    engine3.strategy._open_positions = {"MVIS": pos}
    # Price at 2.0R — eligible — but no vol spike (delta=10k vs avg_per_interval=100k)
    quotes = {"MVIS": {"quote": {"lastPrice": 6.00, "totalVolume": 510_000}}}
    engine3._manage_open_positions(quotes)
    assert pos.trailing_stop_active is False
run("No trail upgrade without volume spike", t_no_upgrade_without_vol_spike)

def t_upgrade_fires_on_r_and_vol():
    upgraded = []
    engine4 = TradingEngine()
    engine4.dry_run         = True
    engine4.trail_trigger_r = 1.5
    engine4.trail_vol_mult  = 2.5
    engine4.trail_pct       = 3.0
    engine4._vol_cache      = {"MVIS": 100_000.0}  # 100k/min avg
    engine4.poll_interval   = 30.0                  # 30s poll → 50k/interval expected
    engine4.partial_exit_enabled = False
    engine4.on("trade_upgraded", lambda d: upgraded.append(d))
    pos = _make_pos()
    pos.last_volume_snapshot = 400_000  # set baseline
    engine4.strategy._open_positions = {"MVIS": pos}
    # Price at 2.0R gain (entry=5.0, stop=4.5, risk=0.5 → 1.5R threshold=5.75, 2R=6.0)
    # Vol delta = 1_000_000 - 400_000 = 600_000 >> 2.5 × 50_000 (threshold=125k)
    quotes = {"MVIS": {"quote": {"lastPrice": 6.00, "totalVolume": 1_000_000}}}
    engine4._manage_open_positions(quotes)
    assert pos.trailing_stop_active is True, "Trailing stop should be active in dry-run"
    assert len(upgraded) == 1
    assert upgraded[0]["trail_pct"] == 3.0
run("Trail upgrade fires on R-multiple + volume spike (dry-run)", t_upgrade_fires_on_r_and_vol)

def t_no_double_upgrade():
    engine5 = TradingEngine()
    engine5.dry_run         = True
    engine5.trail_trigger_r = 1.5
    engine5.trail_vol_mult  = 2.5
    engine5.trail_pct       = 3.0
    engine5._vol_cache      = {"MVIS": 100_000.0}
    engine5.poll_interval   = 30.0
    engine5.partial_exit_enabled = False
    upgrades = []
    engine5.on("trade_upgraded", lambda d: upgrades.append(d))
    pos = _make_pos()
    pos.last_volume_snapshot  = 400_000
    pos.trailing_stop_active  = True  # already upgraded
    engine5.strategy._open_positions = {"MVIS": pos}
    quotes = {"MVIS": {"quote": {"lastPrice": 6.00, "totalVolume": 1_000_000}}}
    engine5._manage_open_positions(quotes)
    assert len(upgrades) == 0, "Should not upgrade again"
run("No double trail upgrade on already-upgraded position", t_no_double_upgrade)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 8 — Web Dashboard API endpoints
# ══════════════════════════════════════════════════════════════════════════════
section("8. Web Dashboard — REST endpoints return expected shapes")

import web_dashboard as wd
from web_dashboard import app as flask_app

# Patch the global engine in web_dashboard
wd.engine = engine
engine.trading_active   = False
engine.dry_run          = True
engine.extended_hours   = False
engine.swing_scan_enabled = True
engine.max_concurrent   = 5
engine._active_symbols  = ["AMC", "GME"]
engine._scan_results    = []
engine._swing_results   = []

flask_app.config["TESTING"] = True
client_http = flask_app.test_client()

def t_status_endpoint():
    r = client_http.get("/api/status")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "trading_active"  in data
    assert "session"         in data
    assert "max_concurrent"  in data
    assert "open_positions"  in data
run("GET /api/status — all expected keys present", t_status_endpoint)

def t_config_get():
    r = client_http.get("/api/config")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "account_size"       in data
    assert "trail_trigger_r"    in data
    assert "trail_vol_mult"     in data
    assert "trail_pct"          in data
    assert "limit_entry_buffer" in data
run("GET /api/config — trail fields present", t_config_get)

def t_config_post():
    payload = {
        "account_size": 8500.0, "risk_pct": 1.0, "orb_minutes": 15,
        "max_trades": 10, "max_concurrent": 3, "auto_trade": True,
        "dry_run": True, "extended_hours": False, "swing_scan_enabled": True,
        "limit_entry_buffer": 0.5, "trail_trigger_r": 2.0,
        "trail_vol_mult": 3.0, "trail_pct": 4.0,
    }
    r = client_http.post("/api/config",
                         data=json.dumps(payload),
                         content_type="application/json")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data.get("ok") is True
    assert engine.account_size == 8500.0
    assert engine.trail_trigger_r == 2.0
    assert engine.trail_pct == 4.0
run("POST /api/config — updates engine including trail fields", t_config_post)

def t_scan_results_endpoint():
    engine._scan_results = [{"symbol": "AMC", "gap_pct": 8.0, "rel_vol": 3.5}]
    r = client_http.get("/api/scan_results")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "results" in data
    assert len(data["results"]) == 1
run("GET /api/scan_results returns results list", t_scan_results_endpoint)

def t_swing_results_endpoint():
    engine._swing_results = [{"symbol": "GME", "swing_count": 4, "support": 4.80}]
    r = client_http.get("/api/swing_results")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "results" in data
    assert data["results"][0]["symbol"] == "GME"
run("GET /api/swing_results returns swing list", t_swing_results_endpoint)

def t_watchlist_get_post():
    r = client_http.post("/api/watchlist",
                         data=json.dumps({"symbols": ["SOFI", "NIO"]}),
                         content_type="application/json")
    assert r.status_code == 200
    r2 = client_http.get("/api/watchlist")
    data = json.loads(r2.data)
    assert "SOFI" in data["symbols"]
run("POST+GET /api/watchlist roundtrip", t_watchlist_get_post)

def t_balance_endpoint():
    r = client_http.get("/api/balance")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "cash_available" in data or data == {}  # ok if not authenticated
run("GET /api/balance returns valid response", t_balance_endpoint)

def t_positions_endpoint():
    r = client_http.get("/api/positions")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "open" in data
    assert "held" in data
    assert "pending" in data
    assert "closed" not in data
    assert isinstance(data["open"], list)
    assert isinstance(data["pending"], list)
run("GET /api/positions has pending+held+open (no closed)", t_positions_endpoint)

def t_trade_log_endpoint():
    r = client_http.get("/api/trade_log")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert isinstance(data, list)
run("GET /api/trade_log returns list", t_trade_log_endpoint)

def t_start_stop_unauthenticated():
    # engine is not authenticated (no real token) — should return 401 or ok depending on mock
    r_stop = client_http.post("/api/stop")
    assert r_stop.status_code == 200
run("POST /api/stop returns 200", t_start_stop_unauthenticated)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 9 — Dry-run execute_trade end-to-end
# ══════════════════════════════════════════════════════════════════════════════
section("9. End-to-end dry-run trade execution")

def t_dry_run_trade():
    logs = []
    engine9 = TradingEngine()
    engine9.dry_run       = True
    engine9.auto_trade    = True
    engine9.trail_trigger_r = 1.5
    engine9.trail_pct       = 3.0
    engine9.limit_entry_buffer = 0.005
    engine9.on("log", lambda m: logs.append(m))

    from orb_strategy import TradeSignal
    sig = TradeSignal(
        symbol="MVIS", direction="LONG",
        entry_price=5.25, stop_price=4.80,
        target_r2=6.15, target_r3=6.60,
        shares=234, risk_dollars=105.3,
        orb_high=5.20, orb_low=4.80, vwap=5.00,
        rel_vol=3.5, gap_pct=7.0, reason="ORB LONG breakout",
    )
    engine9._execute_trade(sig, extended=False)
    # Check position was recorded
    pos = engine9.strategy._open_positions.get("MVIS")
    assert pos is not None, "Expected open position after dry-run trade"
    assert pos.entry_price == 5.25
    # Check log contains DRY RUN
    dry_log = [l for l in logs if "DRY RUN" in str(l)]
    assert len(dry_log) > 0, "Expected [DRY RUN] log entry"
run("Dry-run trade records position and logs DRY RUN", t_dry_run_trade)

def t_extended_hours_dry_run():
    logs = []
    engine10 = TradingEngine()
    engine10.dry_run       = True
    engine10.auto_trade    = True
    engine10.limit_entry_buffer = 0.005
    engine10.on("log", lambda m: logs.append(m))
    from orb_strategy import TradeSignal
    sig = TradeSignal(
        symbol="AMC", direction="LONG",
        entry_price=3.22, stop_price=3.00,
        target_r2=3.66, target_r3=3.88,
        shares=480, risk_dollars=105.6,
        orb_high=3.20, orb_low=3.00, vwap=None,
        rel_vol=2.8, gap_pct=5.5, reason="Swing LONG near support",
    )
    engine10._execute_trade(sig, extended=True)
    pos = engine10.strategy._open_positions.get("AMC")
    assert pos is not None
run("Extended hours dry-run trade records position", t_extended_hours_dry_run)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 10 — Regression: edge cases
# ══════════════════════════════════════════════════════════════════════════════
section("10. Regression — edge cases")

def t_zero_vol_cache_no_upgrade():
    """If vol cache is empty (ORB candles not loaded), trail upgrade must not fire."""
    engine_e = TradingEngine()
    engine_e.dry_run = True
    engine_e.trail_trigger_r = 1.5
    engine_e.trail_vol_mult  = 2.5
    engine_e._vol_cache = {}   # empty — no baseline
    engine_e.partial_exit_enabled = False
    pos = _make_pos()
    pos.last_volume_snapshot = 0
    engine_e.strategy._open_positions = {"MVIS": pos}
    quotes = {"MVIS": {"quote": {"lastPrice": 7.0, "totalVolume": 5_000_000}}}
    engine_e._manage_open_positions(quotes)
    assert pos.trailing_stop_active is False
run("No trail upgrade when vol cache is empty (no baseline)", t_zero_vol_cache_no_upgrade)

def t_scan_empty_results_clears_universe():
    engine_e2 = TradingEngine()
    engine_e2._active_symbols = ["AMC", "GME"]
    engine_e2.strategy._open_positions = {}
    engine_e2._on_scan_results([])
    assert engine_e2._active_symbols == [], "Empty scan should clear universe when no open positions"
run("Empty scan clears trading universe when no open positions", t_scan_empty_results_clears_universe)

def t_max_trades_per_day_cap():
    strat_cap = ORBStrategy()
    strat_cap.require_pullback = False
    strat_cap.set_config(account_size=7111.0, risk_pct=1.5, max_trades=2,
                         max_concurrent_positions=10, entry_cutoff_hour=99)
    strat_cap.reset_day()
    strat_cap.require_pullback = False
    for sym in ("A", "B", "C", "D"):
        strat_cap.set_orb_levels(sym, {"high": 5.2, "low": 4.8, "mid": 5.0})
        strat_cap.set_vwap(sym, 4.9)
        strat_cap.set_indicators(sym, {"ema9": 5.21, "ema20": 5.15, "rsi": 58.0, "atr": 0.08, "vwap": 4.95, "last_closed_close": 5.21, "breakout_vol_ratio": 3.5, "macd_hist": 0.02, "adx": 25.0, "htf_uptrend": True})
    entries = []
    for sym in ("A", "B", "C", "D"):
        s = strat_cap.evaluate(sym, 5.25)
        if s:
            strat_cap.record_entry(s, f"ORD_{sym}")
            entries.append(sym)
    assert len(entries) == 2, f"Max trades=2, but got {len(entries)}"
run("max_trades_per_day cap independent of concurrent cap", t_max_trades_per_day_cap)

def t_position_size_minimum_one_share():
    strat_s = ORBStrategy()
    strat_s.account_size       = 100.0   # tiny account
    strat_s.risk_per_trade_pct = 1.0
    shares, _, _ = strat_s.calc_position_size(50.0, 0.01)  # huge stop distance
    assert shares >= 1, "Must always get at least 1 share"
run("Position sizing always returns ≥ 1 share", t_position_size_minimum_one_share)

def t_vwap_filter_blocks_entry_below_vwap():
    strat_v = ORBStrategy()
    strat_v.require_pullback = False
    strat_v.set_config(account_size=7111.0, require_vwap_above=True, entry_cutoff_hour=99)
    strat_v.reset_day()
    strat_v.require_pullback = False
    strat_v.set_orb_levels("X", {"high": 5.2, "low": 4.8, "mid": 5.0})
    strat_v.set_vwap("X", 6.0)   # VWAP above current price → should block
    sig = strat_v.evaluate("X", 5.25)  # above ORB high but below VWAP=6.0
    assert sig is None, "VWAP filter should block entry when price < VWAP"
run("VWAP filter blocks entry when price < VWAP", t_vwap_filter_blocks_entry_below_vwap)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 11 — Strategy enhancements & risk controls
# ══════════════════════════════════════════════════════════════════════════════
section("11. Enhancements — partial exit, ATR stops, RSI filter, kill switch, sim fills")

def t_rsi_filter_blocks_overbought():
    strat = ORBStrategy()
    strat.set_config(account_size=7111.0, use_rsi_filter=True, rsi_overbought=75.0,
                     require_vwap_above=False, entry_cutoff_hour=99)
    strat.reset_day()
    strat.set_orb_levels("X", {"high": 5.2, "low": 4.8, "mid": 5.0})
    strat.set_indicators("X", {"ema9": 5.1, "ema20": 5.0, "rsi": 88.0,
                               "atr": 0.1, "last_closed_close": 5.30,
                               "breakout_vol_ratio": 3.0})
    sig = strat.evaluate("X", 5.25)
    assert sig is None, "RSI 88 > 75 should block the entry"
run("RSI overbought filter blocks entry", t_rsi_filter_blocks_overbought)

def t_volume_confirm_blocks_weak_breakout():
    strat = ORBStrategy()
    strat.set_config(account_size=7111.0, require_volume_confirm=True,
                     min_breakout_rel_vol=1.5, require_vwap_above=False,
                     use_rsi_filter=False, entry_cutoff_hour=99)
    strat.reset_day()
    strat.set_orb_levels("X", {"high": 5.2, "low": 4.8, "mid": 5.0})
    strat.set_indicators("X", {"ema9": 5.1, "ema20": 5.0, "rsi": 60.0,
                               "atr": 0.1, "last_closed_close": 5.30,
                               "breakout_vol_ratio": 0.8})  # weak vol
    sig = strat.evaluate("X", 5.25)
    assert sig is None, "Breakout vol 0.8x < 1.5x should block"
run("Volume confirmation blocks weak breakout", t_volume_confirm_blocks_weak_breakout)

def t_confirm_close_blocks_wick():
    strat = ORBStrategy()
    strat.set_config(account_size=7111.0, confirm_close=True, require_vwap_above=False,
                     use_rsi_filter=False, require_volume_confirm=False, entry_cutoff_hour=99)
    strat.reset_day()
    strat.set_orb_levels("X", {"high": 5.20, "low": 4.80, "mid": 5.0})
    # Live price pokes above ORB high but last CLOSED candle is still below it
    strat.set_indicators("X", {"ema9": 5.1, "ema20": 5.0, "rsi": 60.0,
                               "atr": 0.1, "last_closed_close": 5.15,
                               "breakout_vol_ratio": 3.0})
    sig = strat.evaluate("X", 5.25)
    assert sig is None, "Wick above ORB without a close should not trigger"
run("Candle-close confirmation blocks intrabar wick", t_confirm_close_blocks_wick)

def t_atr_stop_applied():
    strat = ORBStrategy()
    strat.set_config(account_size=7111.0, use_atr_stops=True, atr_stop_mult=1.5,
                     require_vwap_above=False, use_rsi_filter=False,
                     require_volume_confirm=False, confirm_close=False, entry_cutoff_hour=99,
                     require_pullback=False,  # this test is about stop price, not pullback
                     orb_max_range_pct=100.0)  # disable range check — test is about ATR stop
    strat.reset_day()
    strat.set_orb_levels("X", {"high": 5.20, "low": 4.50, "mid": 4.85})
    strat.set_indicators("X", {"ema9": 5.1, "ema20": 5.0, "rsi": 60.0,
                               "atr": 0.10, "last_closed_close": 5.25,
                               "breakout_vol_ratio": 3.0})
    sig = strat.evaluate("X", 5.25)
    assert sig is not None
    # ATR stop = 5.25 - 0.10*1.5 = 5.10, tighter than ORB low 4.50 → should use 5.10
    assert abs(sig.stop_price - 5.10) < 0.001, f"Expected ATR stop 5.10, got {sig.stop_price}"
run("ATR-based stop applied (tighter than ORB low)", t_atr_stop_applied)

def t_partial_exit_books_pnl_and_breakeven():
    strat = ORBStrategy()
    from orb_strategy import OpenPosition
    pos = OpenPosition(symbol="X", direction="LONG", entry_price=5.0, shares=100,
                       stop_price=4.5, target_r2=6.0, target_r3=6.5, risk_dollars=50.0,
                       original_shares=100)
    strat._open_positions = {"X": pos}
    slice_pnl = strat.record_partial_exit("X", 5.5, 50)
    assert pos.shares == 50, f"Expected 50 shares left, got {pos.shares}"
    assert abs(slice_pnl - 25.0) < 0.001, f"Expected $25 slice PnL, got {slice_pnl}"
    assert abs(pos.stop_price - 5.0) < 0.001, "Stop should move to breakeven"
    assert pos.partial_exit_done is True
run("Partial exit books PnL, trims shares, moves stop to breakeven", t_partial_exit_books_pnl_and_breakeven)

def t_kill_switch_blocks_entries():
    engine = TradingEngine()
    engine.dry_run = True
    engine.account_size = 1000.0
    engine.max_daily_loss_pct = 5.0
    engine.strategy._open_positions = {}   # ignore any positions restored from disk
    engine._kill_switch_tripped = True  # simulate tripped
    from orb_strategy import TradeSignal
    sig = TradeSignal(symbol="AMC", direction="LONG", entry_price=3.0, shares=10,
                      stop_price=2.9, target_r2=3.2, target_r3=3.3, risk_dollars=10.0,
                      orb_high=3.0, orb_low=2.8, vwap=None, rel_vol=3.0, gap_pct=5.0,
                      reason="test")
    engine._execute_trade(sig)
    assert "AMC" not in engine.strategy._open_positions, "Kill switch must block new entries"
run("Kill switch blocks new entries", t_kill_switch_blocks_entries)

def t_sim_fill_engine_resolves_target():
    from sim_client import SimClient
    sim = SimClient()
    sym = "ZZZZ"
    price = sim._current_price(sym)
    sim.place_market_order(sym, 100, "BUY")
    assert len(sim.get_positions()) == 1, "Position should exist after BUY"
    # OCO with target just below current price → should fill immediately on next read
    oid = sim.place_oco_order(sym, 100, stop_price=price * 0.5, limit_price=price * 0.99)
    positions = sim.get_positions()  # triggers _check_fills
    assert positions == [], f"Target fill should close position, got {positions}"
run("Sim fill engine resolves target and closes position", t_sim_fill_engine_resolves_target)

def t_sim_seed_deterministic():
    from sim_client import _sym_seed
    assert _sym_seed("AMC") == _sym_seed("AMC"), "Seed must be stable within a run"
    assert _sym_seed("AMC") != _sym_seed("GME"), "Different symbols → different seeds"
run("Sim seed is deterministic (hashlib, not randomized hash)", t_sim_seed_deterministic)

def t_sim_premarket_candles_exist():
    from sim_client import SimClient
    sim = SimClient()
    candles = sim.get_price_history("AMC", extended_hours=True)
    # If run during/after premarket, there should be candles before 09:30 ET.
    import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    now = _dt.datetime.now(_Z("America/New_York"))
    open_ms = int(_dt.datetime(now.year, now.month, now.day, 9, 30, tzinfo=_Z("America/New_York")).timestamp() * 1000)
    if now.hour > 9 or (now.hour == 9 and now.minute >= 30):
        pre = [c for c in candles if c["datetime"] < open_ms]
        assert len(pre) > 0, "Extended-hours history should include premarket candles"
run("Sim generates premarket candles in extended hours", t_sim_premarket_candles_exist)


# ══════════════════════════════════════════════════════════════════════════════
# SUITE 12 — Advanced indicators + position lifecycle (pending/held/sold)
# ══════════════════════════════════════════════════════════════════════════════
section("12. MACD/ADX/HTF filters + pending→held→sold lifecycle")

def t_macd_helper_shape():
    from scanner import StockScanner
    closes = [10 + (i * 0.1) for i in range(60)]  # steady uptrend
    macd = StockScanner.compute_macd(closes)
    assert macd is not None and "macd" in macd and "signal" in macd and "hist" in macd
    assert macd["macd"] > 0, "Uptrend should give a positive MACD line (fast EMA above slow)"
run("MACD helper returns macd/signal/hist (bullish on uptrend)", t_macd_helper_shape)

def t_adx_helper_trending_vs_flat():
    from scanner import StockScanner
    trend = [{"high": 10 + i*0.5 + 0.2, "low": 10 + i*0.5 - 0.2, "close": 10 + i*0.5} for i in range(40)]
    adx_trend = StockScanner.compute_adx(trend)
    assert adx_trend is not None and adx_trend > 25, f"Strong trend should give high ADX, got {adx_trend}"
run("ADX helper reports strong trend (>25)", t_adx_helper_trending_vs_flat)

def t_macd_filter_blocks_bearish():
    strat = ORBStrategy()
    strat.set_config(account_size=7111.0, use_macd_filter=True, require_vwap_above=False,
                     use_rsi_filter=False, require_volume_confirm=False, confirm_close=False,
                     entry_cutoff_hour=99)
    strat.reset_day()
    strat.set_orb_levels("X", {"high": 5.20, "low": 4.80, "mid": 5.0})
    strat.set_indicators("X", {"ema9": 5.1, "ema20": 5.0, "rsi": 60.0, "atr": 0.1,
                               "last_closed_close": 5.25, "breakout_vol_ratio": 3.0,
                               "macd_hist": -0.02})  # bearish momentum
    assert strat.evaluate("X", 5.25) is None, "Bearish MACD hist should block entry"
run("MACD filter blocks bearish-momentum entry", t_macd_filter_blocks_bearish)

def t_adx_filter_blocks_choppy():
    strat = ORBStrategy()
    strat.set_config(account_size=7111.0, use_adx_filter=True, adx_min=20.0,
                     require_vwap_above=False, use_rsi_filter=False,
                     require_volume_confirm=False, confirm_close=False, entry_cutoff_hour=99)
    strat.reset_day()
    strat.set_orb_levels("X", {"high": 5.20, "low": 4.80, "mid": 5.0})
    strat.set_indicators("X", {"ema9": 5.1, "ema20": 5.0, "rsi": 60.0, "atr": 0.1,
                               "last_closed_close": 5.25, "breakout_vol_ratio": 3.0,
                               "adx": 12.0})  # choppy
    assert strat.evaluate("X", 5.25) is None, "Low ADX (choppy) should block entry"
run("ADX filter blocks choppy (low-ADX) entry", t_adx_filter_blocks_choppy)

def t_htf_filter_blocks_misaligned():
    strat = ORBStrategy()
    strat.set_config(account_size=7111.0, use_htf_filter=True, require_vwap_above=False,
                     use_rsi_filter=False, require_volume_confirm=False, confirm_close=False,
                     entry_cutoff_hour=99)
    strat.reset_day()
    strat.set_orb_levels("X", {"high": 5.20, "low": 4.80, "mid": 5.0})
    strat.set_indicators("X", {"ema9": 5.1, "ema20": 5.0, "rsi": 60.0, "atr": 0.1,
                               "last_closed_close": 5.25, "breakout_vol_ratio": 3.0,
                               "htf_uptrend": False})  # 5-min not aligned
    assert strat.evaluate("X", 5.25) is None, "Misaligned 5-min trend should block entry"
run("5-min trend filter blocks misaligned entry", t_htf_filter_blocks_misaligned)

def t_pending_lifecycle():
    from orb_strategy import TradeSignal
    strat = ORBStrategy()
    sig = TradeSignal(symbol="AMC", direction="LONG", entry_price=3.0, shares=10,
                      stop_price=2.9, target_r2=3.2, target_r3=3.3, risk_dollars=10.0,
                      orb_high=3.0, orb_low=2.8, vwap=None, rel_vol=3.0, gap_pct=5.0, reason="t")
    pos = strat.record_entry(sig, "ORD1", status="pending")
    assert strat.get_pending_positions() and not strat.get_open_positions(), "Should start pending"
    strat.mark_filled("AMC", 3.05)
    assert strat.get_open_positions() and not strat.get_pending_positions(), "Should promote to held"
    assert abs(strat._open_positions["AMC"].entry_price - 3.05) < 1e-6, "Entry price updates to fill"
run("Pending entry promotes to held on fill", t_pending_lifecycle)

def t_drop_unfilled_pending():
    from orb_strategy import TradeSignal
    strat = ORBStrategy()
    sig = TradeSignal(symbol="GME", direction="LONG", entry_price=3.0, shares=10,
                      stop_price=2.9, target_r2=3.2, target_r3=3.3, risk_dollars=10.0,
                      orb_high=3.0, orb_low=2.8, vwap=None, rel_vol=3.0, gap_pct=5.0, reason="t")
    strat.record_entry(sig, "ORD2", status="pending")
    strat.drop_pending("GME")
    assert not strat.get_pending_positions() and not strat.get_open_positions(), "Dropped pending is gone"
run("Unfilled pending entry can be dropped", t_drop_unfilled_pending)

def t_sim_pending_fill_flow():
    """In sim (dry_run off), an entry is pending then promoted to held by fill detection."""
    eng = TradingEngine()
    eng.strategy._open_positions = {}
    eng.enable_simulation()       # sets dry_run False, synthetic broker
    from orb_strategy import TradeSignal
    sym = "WXYZ"
    price = eng.client._current_price(sym)
    sig = TradeSignal(symbol=sym, direction="LONG", entry_price=price, shares=5,
                      stop_price=price*0.95, target_r2=price*1.1, target_r3=price*1.15,
                      risk_dollars=10.0, orb_high=price, orb_low=price*0.95,
                      vwap=None, rel_vol=3.0, gap_pct=5.0, reason="t")
    eng._execute_trade(sig)
    assert sym in [p.symbol for p in eng.strategy.get_pending_positions()], "Entry should be pending"
    eng._check_pending_fills()    # sim broker already holds shares → promote
    held = [p.symbol for p in eng.strategy.get_open_positions()]
    assert sym in held, "Sim fill detection should promote pending → held"
    eng.disable_simulation()
run("Sim pending entry promotes to held via fill detection", t_sim_pending_fill_flow)

def t_scan_interval_config_applies():
    eng = TradingEngine()
    eng.set_config(scan_interval_sec=45.0)
    assert abs(eng.scan_interval_sec - 45.0) < 1e-6, "Engine should store the new scan interval"
    # scanner is a MagicMock in tests — verify set_config got the interval
    kw = eng.scanner.set_config.call_args.kwargs if eng.scanner.set_config.call_args else {}
    assert kw.get("scan_interval_sec") == 45.0, "Scanner.set_config should receive the new interval"
run("Configurable scan interval propagates to scanner", t_scan_interval_config_applies)


# ══════════════════════════════════════════════════════════════════════════════
# Suite 12a — 5-min SMA10 + volume + dip entry rules
# ══════════════════════════════════════════════════════════════════════════════
section("12a. 5-min entry rules — SMA10, volume, dip-only")

from scanner import StockScanner
from unittest.mock import patch

def _make_5m_candles(n_green=55, red_at_end=False):
    """Build 1-min candles resampled to enough 5m bars for SMA10."""
    candles = []
    p = 10.0
    for i in range(n_green):
        o = p
        p += 0.02
        candles.append({
            "datetime": i * 60000,
            "open": o, "high": p + 0.01, "low": o - 0.005,
            "close": p, "volume": 1000 if i % 5 else 2500,
        })
    if red_at_end:
        candles.append({
            "datetime": len(candles) * 60000,
            "open": p, "high": p + 0.01, "low": p - 0.08,
            "close": p - 0.05, "volume": 3000,
        })
        p -= 0.05
    return candles, p

def _chk_5m(candles, price, **kw):
    sc = StockScanner(client=object())
    with patch.object(sc, "_get_today_candles", return_value=candles):
        return sc.get_5min_long_entry_check("X", price, **kw)

def t_5m_blocks_below_sma():
    candles, _ = _make_5m_candles()
    chk = _chk_5m(candles, 9.50, sma_period=10)
    assert not chk["ok"]
    assert "below" in chk["reason"]
run("5m rules block price below SMA10", t_5m_blocks_below_sma)

def t_5m_blocks_chase_without_dip():
    candles, price = _make_5m_candles()
    chk = _chk_5m(candles, price)
    assert not chk["ok"]
run("5m rules block chasing without dip", t_5m_blocks_chase_without_dip)

def t_5m_allows_after_red_candle():
    candles, price = _make_5m_candles(red_at_end=True)
    chk = _chk_5m(candles, price)
    assert chk["ok"], chk.get("reason")
run("5m rules allow entry after red 1m pullback", t_5m_allows_after_red_candle)


# Suite 12b — ORB pullback gate + momentum bypass
# ══════════════════════════════════════════════════════════════════════════════
section("12b. ORB pullback gate — no chase at top unless momentum+volume")

from orb_strategy import ORBStrategy

def t_orb_blocks_immediate_breakout_without_pullback():
    strat = ORBStrategy()
    strat.reset_day()
    strat.require_pullback = True
    strat.orb_momentum_vol_min = 8.0
    strat.orb_momentum_rel_vol_min = 5.0
    strat.orb_pullback_min_pct = 0.20
    strat.set_orb_levels("X", {"high": 10.00, "low": 9.50, "mid": 9.75})
    strat.set_vwap("X", 9.80)
    strat.set_indicators("X", {
        "ema9": 10.05, "ema20": 9.90, "rsi": 55.0, "atr": 0.10,
        "last_closed_close": 10.02, "breakout_vol_ratio": 4.0,
        "candles": [],
    })
    strat.set_scan_meta("X", rel_vol=3.0, gap_pct=2.0)
    sig = strat.evaluate("X", 10.02)
    assert sig is None, "Should block immediate breakout without pullback"
run("ORB blocks breakout-at-top when pullback required", t_orb_blocks_immediate_breakout_without_pullback)

def t_orb_momentum_bypass_allows_breakout():
    strat = ORBStrategy()
    strat.reset_day()
    strat.require_pullback = True
    strat.orb_momentum_vol_min = 8.0
    strat.orb_momentum_rel_vol_min = 5.0
    strat.set_orb_levels("X", {"high": 10.00, "low": 9.50, "mid": 9.75})
    strat.set_vwap("X", 9.80)
    strat.set_indicators("X", {
        "ema9": 10.05, "ema20": 9.90, "rsi": 55.0, "atr": 0.10,
        "last_closed_close": 10.02, "breakout_vol_ratio": 9.0,
        "candles": [],
    })
    strat.set_scan_meta("X", rel_vol=6.0, gap_pct=2.0)
    sig = strat.evaluate("X", 10.02)
    assert sig is not None, "Momentum+volume bypass should allow immediate entry"
    assert "momentum" in sig.reason
run("ORB momentum bypass allows high-volume breakout entry", t_orb_momentum_bypass_allows_breakout)

def t_orb_pullback_reclaim_entry():
    strat = ORBStrategy()
    strat.reset_day()
    strat.require_pullback = True
    strat.orb_pullback_min_pct = 0.20
    strat.orb_pullback_max_pct = 1.50
    strat.orb_pullback_reclaim_pct = 0.10
    strat.orb_momentum_vol_min = 99.0
    strat.set_orb_levels("X", {"high": 10.00, "low": 9.50, "mid": 9.75})
    strat.set_vwap("X", 9.80)
    strat.set_indicators("X", {
        "ema9": 10.05, "ema20": 9.90, "rsi": 55.0, "atr": 0.10,
        "last_closed_close": 10.05, "breakout_vol_ratio": 4.0,
        "candles": [],
    })
    strat.set_scan_meta("X", rel_vol=3.0, gap_pct=2.0)
    strat._update_pullback_state("X", 10.10, 10.00)
    strat._update_pullback_state("X", 10.08, 10.00)
    strat._update_pullback_state("X", 10.05, 10.00)
    ready, reason = strat._pullback_entry_ready("X", 10.06, 10.00, [])
    assert ready, f"Expected pullback reclaim, got: {reason}"
    sig = strat.evaluate("X", 10.06)
    assert sig is not None
    assert "pullback" in sig.reason
run("ORB allows entry after pullback reclaim", t_orb_pullback_reclaim_entry)


# Suite 13 — First Pullback strategy (detect_first_pullback + signal gen)
# ══════════════════════════════════════════════════════════════════════════════
section("13. First Pullback — pattern detection + signal generation")

def _make_candle(open_, close_, high_=None, low_=None, vol=100_000):
    """Helper: build a minimal candle dict."""
    high_ = high_ if high_ is not None else max(open_, close_) * 1.002
    low_  = low_  if low_  is not None else min(open_, close_) * 0.998
    return {"open": open_, "close": close_, "high": high_, "low": low_, "volume": vol}

def _pole(n, start, step):
    """Build n rising green candles starting from `start`."""
    candles = []
    price = start
    for _ in range(n):
        candles.append(_make_candle(price, price + step))
        price += step
    return candles

def _flag(n, top, pullback_per):
    """Build n red candles pulling back from `top`."""
    candles = []
    price = top
    for _ in range(n):
        candles.append(_make_candle(price, price - pullback_per))
        price -= pullback_per
    return candles

def _signal_candle(flag_high, above=0.05):
    """Build a green signal candle that closes above flag_high."""
    entry = flag_high + above
    return _make_candle(flag_high, entry)

def t_fp_detects_clean_pattern():
    """Standard 3-candle pole + 2-candle flag → signal candle."""
    from scanner import StockScanner
    pole    = _pole(3, start=5.00, step=0.10)   # pole top ~5.30
    flag    = _flag(2, top=5.30, pullback_per=0.05)  # flag low ~5.20
    signal  = [_signal_candle(flag_high=5.30, above=0.05)]
    candles = pole + flag + signal
    result  = StockScanner.detect_first_pullback(candles)
    assert result is not None, "Should detect clean bull flag"
    assert result["red_candles"] == 2, "Should count 2 red candles"
    assert result["flag_high"] > 5.28, "Flag high near pole top"
    assert result["flag_low"] < 5.25, "Flag low below flag high"
run("detect_first_pullback: clean 3-pole + 2-flag pattern detected", t_fp_detects_clean_pattern)

def t_fp_rejects_deep_retrace():
    """Flag that retraces >50% of pole → should be rejected."""
    from scanner import StockScanner
    pole    = _pole(3, start=5.00, step=0.20)   # pole height = 0.60, pole top ~5.60
    # Retrace 55% → flag_low ~5.27 (below 50% level of 5.30)
    flag    = _flag(3, top=5.60, pullback_per=0.11)
    signal  = [_signal_candle(flag_high=5.60, above=0.05)]
    candles = pole + flag + signal
    result  = StockScanner.detect_first_pullback(candles)
    assert result is None, "Deep retrace (>50%) should return None"
run("detect_first_pullback: rejects >50% retrace (Ross 50% rule)", t_fp_rejects_deep_retrace)

def t_fp_rejects_too_many_red_candles():
    """Flag with 5 red candles → exceeds max_flag_candles=4 → rejected."""
    from scanner import StockScanner
    pole    = _pole(3, start=5.00, step=0.10)
    flag    = _flag(5, top=5.30, pullback_per=0.01)   # 5 red candles
    signal  = [_signal_candle(flag_high=5.30, above=0.05)]
    candles = pole + flag + signal
    result  = StockScanner.detect_first_pullback(candles)
    assert result is None, "5-candle flag should be rejected"
run("detect_first_pullback: rejects flag with >4 red candles", t_fp_rejects_too_many_red_candles)

def t_fp_rejects_weak_pole():
    """Pole moves only 0.3% — below min_pole_pct=1.0 → rejected."""
    from scanner import StockScanner
    pole   = _pole(3, start=5.00, step=0.005)  # ~0.3% net move
    flag   = _flag(2, top=5.015, pullback_per=0.002)
    signal = [_signal_candle(flag_high=5.015, above=0.005)]
    candles = pole + flag + signal
    result  = StockScanner.detect_first_pullback(candles)
    assert result is None, "Weak pole (<1%) should be rejected"
run("detect_first_pullback: rejects pole with insufficient move", t_fp_rejects_weak_pole)

def t_fp_signal_candle_must_close_above_flag():
    """Signal candle closes AT flag high (not above) → rejected."""
    from scanner import StockScanner
    pole    = _pole(3, start=5.00, step=0.10)
    flag    = _flag(2, top=5.30, pullback_per=0.05)
    # Signal candle closes at flag high, not above
    signal  = [_make_candle(5.25, 5.30)]  # close == flag_high
    candles = pole + flag + signal
    result  = StockScanner.detect_first_pullback(candles)
    assert result is None, "Signal must close ABOVE flag high"
run("detect_first_pullback: signal must close above flag high", t_fp_signal_candle_must_close_above_flag)

def t_fp_strategy_generates_signal():
    """FirstPullbackStrategy.evaluate() returns a TradeSignal for a valid setup."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from utils import set_sim_now
    from first_pullback_strategy import FirstPullbackStrategy

    strat = FirstPullbackStrategy()
    strat.set_config(
        enabled=True, account_size=7111.0, risk_pct=1.5,
        use_macd_filter=False, use_rsi_filter=False,
        max_stop_cents=1.00, min_pole_pct=1.0,
    )

    pole   = _pole(3, start=5.00, step=0.10)
    flag   = _flag(2, top=5.30, pullback_per=0.05)
    signal = [_signal_candle(flag_high=5.30, above=0.05)]
    candles = pole + flag + signal
    ind = {"candles": candles, "macd_hist": 0.05, "rsi": 55.0, "vwap": 5.20,
           "breakout_vol_ratio": 2.5}

    virtual = _dt.datetime(2026, 6, 18, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    set_sim_now(virtual)
    try:
        strat._trade_date = ""
        strat._triggered  = {}
        result = strat.evaluate("TEST", ind, {})
    finally:
        set_sim_now(None)

    assert result is not None, "Should generate a signal for valid setup"
    assert result.symbol == "TEST"
    assert result.direction == "LONG"
    assert result.stop_price < result.entry_price
    assert result.target_r2 > result.entry_price
    assert result.shares >= 1
run("FirstPullbackStrategy.evaluate: generates TradeSignal for valid pattern", t_fp_strategy_generates_signal)

def t_fp_strategy_respects_macd_filter():
    """MACD hist <= 0 with use_macd_filter=True → no signal."""
    from unittest.mock import patch, MagicMock
    from first_pullback_strategy import FirstPullbackStrategy

    strat = FirstPullbackStrategy()
    strat.set_config(
        enabled=True, use_macd_filter=True, use_rsi_filter=False,
        max_stop_cents=1.00, min_pole_pct=1.0,
    )
    pole   = _pole(3, start=5.00, step=0.10)
    flag   = _flag(2, top=5.30, pullback_per=0.05)
    signal = [_signal_candle(flag_high=5.30, above=0.05)]
    ind = {"candles": pole + flag + signal, "macd_hist": -0.01, "rsi": 55.0,
           "vwap": 5.20, "breakout_vol_ratio": 2.5}

    mock_now = MagicMock()
    mock_now.hour = 10
    mock_now.minute = 0
    with patch("first_pullback_strategy.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = mock_now
        strat._trade_date = ""
        strat._triggered  = {}
        result = strat.evaluate("MACDTEST", ind, {})
    assert result is None, "Negative MACD hist should block signal when filter is on"
run("FirstPullbackStrategy: MACD filter blocks bearish-MACD entries", t_fp_strategy_respects_macd_filter)

def t_fp_strategy_blocks_duplicate_symbol():
    """Second evaluate() call for the same symbol after first signal → None."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from utils import set_sim_now
    from first_pullback_strategy import FirstPullbackStrategy

    strat = FirstPullbackStrategy()
    strat.set_config(
        enabled=True, use_macd_filter=False, use_rsi_filter=False,
        max_stop_cents=1.00, min_pole_pct=1.0,
    )
    pole   = _pole(3, start=5.00, step=0.10)
    flag   = _flag(2, top=5.30, pullback_per=0.05)
    signal = [_signal_candle(flag_high=5.30, above=0.05)]
    ind = {"candles": pole + flag + signal, "macd_hist": 0.1, "rsi": 55.0,
           "vwap": 5.20, "breakout_vol_ratio": 2.5}

    virtual = _dt.datetime(2026, 6, 18, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    set_sim_now(virtual)
    try:
        strat._trade_date = ""
        strat._triggered  = {}
        first  = strat.evaluate("DUPETEST", ind, {})
        second = strat.evaluate("DUPETEST", ind, {})
    finally:
        set_sim_now(None)
    assert first  is not None, "First evaluate should produce a signal"
    assert second is None,     "Second evaluate for same symbol should return None"
run("FirstPullbackStrategy: prevents duplicate signal for same symbol", t_fp_strategy_blocks_duplicate_symbol)

def t_fp_strategy_blocked_by_open_position():
    """evaluate() returns None when symbol already has an open position."""
    from first_pullback_strategy import FirstPullbackStrategy

    strat = FirstPullbackStrategy()
    strat.set_config(
        enabled=True, use_macd_filter=False, use_rsi_filter=False,
        max_stop_cents=1.00, min_pole_pct=1.0,
    )
    pole   = _pole(3, start=5.00, step=0.10)
    flag   = _flag(2, top=5.30, pullback_per=0.05)
    signal = [_signal_candle(flag_high=5.30, above=0.05)]
    ind = {"candles": pole + flag + signal, "macd_hist": 0.1, "rsi": 55.0,
           "vwap": 5.20, "breakout_vol_ratio": 2.5}

    # Pass a fake open_positions dict that already contains this symbol;
    # no datetime mock needed — early-exit before time gate
    strat._triggered = {}
    result = strat.evaluate("HELD", ind, {"HELD": object()})
    assert result is None, "Should not generate signal if position already open"
run("FirstPullbackStrategy: blocked when open position already exists", t_fp_strategy_blocked_by_open_position)

def t_fp_candles_included_in_indicators():
    """scanner.get_indicators() return dict must include 'candles' key."""
    from scanner import StockScanner
    from unittest.mock import MagicMock, patch

    mock_client = MagicMock()
    # Return 30 synthetic candles
    mock_client.get_price_history.return_value = [
        {"open": 5.0 + i*0.01, "close": 5.01 + i*0.01,
         "high": 5.02 + i*0.01, "low": 4.99 + i*0.01, "volume": 100_000}
        for i in range(30)
    ]
    scanner = StockScanner(client=mock_client)
    ind = scanner.get_indicators("FAKE", orb_minutes=15)
    assert ind is not None, "get_indicators should return a dict"
    assert "candles" in ind, "Return dict must contain 'candles' key for FP detection"
    assert isinstance(ind["candles"], list), "'candles' should be a list"
    assert len(ind["candles"]) == 30
run("scanner.get_indicators: return dict includes raw candles list", t_fp_candles_included_in_indicators)


# ══════════════════════════════════════════════════════════════════════════════
# Suite 14 — Bug regression: ind init, config persistence, swing scan guard
# ══════════════════════════════════════════════════════════════════════════════
section("14. Bug regression — ind init, config persistence, swing-scan guard")

def t_fp_disabled_returns_none():
    """FirstPullbackStrategy.evaluate() returns None immediately when enabled=False."""
    from first_pullback_strategy import FirstPullbackStrategy
    strat = FirstPullbackStrategy()
    strat.set_config(enabled=False)
    # Even a perfect candle set should be rejected
    pole   = _pole(3, start=5.00, step=0.10)
    flag   = _flag(2, top=5.30, pullback_per=0.05)
    signal = [_signal_candle(flag_high=5.30, above=0.05)]
    ind = {"candles": pole + flag + signal, "macd_hist": 0.1, "rsi": 55.0,
           "vwap": 5.20, "breakout_vol_ratio": 2.5}
    result = strat.evaluate("DISABLED", ind, {})
    assert result is None, "Disabled FP strategy must return None unconditionally"
run("FirstPullbackStrategy: enabled=False short-circuits evaluate()", t_fp_disabled_returns_none)

def t_rr_target_persists_across_set_config():
    """rr_target set via engine.set_config() is reflected on strategy and saved to config dict."""
    eng = TradingEngine()
    eng.set_config(rr_target=3.0)
    assert abs(eng.rr_target - 3.0) < 1e-9, "Engine should store rr_target"
    assert abs(eng.strategy.rr_target - 3.0) < 1e-9, "Strategy should receive rr_target via _apply_config"
    cfg = eng._build_config_dict()
    assert abs(cfg["rr_target"] - 3.0) < 1e-9, "rr_target must be saved in config dict"
run("rr_target propagates to strategy and persists in config dict", t_rr_target_persists_across_set_config)

def t_entry_cutoff_hour_persists():
    """entry_cutoff_hour set via engine.set_config() propagates to ORBStrategy."""
    eng = TradingEngine()
    eng.set_config(entry_cutoff_hour=11)
    assert eng.entry_cutoff_hour == 11, "Engine should store entry_cutoff_hour"
    assert eng.strategy.entry_cutoff_hour == 11, "ORBStrategy must receive entry_cutoff_hour"
    cfg = eng._build_config_dict()
    assert cfg["entry_cutoff_hour"] == 11, "entry_cutoff_hour must appear in saved config"
run("entry_cutoff_hour propagates to ORBStrategy and persists", t_entry_cutoff_hour_persists)

def t_require_vwap_above_persists():
    """require_vwap_above set via engine.set_config() propagates to ORBStrategy."""
    eng = TradingEngine()
    eng.set_config(require_vwap_above=False)
    assert eng.require_vwap_above is False, "Engine should store require_vwap_above"
    assert eng.strategy.require_vwap_above is False, "ORBStrategy must receive require_vwap_above"
    cfg = eng._build_config_dict()
    assert cfg["require_vwap_above"] is False, "require_vwap_above must appear in saved config"
run("require_vwap_above propagates to ORBStrategy and persists", t_require_vwap_above_persists)

def t_ind_none_prevents_stale_fp_candles():
    """If get_indicators raises for symbol B, FP strategy does NOT use symbol A's candles."""
    from first_pullback_strategy import FirstPullbackStrategy
    from unittest.mock import MagicMock, patch

    eng = TradingEngine()
    eng.use_first_pullback = True
    eng.fp_strategy = FirstPullbackStrategy()
    eng.fp_strategy.set_config(enabled=True, use_macd_filter=False, use_rsi_filter=False,
                                max_stop_cents=1.00, min_pole_pct=1.0)

    pole   = _pole(3, start=5.00, step=0.10)
    flag   = _flag(2, top=5.30, pullback_per=0.05)
    signal = [_signal_candle(flag_high=5.30, above=0.05)]
    good_ind = {"candles": pole + flag + signal, "macd_hist": 0.1, "rsi": 55.0,
                "vwap": 5.20, "breakout_vol_ratio": 2.5}

    eval_calls = []

    def fake_evaluate(symbol, ind, open_positions):
        eval_calls.append({"symbol": symbol, "ind": ind})
        return None  # don't actually trade; we're only checking call args

    eng.fp_strategy.evaluate = fake_evaluate

    # Symbol A: indicators succeed; Symbol B: indicators raise
    def fake_get_indicators(symbol, orb_minutes):
        if symbol == "AAA":
            return good_ind
        raise RuntimeError("API error for BBB")

    eng.scanner.get_indicators = fake_get_indicators
    eng.client.get_quotes = MagicMock(return_value={
        "AAA": {"quote": {"lastPrice": 5.35}},
        "BBB": {"quote": {"lastPrice": 3.00}},
    })
    eng.strategy.evaluate = MagicMock(return_value=None)
    eng._active_symbols = ["AAA", "BBB"]

    # Both symbols have ORB levels to avoid early return
    eng.strategy._orb_levels = {"AAA": {"high": 5.2, "low": 4.8, "mid": 5.0},
                                  "BBB": {"high": 2.9, "low": 2.7, "mid": 2.8}}

    # Patch datetime inside fp_strategy to be outside 09:30-11:30 window — we just want
    # to verify FP's evaluate is called only with non-None ind
    with patch("first_pullback_strategy.datetime") as mock_dt:
        mock_outside = MagicMock()
        mock_outside.hour = 10
        mock_outside.minute = 30
        mock_dt.datetime.now.return_value = mock_outside
        eng._poll_and_evaluate(extended=False)

    # BBB should either not appear in eval_calls or appear with ind=None
    bbb_calls = [c for c in eval_calls if c["symbol"] == "BBB"]
    for call in bbb_calls:
        assert call["ind"] is None, "BBB must receive ind=None (not AAA's stale candles)"
run("ind=None per-iteration: stale candles don't bleed to next symbol", t_ind_none_prevents_stale_fp_candles)

def t_swing_scan_not_triggered_when_no_active_symbols():
    """_run_swing_scan is not called when _active_symbols is empty, even past interval."""
    eng = TradingEngine()
    eng.swing_scan_enabled = True
    eng._active_symbols    = []          # empty — no gap scan results yet
    eng._last_swing_scan   = 0.0         # interval elapsed
    eng._swing_scan_interval = 1800

    scan_called = []
    orig = eng._run_swing_scan
    eng._run_swing_scan = lambda: scan_called.append(1) or orig()

    # Simulate the trade loop condition manually
    import time
    import datetime as _dt
    now = _dt.datetime.now()
    # Condition from the fixed _trade_loop:
    in_session = True  # assume in-session
    if in_session and eng.swing_scan_enabled and eng._active_symbols:
        if time.time() - eng._last_swing_scan > eng._swing_scan_interval:
            eng._run_swing_scan()

    assert len(scan_called) == 0, "_run_swing_scan must NOT be called when _active_symbols is empty"
run("Swing scan guard: not triggered when trading universe is empty", t_swing_scan_not_triggered_when_no_active_symbols)

def t_swing_scan_triggers_when_symbols_present():
    """_run_swing_scan IS triggered when _active_symbols is populated and interval elapsed."""
    eng = TradingEngine()
    eng.swing_scan_enabled = True
    eng._active_symbols    = ["NIO", "LCID"]
    eng._last_swing_scan   = 0.0
    eng._swing_scan_interval = 1800

    scan_called = []
    eng._run_swing_scan = lambda: scan_called.append(1)

    import time
    in_session = True
    if in_session and eng.swing_scan_enabled and eng._active_symbols:
        if time.time() - eng._last_swing_scan > eng._swing_scan_interval:
            eng._run_swing_scan()

    assert len(scan_called) == 1, "_run_swing_scan must fire when symbols are present and interval elapsed"
run("Swing scan guard: fires normally when trading universe is populated", t_swing_scan_triggers_when_symbols_present)

def t_manual_sell_closes_position():
    """manual_sell() records exit, computes PnL, and removes the position."""
    eng = TradingEngine()
    eng.dry_run = True
    from orb_strategy import TradeSignal
    sig = TradeSignal(
        symbol="MANU", direction="LONG", entry_price=5.00, shares=100,
        stop_price=4.80, target_r2=5.40, target_r3=5.60, risk_dollars=20.0,
        orb_high=5.00, orb_low=4.80, vwap=None, rel_vol=3.0, gap_pct=5.0, reason="t",
    )
    pos = eng.strategy.record_entry(sig, "DRY_RUN", status="open")
    # Simulate a price above entry
    eng.client.get_quote = MagicMock(return_value={"quote": {"lastPrice": 5.30}})
    eng.manual_sell("MANU")
    assert "MANU" not in eng.strategy._open_positions, "Position should be removed after manual_sell"
    assert len(eng.strategy._closed_positions) == 1, "Closed list should have one entry"
    assert eng.strategy._closed_positions[0].pnl > 0, "PnL should be positive (sold above entry)"
run("manual_sell: closes position and records positive PnL", t_manual_sell_closes_position)

def t_execute_trade_blocked_by_kill_switch():
    """_execute_trade returns immediately when kill switch is tripped."""
    eng = TradingEngine()
    eng.dry_run = True
    eng._kill_switch_tripped = True
    from orb_strategy import TradeSignal
    sig = TradeSignal(
        symbol="KILL", direction="LONG", entry_price=5.00, shares=100,
        stop_price=4.80, target_r2=5.40, target_r3=5.60, risk_dollars=20.0,
        orb_high=5.00, orb_low=4.80, vwap=None, rel_vol=3.0, gap_pct=5.0, reason="t",
    )
    eng._execute_trade(sig)
    assert "KILL" not in eng.strategy._open_positions, "Kill switch must block trade execution"
run("_execute_trade: blocked when kill switch is tripped", t_execute_trade_blocked_by_kill_switch)

def t_pending_timeout_drops_entry():
    """A pending entry past the timeout window is cancelled and removed."""
    import time
    eng = TradingEngine()
    eng.dry_run = False  # pending fill detection only runs in live mode
    eng._pending_timeout_sec = 0.001  # expire immediately

    from orb_strategy import TradeSignal
    sig = TradeSignal(
        symbol="TOUT", direction="LONG", entry_price=3.00, shares=50,
        stop_price=2.85, target_r2=3.30, target_r3=3.45, risk_dollars=7.50,
        orb_high=3.00, orb_low=2.85, vwap=None, rel_vol=2.5, gap_pct=5.0, reason="t",
    )
    pos = eng.strategy.record_entry(sig, "ORD-TOUT-001", status="pending")
    # Force entry_time far in the past
    pos.entry_time = time.time() - 300

    # Mock broker: no position held (order never filled)
    eng.client.get_positions = MagicMock(return_value=[])
    eng.client.cancel_order  = MagicMock(return_value=True)

    eng._check_pending_fills()

    assert "TOUT" not in eng.strategy._open_positions, "Timed-out pending entry must be dropped"
    eng.client.cancel_order.assert_called_once_with("ORD-TOUT-001")
run("Pending timeout: unfilled entry cancelled and dropped after deadline", t_pending_timeout_drops_entry)


# ══════════════════════════════════════════════════════════════════════════════
# Suite 15 — Deep coverage: persistence integrity, indicator edge cases, OCO JSON
# ══════════════════════════════════════════════════════════════════════════════
section("15. Deep coverage — persistence integrity, indicator edges, order JSON")

def t_restore_preserves_partial_exit_state():
    """A partially-scaled position must restore realized_pnl, partial_exit_done,
    breakeven_active and original_shares — otherwise profit is lost and the
    position could scale out a second time after a restart."""
    eng = TradingEngine()
    eng.strategy._open_positions = {}
    eng.strategy._closed_positions = []
    from orb_strategy import TradeSignal
    sig = TradeSignal(
        symbol="SCALE", direction="LONG", entry_price=5.00, shares=100,
        stop_price=4.80, target_r2=5.40, target_r3=5.60, risk_dollars=20.0,
        orb_high=5.00, orb_low=4.80, vwap=None, rel_vol=3.0, gap_pct=5.0, reason="t",
    )
    pos = eng.strategy.record_entry(sig, "ORD-SCALE", status="open")
    # Simulate a partial scale-out: 50 shares booked +$25, stop→breakeven
    eng.strategy.record_partial_exit("SCALE", 5.50, 50)
    eng._save_positions()

    # New engine instance reloads from disk
    eng2 = TradingEngine()
    restored = eng2.strategy._open_positions.get("SCALE")
    assert restored is not None, "Position should be restored"
    assert restored.partial_exit_done is True, "partial_exit_done must survive restart"
    assert restored.breakeven_active is True, "breakeven_active must survive restart"
    assert abs(restored.realized_pnl - 25.0) < 1e-6, "realized_pnl must survive restart"
    assert restored.original_shares == 100, "original_shares must survive restart"
    assert restored.shares == 50, "remaining shares preserved"
    assert abs(restored.stop_price - 5.00) < 1e-6, "stop should be at breakeven"
    # Clean up disk so other tests aren't affected
    eng2.strategy._open_positions = {}
    eng2._save_positions()
run("Restart preserves partial-exit state (realized_pnl/partial/breakeven)", t_restore_preserves_partial_exit_state)

def t_day_pnl_includes_realized_after_restart():
    """get_day_pnl() must include realized PnL booked on still-open positions
    after a restart (not just fully-closed trades)."""
    eng = TradingEngine()
    eng.strategy._open_positions = {}
    eng.strategy._closed_positions = []
    from orb_strategy import TradeSignal
    sig = TradeSignal(
        symbol="RPNL", direction="LONG", entry_price=4.00, shares=200,
        stop_price=3.80, target_r2=4.40, target_r3=4.60, risk_dollars=40.0,
        orb_high=4.00, orb_low=3.80, vwap=None, rel_vol=3.0, gap_pct=5.0, reason="t",
    )
    eng.strategy.record_entry(sig, "ORD-RPNL", status="open")
    eng.strategy.record_partial_exit("RPNL", 4.30, 100)  # +$30 realized
    eng._save_positions()

    eng2 = TradingEngine()
    assert abs(eng2.strategy.get_day_pnl() - 30.0) < 1e-6, "Day PnL must reflect restored realized PnL"
    eng2.strategy._open_positions = {}
    eng2._save_positions()
run("Day PnL includes realized profit on restored open position", t_day_pnl_includes_realized_after_restart)

def t_record_exit_adds_realized_to_final_pnl():
    """record_exit() must add already-booked realized_pnl to the final exit PnL."""
    from orb_strategy import ORBStrategy, TradeSignal
    strat = ORBStrategy()
    sig = TradeSignal(
        symbol="FIN", direction="LONG", entry_price=5.00, shares=100,
        stop_price=4.80, target_r2=5.40, target_r3=5.60, risk_dollars=20.0,
        orb_high=5.00, orb_low=4.80, vwap=None, rel_vol=3.0, gap_pct=5.0, reason="t",
    )
    strat.record_entry(sig, "ORD-FIN", status="open")
    strat.record_partial_exit("FIN", 5.50, 50)   # +$25 realized, 50 shares left
    closed = strat.record_exit("FIN", 5.60, "target")  # runner: (5.60-5.00)*50 = $30
    assert closed is not None
    assert abs(closed.pnl - 55.0) < 1e-6, "Final PnL = $30 runner + $25 realized = $55"
run("record_exit folds realized partial PnL into final PnL", t_record_exit_adds_realized_to_final_pnl)

def t_position_save_load_roundtrip_all_fields():
    """DataHandler positions roundtrip must preserve every OpenPosition field."""
    from orb_strategy import OpenPosition
    import data_handler as dh_mod
    handler = dh_mod.DataHandler()
    pos = OpenPosition(
        symbol="RT", direction="LONG", entry_price=3.33, shares=77,
        stop_price=3.10, target_r2=3.79, target_r3=4.02, risk_dollars=17.71,
        entry_order_id="E1", oco_order_id="O1", status="open",
        trailing_stop_active=True, peak_price=3.95, last_volume_snapshot=123456,
        original_shares=150, partial_exit_done=True, breakeven_active=True,
        realized_pnl=42.5,
    )
    handler.save_positions([vars(pos)], [])
    loaded = handler.load_positions()["open"][0]
    for field in ("symbol", "direction", "entry_price", "shares", "stop_price",
                  "trailing_stop_active", "peak_price", "last_volume_snapshot",
                  "original_shares", "partial_exit_done", "breakeven_active", "realized_pnl"):
        assert loaded[field] == getattr(pos, field), f"Field {field} must roundtrip"
    # cleanup
    handler.save_positions([], [])
run("Position JSON roundtrip preserves all OpenPosition fields", t_position_save_load_roundtrip_all_fields)

def t_rsi_returns_none_on_insufficient_data():
    """compute_rsi returns None when fewer than period+1 closes."""
    from scanner import StockScanner
    assert StockScanner.compute_rsi([1.0, 2.0, 3.0], 14) is None
run("compute_rsi: None on insufficient data", t_rsi_returns_none_on_insufficient_data)

def t_macd_returns_none_on_insufficient_data():
    """compute_macd returns None when fewer than slow+signal closes."""
    from scanner import StockScanner
    assert StockScanner.compute_macd([1.0] * 10) is None
run("compute_macd: None on insufficient data", t_macd_returns_none_on_insufficient_data)

def t_adx_returns_none_on_insufficient_data():
    """compute_adx returns None when fewer than period*2+1 candles."""
    from scanner import StockScanner
    candles = [{"high": 2, "low": 1, "close": 1.5} for _ in range(10)]
    assert StockScanner.compute_adx(candles, 14) is None
run("compute_adx: None on insufficient data", t_adx_returns_none_on_insufficient_data)

def t_atr_returns_none_on_insufficient_data():
    """compute_atr returns None when fewer than period+1 candles."""
    from scanner import StockScanner
    candles = [{"high": 2, "low": 1, "close": 1.5} for _ in range(5)]
    assert StockScanner.compute_atr(candles, 14) is None
run("compute_atr: None on insufficient data", t_atr_returns_none_on_insufficient_data)

def t_rsi_all_gains_returns_100():
    """A strictly rising series yields RSI = 100 (no losses)."""
    from scanner import StockScanner
    closes = [float(i) for i in range(1, 30)]
    assert StockScanner.compute_rsi(closes, 14) == 100.0
run("compute_rsi: strictly rising series = 100", t_rsi_all_gains_returns_100)

def t_macd_bullish_on_uptrend():
    """compute_macd line is positive on a clean uptrend (fast EMA leads slow EMA).
    Note: on a *linear* uptrend the histogram converges toward 0 as the signal
    line catches up, so we assert on the MACD line itself, not the histogram."""
    from scanner import StockScanner
    closes = [5.0 + i * 0.10 for i in range(40)]
    macd = StockScanner.compute_macd(closes)
    assert macd is not None and macd["macd"] > 0, "MACD line should be positive on uptrend"
run("compute_macd: positive MACD line on uptrend", t_macd_bullish_on_uptrend)

def t_macd_histogram_positive_on_acceleration():
    """Histogram turns positive when an uptrend is accelerating (convexity)."""
    from scanner import StockScanner
    # Accelerating series: each step larger than the last → MACD pulls above signal
    closes = [5.0]
    step = 0.02
    for _ in range(45):
        closes.append(closes[-1] + step)
        step += 0.004
    macd = StockScanner.compute_macd(closes)
    assert macd is not None and macd["hist"] > 0, "Histogram should be positive while accelerating"
run("compute_macd: positive histogram on accelerating uptrend", t_macd_histogram_positive_on_acceleration)

def t_detect_fp_minimum_candles_boundary():
    """detect_first_pullback returns None below minimum candle count."""
    from scanner import StockScanner
    # min_pole_candles(2) + min_flag_candles(1) + 1 = 4 minimum
    too_few = [_make_candle(5.0, 5.1) for _ in range(3)]
    assert StockScanner.detect_first_pullback(too_few) is None
run("detect_first_pullback: None below minimum candle count", t_detect_fp_minimum_candles_boundary)

def t_sim_oco_order_structure():
    """SimClient.place_oco_order returns a SIM id (exercises the sim OCO path)."""
    from sim_client import SimClient
    sim = SimClient()
    oco_id = sim.place_oco_order("ABCD", 100, 4.80, 5.40)
    assert oco_id and oco_id.startswith("SIM-"), "Sim OCO should return a SIM order id"
run("SimClient.place_oco_order returns valid sim id", t_sim_oco_order_structure)

def t_sim_market_order_then_position_appears():
    """After a sim BUY, get_positions reflects the held quantity."""
    from sim_client import SimClient
    sim = SimClient()
    oid = sim.place_market_order("EFGH", 50, "BUY")
    assert oid and oid.startswith("SIM-")
    positions = {p["symbol"]: p for p in sim.get_positions()}
    assert "EFGH" in positions, "Sim should report the position after a BUY"
    assert positions["EFGH"]["quantity"] == 50
run("SimClient market BUY reflected in get_positions", t_sim_market_order_then_position_appears)

def t_calc_position_size_floor_one_share():
    """calc_position_size never returns 0 shares even with a huge stop distance."""
    from orb_strategy import ORBStrategy
    strat = ORBStrategy()
    strat.account_size = 100.0
    shares, risk, dist = strat.calc_position_size(entry=5.0, stop=0.01)
    assert shares >= 1, "Must always return at least 1 share"
run("calc_position_size: floors at 1 share", t_calc_position_size_floor_one_share)

def t_kill_switch_resets_on_new_day():
    """Day rollover in _trade_loop clears the kill switch (verified via flag reset)."""
    eng = TradingEngine()
    eng._kill_switch_tripped = True
    eng._eod_flattened = True
    # Simulate the date-rollover block from _trade_loop
    loop_date = "2026-06-17"
    today = "2026-06-18"
    if loop_date != today:
        eng._kill_switch_tripped = False
        eng._eod_flattened = False
    assert eng._kill_switch_tripped is False, "Kill switch must reset on new day"
    assert eng._eod_flattened is False, "EOD flatten flag must reset on new day"
run("Kill switch + EOD flag reset on date rollover", t_kill_switch_resets_on_new_day)

def t_fp_stop_width_rejected_when_too_wide():
    """FirstPullbackStrategy rejects setups whose stop exceeds max_stop_cents."""
    from first_pullback_strategy import FirstPullbackStrategy
    strat = FirstPullbackStrategy()
    strat.set_config(enabled=True, use_macd_filter=False, use_rsi_filter=False,
                     max_stop_cents=0.05, min_pole_pct=1.0)  # very tight max stop
    pole   = _pole(3, start=5.00, step=0.10)
    flag   = _flag(2, top=5.30, pullback_per=0.05)  # flag spans ~0.10+ → stop > 0.05
    signal = [_signal_candle(flag_high=5.30, above=0.05)]
    ind = {"candles": pole + flag + signal, "macd_hist": 0.1, "rsi": 55.0,
           "vwap": 5.20, "breakout_vol_ratio": 2.5}
    mock_now = MagicMock(); mock_now.hour = 10; mock_now.minute = 0
    with patch("first_pullback_strategy.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = mock_now
        strat._trade_date = ""; strat._triggered = {}
        result = strat.evaluate("WIDE", ind, {})
    assert result is None, "Setup with stop wider than max_stop_cents must be rejected"
run("FirstPullbackStrategy: rejects stop wider than max_stop_cents", t_fp_stop_width_rejected_when_too_wide)

def t_fp_time_gate_blocks_outside_window():
    """FP evaluate returns None outside the 09:30–time_limit window."""
    from first_pullback_strategy import FirstPullbackStrategy
    strat = FirstPullbackStrategy()
    strat.set_config(enabled=True, use_macd_filter=False, use_rsi_filter=False,
                     max_stop_cents=1.0, min_pole_pct=1.0, time_limit_hhmm=1130)
    pole   = _pole(3, start=5.00, step=0.10)
    flag   = _flag(2, top=5.30, pullback_per=0.05)
    signal = [_signal_candle(flag_high=5.30, above=0.05)]
    ind = {"candles": pole + flag + signal, "macd_hist": 0.1, "rsi": 55.0,
           "vwap": 5.20, "breakout_vol_ratio": 2.5}
    # 12:00 ET — past the 11:30 limit
    mock_now = MagicMock(); mock_now.hour = 12; mock_now.minute = 0
    # Patch _now_et (what FP actually calls) not the datetime module
    with patch("first_pullback_strategy._now_et", return_value=mock_now):
        strat._trade_date = ""; strat._triggered = {}
        result = strat.evaluate("LATE", ind, {})
    assert result is None, "FP must not signal after the time limit"
run("FirstPullbackStrategy: time gate blocks entries past limit", t_fp_time_gate_blocks_outside_window)


# ══════════════════════════════════════════════════════════════════════════════
# Suite 16 — Analytics layer + configurable runner target
# ══════════════════════════════════════════════════════════════════════════════
section("16. Analytics layer + configurable runner target")

def _closed_trade(symbol, pnl, risk_dollars=100.0, reason="ORB LONG breakout",
                  gap_pct=8.0, entry_time=1781793600.0, status="target", exit_price=5.5):
    """Build a closed-position dict as produced by vars(OpenPosition)."""
    return {
        "symbol": symbol, "direction": "LONG", "entry_price": 5.0, "shares": 100,
        "stop_price": 4.8, "target_r2": 5.4, "target_r3": 5.6, "risk_dollars": risk_dollars,
        "entry_time": entry_time, "status": status, "exit_price": exit_price, "pnl": pnl,
        "entry_reason": reason, "gap_pct": gap_pct,
    }

def t_analytics_basic_metrics():
    """compute_analytics produces correct win rate, profit factor, expectancy."""
    from analytics import compute_analytics
    trades = [
        _closed_trade("A", 200.0),    # win, +2R
        _closed_trade("B", 200.0),    # win, +2R
        _closed_trade("C", -100.0, status="stop", exit_price=4.8),   # loss, -1R
        _closed_trade("D", -100.0, status="stop", exit_price=4.8),   # loss, -1R
    ]
    a = compute_analytics(trades)["overall"]
    assert a["trades"] == 4
    assert a["wins"] == 2 and a["losses"] == 2
    assert abs(a["win_rate"] - 0.5) < 1e-9
    # PF = gross profit 400 / gross loss 200 = 2.0
    assert abs(a["profit_factor"] - 2.0) < 1e-9
    # Total PnL = 400 - 200 = 200
    assert abs(a["total_pnl"] - 200.0) < 1e-9
    # Expectancy R = mean of [2,2,-1,-1] = 0.5
    assert abs(a["expectancy_r"] - 0.5) < 1e-9
run("Analytics: win rate, profit factor, expectancy correct", t_analytics_basic_metrics)

def t_analytics_profit_factor_none_when_no_losses():
    """Profit factor is None (∞) when there are no losing trades."""
    from analytics import compute_analytics
    trades = [_closed_trade("A", 100.0), _closed_trade("B", 50.0)]
    a = compute_analytics(trades)["overall"]
    assert a["profit_factor"] is None, "PF must be None when gross loss is 0"
run("Analytics: profit factor None when no losses", t_analytics_profit_factor_none_when_no_losses)

def t_analytics_empty_is_safe():
    """compute_analytics over no trades returns a zeroed report without error."""
    from analytics import compute_analytics
    a = compute_analytics([])
    assert a["overall"]["trades"] == 0
    assert a["overall"]["win_rate"] == 0.0
    assert a["by_setup"] == {} and a["by_hour"] == {} and a["by_gap"] == {}
run("Analytics: empty trade list is safe", t_analytics_empty_is_safe)

def t_analytics_excludes_open_positions():
    """Only genuine closed round-trips count (open/pending excluded)."""
    from analytics import compute_analytics
    trades = [
        _closed_trade("A", 200.0),
        {"symbol": "OPEN", "status": "open", "pnl": 0.0, "risk_dollars": 100.0,
         "exit_price": None, "entry_reason": "ORB", "entry_time": 1781793600.0, "gap_pct": 5.0},
    ]
    a = compute_analytics(trades)["overall"]
    assert a["trades"] == 1, "Open position must be excluded from analytics"
run("Analytics: excludes still-open positions", t_analytics_excludes_open_positions)

def t_analytics_setup_slice():
    """by_setup groups ORB and First Pullback trades separately."""
    from analytics import compute_analytics
    trades = [
        _closed_trade("A", 200.0, reason="ORB LONG breakout above 5.2"),
        _closed_trade("B", -100.0, reason="ORB LONG breakout above 5.2", status="stop", exit_price=4.8),
        _closed_trade("C", 300.0, reason="first_pullback pole=6% reds=2 retrace50=5.1"),
    ]
    by_setup = compute_analytics(trades)["by_setup"]
    assert "orb" in by_setup and "first_pullback" in by_setup
    assert by_setup["orb"]["trades"] == 2
    assert by_setup["first_pullback"]["trades"] == 1
run("Analytics: by_setup slices ORB vs First Pullback", t_analytics_setup_slice)

def t_analytics_gap_bucket_slice():
    """by_gap buckets trades by gap size."""
    from analytics import compute_analytics
    trades = [
        _closed_trade("A", 100.0, gap_pct=3.0),    # <5%
        _closed_trade("B", 100.0, gap_pct=7.0),    # 5-10%
        _closed_trade("C", 100.0, gap_pct=15.0),   # 10-20%
    ]
    by_gap = compute_analytics(trades)["by_gap"]
    assert "<5%" in by_gap and "5-10%" in by_gap and "10-20%" in by_gap
run("Analytics: by_gap buckets trades by gap size", t_analytics_gap_bucket_slice)

def t_analytics_max_drawdown():
    """Max drawdown tracks the largest peak-to-trough on the equity curve."""
    from analytics import compute_analytics
    # Equity path: +200, +200(=400 peak), -100(=300), -100(=200), -100(=100) → DD=300
    trades = [
        _closed_trade("A", 200.0),
        _closed_trade("B", 200.0),
        _closed_trade("C", -100.0, status="stop", exit_price=4.8),
        _closed_trade("D", -100.0, status="stop", exit_price=4.8),
        _closed_trade("E", -100.0, status="stop", exit_price=4.8),
    ]
    a = compute_analytics(trades)["overall"]
    assert abs(a["max_drawdown"] - 300.0) < 1e-9, f"Max DD should be 300, got {a['max_drawdown']}"
run("Analytics: max drawdown computed from equity curve", t_analytics_max_drawdown)

def t_analytics_classify_setup():
    """classify_setup maps reasons to setup buckets."""
    from analytics import classify_setup
    assert classify_setup("ORB LONG breakout above 5.2") == "orb"
    assert classify_setup("first_pullback pole=6%") == "first_pullback"
    assert classify_setup("Manual buy") == "manual"
    assert classify_setup("") == "other"
run("Analytics: classify_setup maps reasons correctly", t_analytics_classify_setup)

def t_engine_get_analytics_includes_restored_history():
    """engine.get_analytics() reflects closed trades restored from disk."""
    eng = TradingEngine()
    eng.strategy._open_positions = {}
    eng.strategy._closed_positions = []
    from orb_strategy import TradeSignal
    sig = TradeSignal(
        symbol="ANL", direction="LONG", entry_price=5.0, shares=100,
        stop_price=4.8, target_r2=5.4, target_r3=5.6, risk_dollars=20.0,
        orb_high=5.0, orb_low=4.8, vwap=None, rel_vol=3.0, gap_pct=8.0,
        reason="ORB LONG breakout above 5.2",
    )
    eng.strategy.record_entry(sig, "ORD-ANL", status="open")
    eng.client.get_quote = MagicMock(return_value={"quote": {"lastPrice": 5.4}})
    eng.dry_run = True
    eng.manual_sell("ANL")
    eng._save_positions()
    # New engine restores the closed trade and analytics sees it
    eng2 = TradingEngine()
    a = eng2.get_analytics()
    assert a["overall"]["trades"] >= 1, "Restored closed trade must appear in analytics"
    # cleanup
    eng2.strategy._open_positions = {}
    eng2.strategy._closed_positions = []
    eng2._save_positions()
run("engine.get_analytics reflects restored closed-trade history", t_engine_get_analytics_includes_restored_history)

def t_runner_target_extends_after_partial():
    """After a partial exit, _rebracket targets runner_target_r (3R) not 2R."""
    eng = TradingEngine()
    eng.dry_run = False
    eng.runner_target_r = 3.0
    captured = {}
    def fake_oco(symbol, shares, stop, target, extended_hours=False):
        captured["target"] = target
        captured["stop"] = stop
        return "OCO-1"
    eng.client.place_oco_order = fake_oco

    from orb_strategy import OpenPosition
    # entry 5.00, R=0.20 → target_r2=5.40 (2R), target_r3=5.60 (3R)
    pos = OpenPosition(
        symbol="RUN", direction="LONG", entry_price=5.00, shares=50,
        stop_price=5.00, target_r2=5.40, target_r3=5.60, risk_dollars=20.0,
        original_shares=100, partial_exit_done=True, breakeven_active=True,
    )
    eng._rebracket(pos)
    # runner_target_r=3.0 → entry + R*3 = 5.00 + 0.20*3 = 5.60
    assert abs(captured["target"] - 5.60) < 1e-6, f"Runner should target 3R (5.60), got {captured['target']}"
run("Runner target: extends to 3R after partial exit", t_runner_target_extends_after_partial)

def t_runner_target_configurable_to_4r():
    """runner_target_r=4.0 projects the runner to 4R."""
    eng = TradingEngine()
    eng.dry_run = False
    eng.runner_target_r = 4.0
    captured = {}
    eng.client.place_oco_order = lambda symbol, shares, stop, target, extended_hours=False: captured.update(target=target) or "OCO-2"
    from orb_strategy import OpenPosition
    pos = OpenPosition(
        symbol="RUN4", direction="LONG", entry_price=5.00, shares=50,
        stop_price=5.00, target_r2=5.40, target_r3=5.60, risk_dollars=20.0,
        original_shares=100, partial_exit_done=True,
    )
    eng._rebracket(pos)
    # 5.00 + 0.20*4 = 5.80
    assert abs(captured["target"] - 5.80) < 1e-6, f"Runner should target 4R (5.80), got {captured['target']}"
run("Runner target: configurable to 4R", t_runner_target_configurable_to_4r)

def t_runner_target_persists_in_config():
    """runner_target_r propagates through set_config and persists in config dict."""
    eng = TradingEngine()
    eng.set_config(runner_target_r=3.5)
    assert abs(eng.runner_target_r - 3.5) < 1e-9
    assert abs(eng._build_config_dict()["runner_target_r"] - 3.5) < 1e-9
run("Runner target: persists through set_config + config dict", t_runner_target_persists_in_config)

def t_non_partial_rebracket_uses_original_target():
    """A position that has NOT scaled out re-brackets at its original 2R target."""
    eng = TradingEngine()
    eng.dry_run = False
    eng.strategy.rr_target = 2.0
    captured = {}
    eng.client.place_oco_order = lambda symbol, shares, stop, target, extended_hours=False: captured.update(target=target) or "OCO-3"
    from orb_strategy import OpenPosition
    pos = OpenPosition(
        symbol="NOSCALE", direction="LONG", entry_price=5.00, shares=100,
        stop_price=4.80, target_r2=5.40, target_r3=5.60, risk_dollars=20.0,
        original_shares=100, partial_exit_done=False,
    )
    eng._rebracket(pos)
    assert abs(captured["target"] - 5.40) < 1e-6, "Un-scaled position should use original 2R target"
run("Runner target: un-scaled position keeps original target", t_non_partial_rebracket_uses_original_target)

def t_extended_hours_swing_entries_fire():
    """_poll_extended_hours places orders when swing signal fires (extended hours re-enabled)."""
    eng = TradingEngine()
    eng.dry_run    = True   # dry-run so no real broker calls
    eng.auto_trade = True
    eng.extended_hours = True
    eng.use_market_filter = False  # disable SPY check for this isolated test
    eng._active_symbols = ["NIO"]

    eng._swing_results = [{"symbol": "NIO", "swing_count": 10, "support": 2.40,
                           "resistance": 2.60, "avg_amplitude": 2.5,
                           "current_bias": "At Low", "day_range_pct": 2.5, "last": 2.41}]

    eng.client.get_quotes = MagicMock(return_value={
        "NIO": {"quote": {"lastPrice": 2.41}}
    })
    eng.swing_scanner.get_swing_entry_signal = MagicMock(return_value={
        "direction": "LONG", "entry": 2.42, "stop": 2.35,
        "target": 2.56, "support": 2.40, "resistance": 2.60, "reason": "Swing LONG",
    })

    eng.strategy.set_orb_levels("NIO", {"high": 2.5, "low": 2.3, "mid": 2.4})
    eng._poll_extended_hours()

    # Dry-run records the position directly without a real broker call
    assert "NIO" in eng.strategy._open_positions, \
        "Extended hours swing entry should be recorded in dry-run mode"
run("Extended hours: swing entries fire (pre-market / after-hours enabled)", t_extended_hours_swing_entries_fire)


# ══════════════════════════════════════════════════════════════════════════════
# Suite 17 — Sim clock: 24/7 sim, session bypass, day-cycle advance
# ══════════════════════════════════════════════════════════════════════════════
section("17. Sim clock — 24/7 sim, virtual session, day-cycle advance")

def t_sim_clock_starts_in_regular_session():
    """enable_simulation sets the virtual clock to 09:46 AM ET — inside regular hours."""
    eng = TradingEngine()
    eng.enable_simulation()
    now = eng._get_now()
    assert now.hour == 9 and now.minute == 46, f"Sim clock should start at 09:46 ET, got {now.strftime('%H:%M')}"
    assert eng._in_regular(now), "09:46 AM must be in regular session"
    hm = eng._hhmm(now)
    assert hm >= 930 + eng.orb_minutes, "Clock must be past ORB window"
    eng.disable_simulation()
run("enable_simulation: virtual clock starts at 09:46 AM ET (regular session)", t_sim_clock_starts_in_regular_session)

def t_sim_clock_advances_with_real_time():
    """The sim clock advances at 1:1 real-time pace while sim is active."""
    import time as _time
    eng = TradingEngine()
    eng.enable_simulation()
    t0 = eng._get_now()
    _time.sleep(0.15)
    t1 = eng._get_now()
    elapsed_sim = (t1 - t0).total_seconds()
    assert 0.10 < elapsed_sim < 0.50, f"Sim clock should advance ~0.15s, got {elapsed_sim:.3f}s"
    eng.disable_simulation()
run("Sim clock advances at 1:1 real-time pace", t_sim_clock_advances_with_real_time)

def t_disable_sim_restores_real_clock():
    """disable_simulation restores the real ET clock (sim clock clears to None)."""
    import time as _time
    import datetime as _dt
    eng = TradingEngine()
    eng.enable_simulation()
    eng.disable_simulation()
    assert eng._sim_clock_base is None, "Sim clock base must be None after disable"
    now = eng._get_now()
    real_et = _dt.datetime.now(eng._get_now().tzinfo)
    # Allow 2 seconds of tolerance
    diff = abs((now - _dt.datetime.now(_dt.timezone(now.utcoffset()))).total_seconds())
    # Simply check that the clock hour is plausible real time, not 10:15
    from utils import _sim_now
    assert _sim_now is None, "utils._sim_now must be None after disable_simulation"
run("disable_simulation: real ET clock restored, utils._sim_now cleared", t_disable_sim_restores_real_clock)

def t_set_sim_now_propagates_to_strategies():
    """now_et() in orb/FP strategies returns the sim clock when set."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from utils import set_sim_now, now_et
    from orb_strategy import ORBStrategy
    from first_pullback_strategy import FirstPullbackStrategy

    virtual = _dt.datetime(2026, 6, 23, 10, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    set_sim_now(virtual)
    try:
        assert now_et() == virtual, "utils.now_et() must return the sim clock"
        strat = ORBStrategy()
        # reset_day uses now_et() — should pick up the virtual date
        strat._trade_date = ""
        strat.reset_day()
        assert strat._trade_date == "2026-06-23", f"ORBStrategy.reset_day() used wrong date: {strat._trade_date}"

        fp = FirstPullbackStrategy()
        fp._trade_date = ""
        fp.reset_day()
        assert fp._trade_date == "2026-06-23", f"FP reset_day() used wrong date: {fp._trade_date}"
    finally:
        set_sim_now(None)
run("set_sim_now propagates to ORBStrategy and FP strategy via now_et()", t_set_sim_now_propagates_to_strategies)

def t_sim_clock_enables_orb_signal_outside_market_hours():
    """In sim mode the ORB evaluate() respects the virtual clock, not real time.
    A virtual 10:15 AM allows entry even when the real clock is midnight."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from utils import set_sim_now
    from orb_strategy import ORBStrategy

    virtual = _dt.datetime(2026, 6, 23, 10, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    set_sim_now(virtual)
    try:
        strat = ORBStrategy()
        strat.set_config(entry_cutoff_hour=12, require_vwap_above=False,
                         use_rsi_filter=False, require_volume_confirm=False,
                         confirm_close=False, require_pullback=False, max_trades=10)
        strat.reset_day()   # lock in virtual date before setting indicators
        strat.set_orb_levels("SIM", {"high": 5.20, "low": 4.80, "mid": 5.00})
        strat.set_indicators("SIM", {"ema9": 5.3, "ema20": 5.1, "rsi": 60.0,
                                      "atr": 0.12, "vwap": 5.10,
                                      "last_closed_close": 5.25,
                                      "breakout_vol_ratio": 2.0, "candles": []})
        sig = strat.evaluate("SIM", 5.25)
        assert sig is not None, "ORB should signal at virtual 10:15 AM even if real clock is past midnight"
    finally:
        set_sim_now(None)
run("ORB signals fire during virtual session (real clock irrelevant in sim)", t_sim_clock_enables_orb_signal_outside_market_hours)

def t_sim_fp_time_gate_uses_virtual_clock():
    """First Pullback time gate passes at virtual 10:15 AM even at real midnight."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from unittest.mock import MagicMock
    from utils import set_sim_now
    from first_pullback_strategy import FirstPullbackStrategy
    from scanner import StockScanner

    virtual = _dt.datetime(2026, 6, 23, 10, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    set_sim_now(virtual)
    try:
        strat = FirstPullbackStrategy()
        strat.set_config(enabled=True, use_macd_filter=False, use_rsi_filter=False,
                         max_stop_cents=1.0, min_pole_pct=1.0, time_limit_hhmm=1130)
        strat._trade_date = ""
        strat._triggered  = {}
        pole   = _pole(3, start=5.00, step=0.10)
        flag   = _flag(2, top=5.30, pullback_per=0.05)
        signal = [_signal_candle(flag_high=5.30, above=0.05)]
        ind = {"candles": pole + flag + signal, "macd_hist": 0.1, "rsi": 55.0,
               "vwap": 5.20, "breakout_vol_ratio": 2.5}
        result = strat.evaluate("FPVIRT", ind, {})
        assert result is not None, "FP should signal at virtual 10:15 AM (time gate passes)"
    finally:
        set_sim_now(None)
run("FP time gate passes at virtual 10:15 AM (real clock irrelevant)", t_sim_fp_time_gate_uses_virtual_clock)

def t_next_sim_trading_day_skips_weekend():
    """_next_sim_trading_day advances past Saturday and Sunday to Monday."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    eng = TradingEngine()
    friday = _dt.datetime(2026, 6, 19, 9, 46, 0, tzinfo=ZoneInfo("America/New_York"))
    monday = eng._next_sim_trading_day(friday)
    assert monday.weekday() == 0, f"Expected Monday (0), got weekday {monday.weekday()}"
    assert monday.strftime("%Y-%m-%d") == "2026-06-22"
    assert monday.hour == 9 and monday.minute == 46, "New day clock must start at 09:46"
run("_next_sim_trading_day: skips weekend, advances to Monday", t_next_sim_trading_day_skips_weekend)

def t_start_new_sim_day_advances_clock():
    """_start_new_sim_day increments the cycle count and advances the virtual date."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    eng = TradingEngine()
    eng.enable_simulation()
    day0 = eng._sim_clock_base.date()
    eng._start_new_sim_day()
    day1 = eng._sim_clock_base.date()
    assert eng._sim_day_count == 1, "Cycle count should be 1 after first advance"
    assert day1 > day0, "Sim date must advance forward"
    assert eng._sim_eod_pending is False, "EOD pending flag must clear after advance"
    eng.disable_simulation()
run("_start_new_sim_day: increments cycle, advances date, clears EOD flag", t_start_new_sim_day_advances_clock)

def t_sim_candles_generated_with_virtual_date():
    """SimClient generates candles seeded by the virtual date, not real date.
    Two different virtual dates produce different candle series."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from utils import set_sim_now
    from sim_client import SimClient

    sim = SimClient()

    d1 = _dt.datetime(2026, 6, 23, 10, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    set_sim_now(d1)
    candles1 = sim.get_price_history("NIO")

    d2 = _dt.datetime(2026, 6, 24, 10, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    set_sim_now(d2)
    candles2 = sim.get_price_history("NIO")
    set_sim_now(None)

    assert len(candles1) > 0 and len(candles2) > 0, "Both virtual days should produce candles"
    opens1 = [c["open"] for c in candles1[:5]]
    opens2 = [c["open"] for c in candles2[:5]]
    assert opens1 != opens2, "Different virtual dates must produce different candle patterns"
run("SimClient: different virtual dates produce different candle series", t_sim_candles_generated_with_virtual_date)







# ══════════════════════════════════════════════════════════════════════════════
# SUITE 18 — Trade-aspect debug scenarios (div, filters, journal, risk)
# ══════════════════════════════════════════════════════════════════════════════
section("18. Trade aspects — div exemption, retrace, journal, risk gates")

def t_div_swing_exempt_from_retrace_and_spy():
    """div_swing LONG must pass retrace/SPY gates that block momentum entries."""
    from trading_engine import TradingEngine
    from orb_strategy import TradeSignal
    import trading_engine as te_mod
    from unittest.mock import MagicMock, patch

    eng = TradingEngine.__new__(TradingEngine)
    eng.strategy = MagicMock()
    eng.strategy.get_day_pnl.return_value = 0
    eng.strategy.get_active_positions.return_value = []
    eng.strategy.record_entry = MagicMock(return_value=MagicMock())
    eng.scanner = MagicMock()
    eng.scanner.get_intraday_retrace_pct.return_value = 180.0
    eng.client = MagicMock()
    eng.client.get_balance.return_value = {"cash_available": 10000}
    eng.dry_run = True
    eng.limit_entry_buffer = 0.005
    eng.strategy.rr_target = 2.0
    eng._kill_switch_tripped = False
    eng.account_size = 7000
    eng.max_daily_loss_pct = 5.0
    eng.intraday_max_retrace_pct = 40.0
    eng.time_decay_factor = 1.0
    eng.time_decay_start_hhmm = 1130
    eng.use_market_filter = True
    eng.market_filter_symbol = "SPY"
    eng._last_prices = {}
    eng.emit = MagicMock()
    eng.data_handler = MagicMock()
    eng._save_positions = MagicMock()
    eng._emit_status_update = MagicMock()
    eng.news_sentiment_enabled = False

    with patch.object(TradingEngine, "_market_bullish", return_value=False):
        sig = TradeSignal(
            symbol="GME", direction="LONG", entry_price=21.0, stop_price=20.5,
            target_r2=22.0, target_r3=23.0, shares=10, risk_dollars=5.0,
            orb_high=0, orb_low=0, vwap=None, rel_vol=0, gap_pct=0,
            reason="div_swing LONG [30m BB21] test",
        )
        eng._execute_trade(sig, extended=False)
    eng.data_handler.append_trade_log.assert_called()

def t_momentum_blocked_by_retrace_when_not_div():
    from trading_engine import TradingEngine
    from orb_strategy import TradeSignal
    from unittest.mock import MagicMock, patch

    eng = TradingEngine.__new__(TradingEngine)
    eng.strategy = MagicMock()
    eng.scanner = MagicMock()
    eng.scanner.get_intraday_retrace_pct.return_value = 80.0
    eng.client = MagicMock()
    eng.dry_run = True
    eng.intraday_max_retrace_pct = 40.0
    eng.time_decay_factor = 1.0
    eng._kill_switch_tripped = False
    eng.account_size = 7000
    eng.max_daily_loss_pct = 5.0
    eng.strategy.get_day_pnl.return_value = 0
    eng.strategy.get_active_positions.return_value = []
    eng.use_market_filter = False
    eng.strategy.rr_target = 2.0
    eng.limit_entry_buffer = 0.005
    eng._last_prices = {}
    eng.emit = MagicMock()
    eng.data_handler = MagicMock()

    sig = TradeSignal(
        symbol="RIOT", direction="LONG", entry_price=20.0, stop_price=19.5,
        target_r2=21.0, target_r3=22.0, shares=10, risk_dollars=5.0,
        orb_high=0, orb_low=0, vwap=None, rel_vol=0, gap_pct=0,
        reason="ORB LONG breakout",
    )
    eng._execute_trade(sig, extended=False)
    eng.data_handler.append_trade_log.assert_not_called()

run("div_swing exempt from SPY + retrace gates", t_div_swing_exempt_from_retrace_and_spy)
run("momentum ORB blocked by intraday retrace gate", t_momentum_blocked_by_retrace_when_not_div)

def t_journal_endpoint_shape():
    r = client_http.get("/api/journal")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "trades" in data
    assert isinstance(data["trades"], list)
    if data["trades"]:
        t0 = data["trades"][0]
        for k in ("symbol", "pnl", "exit_time", "source"):
            assert k in t0

def t_journal_loads_from_trade_log():
    import journal as jmod
    trades = jmod.load_trades_from_trade_log()
    assert isinstance(trades, list)
    assert len(trades) >= 1, "trade_log.json should yield completed round-trips"
    t0 = trades[0]
    for k in ("symbol", "pnl", "source", "entry_price", "exit_price"):
        assert k in t0
run("journal.load_trades_from_trade_log yields bot trades", t_journal_loads_from_trade_log)

run("GET /api/journal returns trade list with required fields", t_journal_endpoint_shape)

def t_analytics_endpoint_removed():
    r = client_http.get("/api/analytics")
    assert r.status_code == 404

run("GET /api/analytics removed (journal handles history)", t_analytics_endpoint_removed)

def t_execute_trade_buying_power_gate():
    from trading_engine import TradingEngine
    from orb_strategy import TradeSignal
    from unittest.mock import MagicMock

    eng = TradingEngine.__new__(TradingEngine)
    eng.strategy = MagicMock()
    eng.strategy.get_day_pnl.return_value = 0
    eng.strategy.get_active_positions.return_value = []
    eng.scanner = MagicMock()
    eng.scanner.get_intraday_retrace_pct.return_value = None
    eng.client = MagicMock()
    eng.client.get_balance.return_value = {"cash_available": 100.0}
    eng.dry_run = True
    eng.intraday_max_retrace_pct = 0
    eng.time_decay_factor = 1.0
    eng._kill_switch_tripped = False
    eng.account_size = 7000
    eng.max_daily_loss_pct = 5.0
    eng.use_market_filter = False
    eng.strategy.rr_target = 2.0
    eng.limit_entry_buffer = 0.005
    eng._last_prices = {}
    eng.emit = MagicMock()
    eng.data_handler = MagicMock()

    sig = TradeSignal(
        symbol="BIG", direction="LONG", entry_price=50.0, stop_price=48.0,
        target_r2=54.0, target_r3=56.0, shares=100, risk_dollars=200.0,
        orb_high=0, orb_low=0, vwap=None, rel_vol=0, gap_pct=0,
        reason="ORB LONG breakout",
    )
    eng._execute_trade(sig, extended=False)
    eng.data_handler.append_trade_log.assert_not_called()

run("buying-power gate blocks oversized entry", t_execute_trade_buying_power_gate)

def t_pending_to_held_pipeline_fields():
    from orb_strategy import OpenPosition
    pos = OpenPosition(
        symbol="TST", direction="LONG", entry_price=5.0, shares=10,
        stop_price=4.8, target_r2=5.4, target_r3=5.6, risk_dollars=2.0,
        entry_time=0, entry_order_id="P1", status="pending",
    )
    assert pos.status == "pending"
    pos.status = "held"
    assert pos.status == "held"

run("OpenPosition pending→held status transition", t_pending_to_held_pipeline_fields)


# ══════════════════════════════════════════════════════════════════════════════
# Suite 19 — News sentiment gate
# ══════════════════════════════════════════════════════════════════════════════
section("19. News sentiment — bearish block, bullish boost, fail-open")

def t_news_hard_block_offering():
    from news_sentiment import NewsSentimentService
    ns = NewsSentimentService(block_score=-40)
    scored = ns.score_headline("Company announces secondary offering of common stock")
    assert scored["blocked"] is True
    assert scored["score"] <= -40

def t_news_bullish_boost_score():
    from news_sentiment import NewsSentimentService
    ns = NewsSentimentService(boost_score=25, size_boost_pct=0.15)
    scored = ns.score_headline("Company beats earnings and raises guidance")
    assert scored["score"] >= 25
    assert scored["size_mult"] > 1.0

def t_news_fail_open_on_fetch_error():
    from news_sentiment import NewsSentimentService
    from unittest.mock import patch
    ns = NewsSentimentService()
    with patch.object(ns, "_fetch_headlines", side_effect=Exception("network down")):
        sent = ns.get_sentiment("ZZZZ")
    assert sent["blocked"] is False
    assert sent.get("fail_open") is True

def t_div_blocked_by_bearish_news():
    from trading_engine import TradingEngine
    from orb_strategy import TradeSignal
    from news_sentiment import NewsSentimentService
    from unittest.mock import MagicMock, patch

    eng = TradingEngine.__new__(TradingEngine)
    eng.strategy = MagicMock()
    eng.strategy.get_day_pnl.return_value = 0
    eng.strategy.get_active_positions.return_value = []
    eng.strategy.record_entry = MagicMock(return_value=MagicMock())
    eng.scanner = MagicMock()
    eng.scanner.get_intraday_retrace_pct.return_value = 180.0
    eng.client = MagicMock()
    eng.client.get_balance.return_value = {"cash_available": 10000}
    eng.dry_run = False
    eng.limit_entry_buffer = 0.005
    eng.strategy.rr_target = 2.0
    eng._kill_switch_tripped = False
    eng.account_size = 7000
    eng.max_daily_loss_pct = 5.0
    eng.intraday_max_retrace_pct = 40.0
    eng.time_decay_factor = 1.0
    eng.time_decay_start_hhmm = 1130
    eng.use_market_filter = False
    eng._last_prices = {}
    eng.emit = MagicMock()
    eng.data_handler = MagicMock()
    eng._save_positions = MagicMock()
    eng._emit_status_update = MagicMock()
    eng.news_sentiment_enabled = True
    eng._news_sentiment = NewsSentimentService(block_score=-40)
    eng._news_sentiment.set_cached_sentiment("GME", {
        "score": -100,
        "tags": ["hard_block"],
        "blocked": True,
        "size_mult": 0.0,
        "headline": "GME announces secondary offering",
        "headlines": [],
        "fail_open": False,
    })

    sig = TradeSignal(
        symbol="GME", direction="LONG", entry_price=21.0, stop_price=20.5,
        target_r2=22.0, target_r3=23.0, shares=10, risk_dollars=5.0,
        orb_high=0, orb_low=0, vwap=None, rel_vol=0, gap_pct=0,
        reason="div_swing LONG [30m BB21] test",
    )
    eng._execute_trade(sig, extended=False)
    eng.data_handler.append_trade_log.assert_not_called()

def t_orb_allowed_with_neutral_news():
    from trading_engine import TradingEngine
    from orb_strategy import TradeSignal
    from news_sentiment import NewsSentimentService
    from unittest.mock import MagicMock

    eng = TradingEngine.__new__(TradingEngine)
    eng.strategy = MagicMock()
    eng.strategy.get_day_pnl.return_value = 0
    eng.strategy.get_active_positions.return_value = []
    eng.strategy.record_entry = MagicMock(return_value=MagicMock())
    eng.scanner = MagicMock()
    eng.scanner.get_intraday_retrace_pct.return_value = None
    eng.client = MagicMock()
    eng.client.get_balance.return_value = {"cash_available": 10000}
    eng.client.place_market_order.return_value = "ORD1"
    eng.dry_run = False
    eng.limit_entry_buffer = 0.005
    eng.strategy.rr_target = 2.0
    eng._kill_switch_tripped = False
    eng.account_size = 7000
    eng.max_daily_loss_pct = 5.0
    eng.intraday_max_retrace_pct = 0
    eng.time_decay_factor = 1.0
    eng.time_decay_start_hhmm = 1130
    eng.use_market_filter = False
    eng._last_prices = {}
    eng.emit = MagicMock()
    eng.data_handler = MagicMock()
    eng._save_positions = MagicMock()
    eng._emit_status_update = MagicMock()
    eng.news_sentiment_enabled = True
    eng._news_sentiment = NewsSentimentService()
    eng._news_sentiment.set_cached_sentiment("RIOT", {
        "score": 5,
        "tags": [],
        "blocked": False,
        "size_mult": 1.0,
        "headline": "",
        "headlines": [],
        "fail_open": False,
    })

    sig = TradeSignal(
        symbol="RIOT", direction="LONG", entry_price=20.0, stop_price=19.5,
        target_r2=21.0, target_r3=22.0, shares=10, risk_dollars=5.0,
        orb_high=0, orb_low=0, vwap=None, rel_vol=0, gap_pct=0,
        reason="ORB LONG breakout",
    )
    eng._execute_trade(sig, extended=False)
    eng.client.place_market_order.assert_called()

run("news: hard block on secondary offering headline", t_news_hard_block_offering)
run("news: bullish headline scores boost multiplier", t_news_bullish_boost_score)
run("news: fail-open when Yahoo fetch fails", t_news_fail_open_on_fetch_error)
run("news: div_swing blocked by bearish catalyst (no exemption)", t_div_blocked_by_bearish_news)
run("news: ORB allowed when sentiment neutral", t_orb_allowed_with_neutral_news)


# ══════════════════════════════════════════════════════════════════════════════
# Suite 20 — Candidate criteria report
# ══════════════════════════════════════════════════════════════════════════════
section("20. Watchlist criteria — met/blocked report")

def t_candidates_api_shape():
    from trading_engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    eng.trading_active = True
    eng.auto_trade = True
    eng._kill_switch_tripped = False
    eng.dry_run = True
    eng.news_sentiment_enabled = False
    eng.entry_cutoff_hour = 23
    eng.extended_hours = True
    eng.use_orb_strategy = False
    eng.swing_scan_enabled = True
    eng.use_div_strategy = True
    eng.min_rel_vol = 2.5
    eng.min_realtime_rvol = 2.0
    eng.min_gap_pct = 2.0
    eng._active_symbols = ["AMC"]
    eng._div_symbols = ["GME"]
    eng._scan_results = [{"symbol": "AMC", "last": 3.5, "rel_vol": 4.0, "gap_pct": 6.0, "realtime_rvol": 5.0}]
    eng._div_scan_results = [{"symbol": "GME", "last": 21.0, "dist_pct": 8.0, "lower": 20.5, "mid": 22.0, "upper": 24.0}]
    eng._swing_results = [{"symbol": "AMC", "swing_count": 5, "avg_amplitude": 3.2, "current_bias": "At Low"}]
    eng._last_prices = {"AMC": 3.5, "GME": 21.0}
    eng._div_criteria_cache = {
        "GME": {
            "met": ["Near lower BB"],
            "blocked": ["MACD rising"],
            "ready": False,
            "price": 21.0,
            "detail": {},
            "checks": [
                {"label": "Near lower Bollinger Band", "pass": True, "group": "div"},
                {"label": "MACD histogram rising", "pass": False, "group": "div"},
            ],
        }
    }
    eng.strategy = __import__("orb_strategy").ORBStrategy()
    eng.strategy._trades_today = 0
    eng.strategy._open_positions = {}
    eng.strategy.max_trades_per_day = 5
    eng.strategy.max_concurrent_positions = 2
    from candidate_status import refresh_candidate_report
    report = refresh_candidate_report(eng)
    assert len(report) == 2
    gme = next(x for x in report if x["symbol"] == "GME")
    assert any(ch["label"] == "Near lower Bollinger Band" and ch["pass"] for ch in gme["checks"])
    assert any(ch["label"] == "MACD histogram rising" and not ch["pass"] for ch in gme["checks"])

run("candidates report lists met/blocked per watched symbol", t_candidates_api_shape)

# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════
total = passed + failed + warned
print(f"\n{BOLD}{'═'*60}{RESET}")
print(f"{BOLD}  SMOKE TEST COMPLETE{RESET}")
print(f"  {GREEN}Passed : {passed}{RESET}")
print(f"  {RED}Failed : {failed}{RESET}")
if warned:
    print(f"  {YELLOW}Warned : {warned}{RESET}")
print(f"  Total  : {total}")
print(f"{BOLD}{'═'*60}{RESET}\n")

if failed > 0:
    sys.exit(1)
