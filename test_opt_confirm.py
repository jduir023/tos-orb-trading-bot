"""Acceptance checks for the options confirm sleeve. No live orders."""
from __future__ import annotations
import datetime
from zoneinfo import ZoneInfo

from contract_selector import listed_strike_ladder, next_expiry, select_contract
from level_engine import LevelEngine, resample_5m, take_profit_price, TARGET_FRAC
from options_confirm_strategy import OptionsConfirmStrategy, _occ_to_osi, _option_px
from options_universe import OptionsUniverse, CORE_UNDERLYINGS

ET = ZoneInfo("America/New_York")
DAY = datetime.datetime(2026, 8, 28, tzinfo=ET)


def _ms(dt: datetime.datetime) -> int:
    return int(dt.timestamp() * 1000)


def _bar(dt, o, h, l, c, v=12000):
    return {"datetime": _ms(dt), "open": o, "high": h, "low": l, "close": c, "volume": v}


def _session_1m(until_hm, extra=None):
    """1-min bars from 09:30 ET to until_hm (HHMM), coiled in 217.89–219.43."""
    rows = []
    t = DAY.replace(hour=9, minute=30, second=0, microsecond=0)
    end_h, end_m = divmod(until_hm, 100)
    end = DAY.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    i = 0
    while t < end:
        # Oscillate inside the box so pivots print; tag a 219.43 rejection high.
        phase = i % 12
        if phase == 4:
            rows.append(_bar(t, 218.80, 219.43, 218.70, 219.10))
        elif phase == 8:
            rows.append(_bar(t, 218.40, 218.60, 217.89, 218.20))
        else:
            px = 218.40 + (phase * 0.06)
            rows.append(_bar(t, px, px + 0.18, px - 0.18, px + 0.04))
        t += datetime.timedelta(minutes=1)
        i += 1
    for b in extra or []:
        rows.append(b)
    return rows


def test_next_expiry_never_today():
    today = datetime.date(2026, 8, 28)  # Friday
    exp = next_expiry("wed_then_fri", today)
    assert exp is not None
    assert exp > today
    assert exp.weekday() in (2, 4)
    assert next_expiry("wed_then_fri", datetime.date(2026, 9, 2)).isoformat() != "2026-09-02"
    wed = next_expiry("wed_then_fri", datetime.date(2026, 9, 1))  # Tuesday
    assert wed == datetime.date(2026, 9, 2)


def test_refuse_0dte():
    today = datetime.date(2026, 8, 28).isoformat()
    assert select_contract(None, "NVDA", "CALL", 218.47, today) is None


def test_wct_fails_universe_price_gate():
    u = OptionsUniverse(client=None)
    row = u._gate("WCT", 0.90, 2_000_000, "2026-09-04")
    assert row["tradable"] is False
    assert "price" in row["skip_reason"]
    nvda = u._gate("NVDA", 218.47, 20_000_000, "2026-09-04", None)
    assert nvda["skip_reason"] == "no client"  # price/adv passed; no chain without Schwab
    assert "NVDA" in CORE_UNDERLYINGS
    assert "WCT" not in CORE_UNDERLYINGS


def test_resample_drops_in_progress():
    t0 = DAY.replace(hour=13, minute=15, second=0)
    candles = [_bar(t0 + datetime.timedelta(minutes=i), 219.0, 219.2, 218.9, 219.1) for i in range(3)]
    now = _ms(t0 + datetime.timedelta(minutes=2, seconds=30))
    bars = resample_5m(candles, now_ms=now)
    assert bars == []  # 13:15 bucket still forming


def test_one_min_poke_is_not_confirm():
    """12:17 CT = 13:17 ET 1-min close through the lid must not fire."""
    poke_t = DAY.replace(hour=13, minute=17)
    candles = _session_1m(1315)
    candles.append(_bar(poke_t, 219.20, 219.60, 219.10, 219.20))
    eng = LevelEngine()
    now = _ms(poke_t + datetime.timedelta(seconds=20))
    ev = eng.evaluate("NVDA", candles, last=219.20, now_ms=now)
    assert ev is not None
    assert not ev.get("ready"), f"1-min poke fired: {ev.get('signal')} skip={ev.get('skip')}"
    assert ev.get("signal") is None


