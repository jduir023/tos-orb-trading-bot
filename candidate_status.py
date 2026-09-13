"""
candidate_status.py
Build a per-symbol criteria report for the dashboard watchlist panel.
Each candidate exposes checks: [{label, pass, group}] for checklist UI.
"""

import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _merge_checks(row: Dict[str, Any], checks: List[Dict[str, Any]]) -> None:
    existing = row.setdefault("checks", [])
    existing.extend(checks)


def refresh_candidate_report(engine) -> List[Dict[str, Any]]:
    """Rebuild the cached candidate criteria report from current engine state."""
    by_sym: Dict[str, Dict[str, Any]] = {}

    def ensure(sym: str) -> Dict[str, Any]:
        sym = (sym or "").upper()
        if sym not in by_sym:
            by_sym[sym] = {
                "symbol": sym,
                "price": float(engine._last_prices.get(sym, 0) or 0),
                "sources": [],
                "checks": [],
                "ready": False,
            }
        return by_sym[sym]

    now = datetime.datetime.now(_ET)
    trades_left = engine.strategy.max_trades_per_day - engine.strategy._trades_today
    slots_left = (
        engine.strategy.max_concurrent_positions
        - len(engine.strategy.get_active_positions())
        - len(engine.strategy.get_pending_positions())
    )

    global_checks = [
        {"label": "Bot running", "pass": bool(engine.trading_active), "group": "global"},
        {"label": "Paper auto-trade armed", "pass": bool(engine.auto_trade), "group": "global"},
        {"label": "Kill switch off", "pass": not engine._kill_switch_tripped, "group": "global"},
        {
            "label": "Daily trade slots available",
            "pass": trades_left > 0,
            "group": "global",
        },
        {
            "label": "Position slots available",
            "pass": slots_left > 0,
            "group": "global",
        },
        {
            "label": "Before entry cutoff",
            "pass": now.hour < engine.entry_cutoff_hour,
            "group": "global",
        },
    ]

    scan_by = {r["symbol"]: r for r in (engine._scan_results or [])}
    div_scan_by = {r["symbol"]: r for r in (engine._div_scan_results or [])}
    swing_by = {r["symbol"]: r for r in (engine._swing_results or [])}
    div_crit_cache = getattr(engine, "_div_criteria_cache", {}) or {}

    pending_syms = [p.symbol for p in engine.strategy.get_pending_positions()]
    open_syms = list(engine.strategy._open_positions.keys())
    watchlist = list(getattr(engine, '_watchlist', None) or [])

    all_symbols = list(dict.fromkeys(
        list(engine._active_symbols or [])
        + list(engine._div_symbols or [])
        + watchlist
        + open_syms
        + pending_syms
    ))

    need_px = [s for s in all_symbols if float(engine._last_prices.get(s, 0) or 0) <= 0]
    if need_px and getattr(engine, 'client', None):
        try:
            quotes = engine.client.get_quotes(need_px[:40])
            for sym in need_px[:40]:
                data = (quotes or {}).get(sym, {})
                px = float((data.get('quote') or {}).get('lastPrice', 0) or 0)
                if px > 0:
                    engine._last_prices[sym] = px
        except Exception:
            pass

    for sym in all_symbols:
        row = ensure(sym)
        row["checks"] = []

        price = float(engine._last_prices.get(sym, 0) or 0)

        hot_syms = (
            set(engine._active_symbols or [])
            | set(engine._div_symbols or [])
            | set(open_syms)
            | set(pending_syms)
        )
        if (
            getattr(engine, "use_5min_entry_rules", False)
            and price > 0
            and sym in hot_syms
        ):
            try:
                m5 = engine.scanner.get_5min_long_entry_check(
                    sym, price,
                    sma_period=getattr(engine, "htf_sma_period", 10),
                    dip_sma_touch_pct=getattr(engine, "dip_sma_touch_pct", 0.30),
                )
                _merge_checks(row, m5.get("checks", []))
            except Exception:
                pass

        if price <= 0:
            sr = scan_by.get(sym) or div_scan_by.get(sym)
            if sr:
                price = float(sr.get("last", 0) or 0)
        row["price"] = price

        sym_checks = list(global_checks)
        sym_checks.append({
            "label": "No open position",
            "pass": sym not in engine.strategy._open_positions,
            "group": "global",
        })
        pending = [p.symbol for p in engine.strategy.get_pending_positions()]
        sym_checks.append({
            "label": "No pending entry",
            "pass": sym not in pending,
            "group": "global",
        })

        news_pass = True
        if (
            getattr(engine, "news_sentiment_enabled", False)
            and not engine.dry_run
            and getattr(engine, "_news_sentiment", None)
        ):
            try:
                sent = engine._news_sentiment.get_sentiment(sym)
                news_pass = not bool(sent.get("blocked"))
                sym_checks.append({
                    "label": "News catalyst clear",
                    "pass": news_pass,
                    "group": "global",
                })
            except Exception:
                sym_checks.append({
                    "label": "News catalyst clear",
                    "pass": True,
                    "group": "global",
                })

        _merge_checks(row, sym_checks)

        if sym in watchlist:
            if 'watchlist' not in row['sources']:
                row['sources'].append('watchlist')
            _merge_checks(row, [{
                'label': 'On user watchlist',
                'pass': True,
                'group': 'watchlist',
            }])
            if engine.use_div_strategy and sym not in (engine._div_symbols or []):
                crit = div_crit_cache.get(sym)
                if crit and crit.get('checks'):
                    if 'div' not in row['sources']:
                        row['sources'].append('div')
                    _merge_checks(row, crit['checks'])
                    if crit.get('price'):
                        row['price'] = float(crit['price'])
                elif engine.use_div_strategy:
                    _merge_checks(row, [{
                        'label': 'Div criteria (awaiting scan)',
                        'pass': False,
                        'group': 'div',
                    }])
            if engine.use_orb_strategy and sym not in (engine._active_symbols or []):
                levels = engine.strategy._orb_levels.get(sym)
                px = float(row.get('price', 0) or 0)
                orb_wl = [{'label': 'ORB strategy enabled', 'pass': True, 'group': 'orb'}]
                if levels:
                    orb_wl.append({'label': 'ORB levels loaded', 'pass': True, 'group': 'orb'})
                    if px > 0:
                        orb_wl.append({
                            'label': f"Price above ORB high (${levels['high']:.2f})",
                            'pass': px > float(levels['high']),
                            'group': 'orb',
                        })
                else:
                    orb_wl.append({'label': 'ORB levels loaded', 'pass': False, 'group': 'orb'})
                if 'orb' not in row['sources']:
                    row['sources'].append('orb')
                _merge_checks(row, orb_wl)

        if sym in (engine._active_symbols or []):
            if "scanner" not in row["sources"]:
                row["sources"].append("scanner")
            sr = scan_by.get(sym)
            orb_checks = [
                {
                    "label": "In active scan universe",
                    "pass": True,
                    "group": "scanner",
                },
                {
                    "label": "ORB strategy enabled",
                    "pass": bool(engine.use_orb_strategy),
                    "group": "orb",
                },
            ]
            if sr:
                orb_checks.extend([
                    {
                        "label": f"Rel volume >= {getattr(engine, 'min_rel_vol', 2.5)}x",
                        "pass": float(sr.get("rel_vol", 0) or 0) >= float(getattr(engine, "min_rel_vol", 2.5)),
                        "group": "scanner",
                    },
                    {
                        "label": f"Realtime rvol >= {getattr(engine, 'min_realtime_rvol', 2.0)}x",
                        "pass": float(sr.get("realtime_rvol", 0) or 0) >= float(getattr(engine, "min_realtime_rvol", 2.0)),
                        "group": "scanner",
                    },
                    {
                        "label": f"Price change >= {getattr(engine, 'min_gap_pct', 2.0)}%",
                        "pass": abs(float(sr.get("gap_pct", 0) or 0)) >= float(getattr(engine, "min_gap_pct", 2.0)),
                        "group": "scanner",
                    },
                    {
                        "label": "Cameron setup",
                        "pass": bool(sr.get("cameron_setup")),
                        "group": "scanner",
                    },
                ])
            if engine.use_orb_strategy:
                orb_checks.append({
                    "label": "ORB breakout signal",
                    "pass": False,
                    "group": "orb",
                })
            _merge_checks(row, orb_checks)

        if sym in (engine._div_symbols or []):
            if "div" not in row["sources"]:
                row["sources"].append("div")
            crit = div_crit_cache.get(sym)
            if crit and crit.get("checks"):
                _merge_checks(row, crit["checks"])
                if crit.get("price"):
                    row["price"] = float(crit["price"])
            elif div_scan_by.get(sym):
                ds = div_scan_by[sym]
                _merge_checks(row, [
                    {
                        "label": "Near lower Bollinger Band",
                        "pass": True,
                        "group": "div",
                    },
                    {
                        "label": "Full divergence check",
                        "pass": False,
                        "group": "div",
                    },
                ])
                row["price"] = float(ds.get("last", row["price"]) or row["price"])

        if sym in swing_by and engine.swing_scan_enabled:
            if "swing" not in row["sources"]:
                row["sources"].append("swing")
            sw = swing_by[sym]
            try:
                in_session = engine._in_any_session(now)
            except Exception:
                in_session = True
            _merge_checks(row, [
                {
                    "label": "Swing scan enabled",
                    "pass": True,
                    "group": "swing",
                },
                {
                    "label": "Minimum swing count",
                    "pass": int(sw.get("swing_count", 0) or 0) >= 3,
                    "group": "swing",
                },
                {
                    "label": "At swing low (bias)",
                    "pass": sw.get("current_bias") == "At Low",
                    "group": "swing",
                },
                {
                    "label": "In trading session",
                    "pass": in_session,
                    "group": "swing",
                },
            ])

        checks = row.get("checks", [])
        row["ready"] = bool(checks) and all(c.get("pass") for c in checks)

    report = sorted(by_sym.values(), key=lambda x: x["symbol"])
    import time as _time
    engine._candidate_report = report
    engine._candidate_report_ts = _time.time()
    return report


def get_candidates_report(engine) -> List[Dict[str, Any]]:
    """Return cached report, refreshing if empty."""
    report = getattr(engine, "_candidate_report", None)
    if not report:
        return refresh_candidate_report(engine)
    return report