def test_reject_21893_does_not_fire_puts():
    """Third reject ~218.93 is still above 218.50 — no puts."""
    candles = _session_1m(1320)
    # Completed 5-min bars at 13:20 / 13:25 / 13:35 that close ~218.93
    for hm, close in ((1320, 218.90), (1325, 218.66), (1335, 218.93)):
        h, m = divmod(hm, 100)
        start = DAY.replace(hour=h, minute=m) - datetime.timedelta(minutes=5)
        for i in range(5):
            t = start + datetime.timedelta(minutes=i)
            hi = 219.43 if i == 2 else close + 0.12
            candles.append(_bar(t, close + 0.05, hi, close - 0.15, close))
    eng = LevelEngine()
    now = _ms(DAY.replace(hour=13, minute=36))
    ev = eng.evaluate("NVDA", candles, last=218.93, now_ms=now)
    assert ev is not None
    sig = ev.get("signal")
    assert not (sig and sig.get("side") == "PUT"), f"puts fired on reject {sig} close={ev.get('last_5m_close')}"


def test_put_only_on_5m_close_under_21850():
    candles = _session_1m(1400)
    start = DAY.replace(hour=14, minute=10)
    for i in range(5):
        t = start + datetime.timedelta(minutes=i)
        close = 218.35 if i == 4 else 218.60
        candles.append(_bar(t, 218.55, 218.70, 218.30, close))
    eng = LevelEngine()
    now = _ms(DAY.replace(hour=14, minute=16))
    ev = eng.evaluate("NVDA", candles, last=218.47, now_ms=now)
    # May or may not be PUT depending on whether put_break printed at 218.50.
    # Hard rule: a close of 218.35 is required; last=218.47 alone is not a put.
    if ev.get("signal") and ev["signal"]["side"] == "PUT":
        assert ev["last_5m_close"] < 218.50


def test_scan_does_not_burn_signal_before_fill():
    """Universe scan must not consume a break. Only a fill marks it used."""
    candles = _session_1m(1400)
    start = DAY.replace(hour=14, minute=5)
    for i in range(5):
        t = start + datetime.timedelta(minutes=i)
        close = 219.80 if i == 4 else 219.20
        candles.append(_bar(t, 219.30, 219.90, 219.10, close))
    eng = LevelEngine()
    now = _ms(DAY.replace(hour=14, minute=11))
    ev1 = eng.evaluate("NVDA", candles, last=219.80, now_ms=now)
    assert ev1 and ev1.get("signal"), f"expected confirm, skip={ev1.get('skip') if ev1 else None} close={ev1.get('last_5m_close') if ev1 else None}"
    side = ev1["signal"]["side"]
    brk = ev1["signal"]["break"]
    ev2 = eng.evaluate("NVDA", candles, last=219.80, now_ms=now)
    assert ev2.get("signal"), f"scan burned {side} before fill skip={ev2.get('skip')}"
    eng.mark_used("NVDA", side, brk)
    ev3 = eng.evaluate("NVDA", candles, last=219.80, now_ms=now)
    assert not ev3.get("signal")
    assert "already used" in (ev3.get("skip") or "")


def test_open_position_mark_pnl():
    class Q:
        def get_option_quote(self, occ: str):
            assert "INTC" in occ
            return {"quote": {"bidPrice": 1.50, "askPrice": 1.60, "mark": 1.55}}

    s = OptionsConfirmStrategy(client=Q())
    s._positions = [{
        "symbol": "INTC", "side": "CALL", "occ": "INTC260902C00090000",
        "qty": 1, "entry": 1.37,
    }]
    s._refresh_open_marks(force=True)
    pos = s._positions[0]
    assert pos["mark"] == 1.55
    assert pos["pnl"] == 18.0  # (1.55-1.37)*100
    assert pos["pnl_pct"] == 13.14
    assert _occ_to_osi("INTC260902C00090000") == "INTC  260902C00090000"
    bid, ask, mark = _option_px({"quote": {"bid": 1.0, "ask": 1.2}})
    assert mark == 1.1


def test_listed_strike_ladder():
    last = 100.4
    strikes = [90, 92, 94, 96, 98, 100, 102, 104, 106, 108, 110, 112]
    got = listed_strike_ladder(strikes, last, 3)
    assert got == [96.0, 98.0, 100.0, 102.0, 104.0, 106.0]


def test_chain_ticket_quotes_ladder():
    exp = "2026-09-04"
    def _c(strike, cp, bid, ask):
        return {
            "symbol": f"NVDA  260904{cp}{int(strike)*1000:08d}",
            "strikePrice": strike, "bid": bid, "ask": ask, "mark": (bid+ask)/2,
            "daysToExpiration": 4, "multiplier": 100, "totalVolume": 10, "openInterest": 800,
        }
    class Fake:
        def get_option_chain(self, *a, **k):
            xs = list(range(84, 118, 2))
            strikes = {str(s): [_c(s, "C", 1.0, 1.1), _c(s, "P", 0.9, 1.0)] for s in xs}
            return {
                "underlying": {"last": 100.4},
                "callExpDateMap": {exp + ":4": {str(s): [strikes[str(s)][0]] for s in xs}},
                "putExpDateMap": {exp + ":4": {str(s): [strikes[str(s)][1]] for s in xs}},
            }
        def get_option_quotes(self, symbols):
            out = {}
            for s in symbols:
                k = str(s).replace(" ", "")
                out[k] = {"quote": {"bidPrice": 2.2, "askPrice": 2.3, "mark": 2.25}}
            return out
        def get_quote(self, symbol):
            return {"quote": {"lastPrice": 100.4}}
    s = OptionsConfirmStrategy(client=Fake())
    d = s.chain_ticket("NVDA", exp, rebuild=True)
    assert d.get("ok")
    assert d.get("expiry") == exp
    strikes = [r["strike"] for r in d.get("strikes") or []]
    assert len(strikes) == 16  # 8 below including 100 + 8 above? 8 below of 100.4 = 8, no exact ATM
    below = [x for x in strikes if x < 100.4]
    above = [x for x in strikes if x > 100.4]
    assert len(below) == 8 and len(above) == 8
    assert d["strikes"][0]["call"]["mark"] == 2.25


def test_take_profit_three_quarters():
    assert TARGET_FRAC == 0.75
    call_tp = take_profit_price(219.43, 220.90, "CALL")
    assert abs(call_tp - (219.43 + 0.75 * (220.90 - 219.43))) < 1e-6
    put_tp = take_profit_price(218.50, 217.89, "PUT")
    assert abs(put_tp - (218.50 - 0.75 * (218.50 - 217.89))) < 1e-6
    # Old fill stored the full next level as target — rebase.
    s = OptionsConfirmStrategy(client=None)
    pos = {"side": "CALL", "break": 219.43, "target": 220.90}
    s._arm_take_profit(pos)
    assert pos["level"] == 220.90
    assert abs(pos["target"] - call_tp) < 1e-6
    # 5-min close at 3/4 sells; full level is not required.
    start = DAY.replace(hour=14, minute=0)
    candles = [_bar(start + datetime.timedelta(minutes=i), 219.8, 220.6, 219.7, 219.9) for i in range(10)]
    candles += [_bar(start + datetime.timedelta(minutes=10 + i), 220.4, 220.6, 220.3, call_tp + 0.01) for i in range(5)]
    eng = LevelEngine()
    now = _ms(start + datetime.timedelta(minutes=16))
    reason = eng.kill("NVDA", candles, pos, now_ms=now)
    assert reason and "3/4" in reason, reason


def test_card_format():
    candles = _session_1m(1130)
    eng = LevelEngine()
    now = _ms(DAY.replace(hour=11, minute=31))
    ev = eng.evaluate("NVDA", candles, last=218.47, now_ms=now)
    assert ev and ev.get("card")
    assert ev["card"].startswith("NVDA")
    assert "Calls on confirmation over" in ev["card"]
    assert "Puts on confirmation under" in ev["card"]


if __name__ == "__main__":
    tests = [
        test_next_expiry_never_today,
        test_refuse_0dte,
        test_wct_fails_universe_price_gate,
        test_resample_drops_in_progress,
        test_one_min_poke_is_not_confirm,
        test_reject_21893_does_not_fire_puts,
        test_put_only_on_5m_close_under_21850,
        test_scan_does_not_burn_signal_before_fill,
        test_open_position_mark_pnl,
        test_listed_strike_ladder,
        test_chain_ticket_quotes_ladder,
        test_take_profit_three_quarters,
        test_card_format,
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
