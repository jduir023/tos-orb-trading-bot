"""
web_dashboard.py
Flask + SocketIO web dashboard for the TOS ORB Bot.
Runs on VPS — access from any browser at http://your-vps-ip:5000
"""

import threading
from flask import Flask, jsonify, make_response, render_template, request
from flask_socketio import SocketIO, emit

from trading_engine import TradingEngine
from backtest_engine import BacktestEngine as _BacktestEngine
_backtest_engine = _BacktestEngine()
import journal as _journal
from utils import log_message, format_12h, now_et
from pillars import SPECS as PILLAR_SPECS, TRIGGER_PCT as PILLAR_TRIGGER_PCT
from polymarket_monitor import PolymarketMonitor


app = Flask(__name__)
import os as _os
app.config["SECRET_KEY"] = _os.environ.get("TOS_SECRET_KEY", "tosbot-secret-change-me")
app.config["TEMPLATES_AUTO_RELOAD"] = True
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# When running behind a reverse proxy at a subpath, set SOCKET_PATH env var
# e.g. SOCKET_PATH=/tos-trading-2026/socket.io
_SOCKET_PATH  = _os.environ.get("SOCKET_PATH", "/socket.io")
_BASE_PATH    = _os.environ.get("BOT_BASE_PATH", "")

import time as _wtime
_bot_start_time = _wtime.time()
from scanner_proxy import scanner_bp
app.register_blueprint(scanner_bp)

engine = TradingEngine()

def _boot_book_streamer():
    try:
        from scanner_proxy import _get_streamer
        _get_streamer()
    except Exception as _exc:
        log_message(f"[STREAMER] boot: {_exc}")

threading.Thread(target=_boot_book_streamer, daemon=True, name="book-streamer-boot").start()

# Polymarket signal monitor (started on demand via /api/polymarket/start)
def _pm_emit(event, data):
    socketio.emit(event, data)

pm_monitor = PolymarketMonitor(engine=engine, emit_callback=_pm_emit)

def _serialize_position(p) -> dict:
    """OpenPosition / pending → dict with strategy tags and stop-loss $."""
    d = dict(vars(p)) if not isinstance(p, dict) else dict(p)
    reason = str(d.get("entry_reason") or "")
    tags = _journal.classify_strategy(reason)
    d["strategy"] = tags["strategy"]
    d["strategy_key"] = tags["strategy_key"]
    risk = float(d.get("risk_dollars") or 0)
    entry = float(d.get("entry_price") or 0)
    stop = float(d.get("stop_price") or 0)
    shares = int(d.get("shares") or 0)
    if risk <= 0 and entry and stop and shares:
        risk = abs(entry - stop) * shares
    d["stop_loss_dollars"] = round(risk, 2)
    d["risk_dollars"] = round(float(d.get("risk_dollars") or risk), 2)
    # drop non-JSON noise if any
    for k in list(d.keys()):
        if k.startswith("_"):
            d.pop(k, None)
    return d


def _open_risk_dollars() -> float:
    total = 0.0
    for p in engine.get_open_positions():
        d = _serialize_position(p)
        total += float(d.get("stop_loss_dollars") or 0)
    return round(total, 2)


def _build_pipeline() -> list:
    """Multi-source watching / hot symbols for Ops pipeline."""
    by_sym = {}

    def add(symbol, state, strategy, strategy_key, detail="", score=None, price=None, extra=None):
        symbol = (symbol or "").upper().strip()
        if not symbol:
            return
        pri = {"held": 50, "pending": 40, "hot": 30, "scanning": 20, "watching": 10}.get(state, 0)
        prev = by_sym.get(symbol)
        if prev and prev.get("_pri", 0) > pri:
            return
        row = {
            "symbol": symbol,
            "state": state,
            "strategy": strategy,
            "strategy_key": strategy_key,
            "detail": detail or "",
            "score": score,
            "price": price,
            "_pri": pri,
        }
        if extra:
            row.update(extra)
        by_sym[symbol] = row

    held_syms = {p.symbol.upper() for p in engine.get_open_positions()}
    pend_syms = {p.symbol.upper() for p in engine.get_pending_positions()}
    for p in engine.get_open_positions():
        d = _serialize_position(p)
        add(d["symbol"], "held", d.get("strategy") or "Held", d.get("strategy_key") or "unknown",
            detail="Open position", price=d.get("entry_price"))
    for p in engine.get_pending_positions():
        d = _serialize_position(p)
        add(d["symbol"], "pending", d.get("strategy") or "Pending", d.get("strategy_key") or "unknown",
            detail="Pending fill", price=d.get("entry_price"))

    for s in (engine.get_watchlist() or []):
        if s.upper() not in held_syms and s.upper() not in pend_syms:
            add(s, "watching", "Watchlist", "watchlist", detail="Manual watchlist")

    for r in (getattr(engine, "options_strategy", None) and engine.options_strategy.get_candidates() or []):
        if isinstance(r, dict) and r.get("symbol"):
            side = ((r.get("signal") or {}).get("side") if isinstance(r.get("signal"), dict) else None) or (
                "CALL" if r.get("ready") else "—"
            )
            add(r.get("symbol"), "hot" if r.get("ready") else "scanning", "Options Confirm", "opt_confirm",
                detail=r.get("level_skip") or r.get("skip_reason") or side,
                price=r.get("last"), score=r.get("score_pct"),
                extra={"support": r.get("put_break") or r.get("support"),
                       "resistance": r.get("call_break") or r.get("resistance")})

    for r in (engine.get_scan_results() or []):
        if not isinstance(r, dict):
            continue
        sym = r.get("symbol") or r.get("ticker") or ""
        gap = r.get("gap_pct") or r.get("gap") or r.get("change_pct")
        detail = f"Gap {gap:+.1f}%" if isinstance(gap, (int, float)) else "Scanner"
        rvol = r.get("rel_vol") or r.get("rvol") or r.get("relative_volume")
        if rvol is not None:
            detail += f" · RVol {rvol}"
        add(sym, "scanning", "Scanner", "scanner", detail=detail,
            price=r.get("price") or r.get("last"), score=r.get("score"),
            extra={"gap_pct": gap, "rel_vol": rvol,
                   "support": r.get("support"), "resistance": r.get("resistance")})

    for s in (getattr(engine, "_active_symbols", None) or []):
        add(s, "hot", "ORB", "orb", detail="ORB active universe")

    for s in (getattr(engine, "_div_symbols", None) or []):
        add(s, "hot", "Divergence", "divergence", detail="Div candidate")

    for r in (getattr(engine, "_div_scan_results", None) or []):
        if isinstance(r, dict):
            add(r.get("symbol"), "hot", "Divergence", "divergence",
                detail=r.get("reason") or r.get("detail") or "Div scan",
                price=r.get("price") or r.get("last"), score=r.get("score"))

    for r in (getattr(engine, "_swing_results", None) or []):
        if isinstance(r, dict):
            add(r.get("symbol"), "hot", "Swing", "swing",
                detail=f"Swings {r.get('swings', r.get('swing_count', '—'))}",
                price=r.get("last") or r.get("price"), score=r.get("swings"))

    try:
        for r in (engine.rsi_strategy.get_candidates() or []):
            if isinstance(r, dict):
                ready = r.get("ready")
                add(r.get("symbol"), "hot" if ready else "scanning", "RSI", "rsi",
                    detail=("Ready" if ready else "Watch") + f" RSI={r.get('rsi', '—')}",
                    price=r.get("price") or r.get("last"), score=r.get("score"))
    except Exception:
        pass

    try:
        screener = getattr(engine, "scalp_screener", None)
        if screener:
            for r in (screener.get_results() or []):
                if isinstance(r, dict):
                    add(r.get("symbol"), "hot", "Scalp", "scalp",
                        detail=f"Score {r.get('score', '—')} · dip {r.get('dip_pct', '—')}%",
                        price=r.get("price") or r.get("last"), score=r.get("score"))
    except Exception:
        pass

    rows = list(by_sym.values())
    for r in rows:
        r.pop("_pri", None)
        try:
            lv = engine.sr_engine.get(r.get("symbol") or "")
            if lv:
                r.setdefault("support", lv.get("support"))
                r.setdefault("resistance", lv.get("resistance"))
        except Exception:
            pass
    # held first, then pending, hot, scanning, watching
    order = {"held": 0, "pending": 1, "hot": 2, "scanning": 3, "watching": 4}
    rows.sort(key=lambda x: (order.get(x.get("state"), 9), x.get("symbol") or ""))
    return rows




# ------------------------------------------------------------------
# Wire engine events → SocketIO broadcasts
# ------------------------------------------------------------------

def _setup_engine_events():
    engine.on("log",          lambda m: socketio.emit("log",          {"msg": str(m)}))
    engine.on("balance",      lambda d: socketio.emit("balance",      d or {}))
    engine.on("positions",    lambda d: socketio.emit("positions",    d or {}))
    engine.on("scan_results", lambda r: socketio.emit("scan_results", {"results": r or []}))
    engine.on("swing_results",lambda r: socketio.emit("swing_results",{"results": r or []}))
    engine.on("rsi_scan_results", lambda r: socketio.emit("rsi_scan_results", {"results": r or []}))
    engine.on("scalp_scan_results", lambda r: socketio.emit("scalp_scan_results", {"results": r or []}))
    engine.on("trade_placed", lambda d: socketio.emit("trade_placed", d or {}))
    engine.on("trade_upgraded",  lambda d: socketio.emit("trade_upgraded",  d or {}))
    engine.on("day_pnl",         lambda v: socketio.emit("day_pnl",         {"pnl": float(v or 0)}))
    engine.on("status",       lambda s: socketio.emit("status",       {"status": str(s)}))

    engine.on("paper",        lambda d: socketio.emit("paper",        d or {}))
    engine.on("sim_day",      lambda d: socketio.emit("sim_day",      d or {}))
    engine.on("trade_closed", lambda d: socketio.emit("trade_closed", d or {}))
    engine.on("signal",       lambda s: socketio.emit("signal", {
        "symbol":      s.symbol,
        "direction":   s.direction,
        "entry":       s.entry_price,
        "stop":        s.stop_price,
        "target_r2":   s.target_r2,
        "shares":      s.shares,
        "risk":        s.risk_dollars,
        "reason":      s.reason,
    }))


_setup_engine_events()


# ------------------------------------------------------------------
# REST API
# ------------------------------------------------------------------

@app.route("/")
def index():
    resp = make_response(render_template("index.html", socket_path=_SOCKET_PATH, base_path=_BASE_PATH))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/status")
def api_status():
    # Trigger a token refresh if access token is expired but refresh token exists.
    # This ensures the dashboard shows "authenticated: true" immediately after a
    # bot restart rather than waiting for the next API call to self-heal.
    if not engine.is_authenticated() and not getattr(engine, "sim_mode", False):
        try:
            engine.client._ensure_token()
        except Exception:
            pass
    open_risk = _open_risk_dollars()
    acct = float(getattr(engine, "account_size", 0) or 0)
    return jsonify({
        "trading_active":           engine.trading_active,
        "authenticated":            engine.is_authenticated(),
        "dry_run":                  True,
        "paper_mode":               True,
        "auto_trade":               bool(engine.auto_trade),
        "paper":                    engine.paper.snapshot(getattr(engine, "_last_prices", {}) or {}),
        "active_strategy":          engine.active_strategy(),
        "extended_hours":           engine.extended_hours,
        "swing_enabled":            engine.swing_scan_enabled,
        "div_strategy_enabled":      getattr(engine, "use_div_strategy", False),
        "orb_strategy_enabled":      getattr(engine, "use_orb_strategy", True),
        "scalp_enabled":              getattr(engine, "scalp_enabled", False),
        "scalp_symbol":               getattr(engine, "scalp_symbol", ""),
        "scalp_session":              engine.scalp_strategy.get_session_stats(),
        "rsi_enabled":                getattr(engine, "rsi_enabled", False),
        "options_enabled":            getattr(engine, "options_enabled", False),
        "trades_today":             engine.get_trades_today(),
        "day_pnl":                  engine.get_day_pnl(),
        "active_symbols":           engine._active_symbols,
        "div_symbols":              getattr(engine, "_div_symbols", []),
        "open_positions":           len(engine.get_open_positions()),
        "pending_positions":        len(engine.get_pending_positions()),
        "max_concurrent":           engine.max_concurrent,
        "session":                  engine.get_session_status(),
        "sim_mode":                 False,
        "account_size":             float(engine.paper.starting_cash),
        "open_risk_dollars":        open_risk,
        "open_risk_pct":            round(open_risk / acct * 100, 2) if acct > 0 else 0,
    })




def _stamp_trade_times(trades):
    """Attach 12-hour Eastern display strings to journal trades."""
    for t in trades or []:
        t["entry_time_display"] = format_12h(t.get("entry_time"))
        t["exit_time_display"] = format_12h(t.get("exit_time"))
    return trades


def _scanner_row(scanner_id, name, enabled, last_ts, interval, result_count,
                 detail="", error="", scan_now_id="", results=None):
    """Build one scanner health card payload."""
    now = _wtime.time()
    last_ts = float(last_ts or 0)
    interval = float(interval or 60) or 60.0
    ago = (now - last_ts) if last_ts else None
    bot_on = bool(engine.trading_active)
    if error:
        status = "error"
        label = "Error"
    elif not enabled:
        status = "off"
        label = "Off"
    elif not bot_on:
        status = "off"
        label = "Bot stopped"
    elif not last_ts:
        status = "idle"
        label = "Waiting for first scan"
    elif ago is not None and ago > max(interval * 3, 90):
        status = "stale"
        label = "Stale — not scanning"
    else:
        status = "ok"
        label = "Working" if result_count else "Working · no hits"
    return {
        "id": scanner_id,
        "name": name,
        "enabled": bool(enabled),
        "status": status,
        "label": label,
        "last_scan_ts": last_ts,
        "last_scan": format_12h(last_ts, with_date=False, with_seconds=True) if last_ts else "Never",
        "last_scan_ago_sec": round(ago, 1) if ago is not None else None,
        "interval_sec": interval,
        "result_count": int(result_count or 0),
        "detail": detail or "",
        "error": error or "",
        "scan_now": scan_now_id,
        "results": results or [],
    }


def _scanner_health_payload():
    gap = list(engine.get_scan_results() or [])
    gap_up = sum(1 for r in gap if float(r.get("gap_pct") or 0) > 0)
    gap_dn = len(gap) - gap_up
    scanner = getattr(engine, "scanner", None)
    gap_ts = float(getattr(scanner, "_last_scan_ts", 0) or 0)
    gap_err = str(getattr(scanner, "_last_error", "") or "")
    gap_running = bool(getattr(scanner, "_running", False))

    rsi_data = {}
    try:
        rsi_data = engine.rsi_strategy.get_tab_data() or {}
    except Exception:
        rsi_data = {}
    rsi_cands = rsi_data.get("candidates") or []
    rsi_ready = sum(1 for c in rsi_cands if c.get("ready"))

    div_cands = list(getattr(engine, "_div_scan_results", None) or [])
    swing_cands = list(engine.get_swing_results() or [])
    scalp_cands = []
    try:
        screener = getattr(engine, "scalp_screener", None)
        if screener:
            scalp_cands = list(screener.get_results() or [])
    except Exception:
        scalp_cands = []

    orb_levels = {}
    try:
        orb_levels = dict(getattr(engine.strategy, "_orb_levels", {}) or {})
    except Exception:
        orb_levels = {}

    last_loop = getattr(engine, "_last_loop_time", 0) or 0
    loop_lag = round(_wtime.time() - last_loop, 1) if last_loop else None

    def _slim(r, extra=None):
        row = {
            "symbol": r.get("symbol"),
            "price": r.get("price") or r.get("last"),
            "ready": r.get("ready"),
            "passed": r.get("passed"),
            "total": r.get("total"),
            "score_pct": r.get("score_pct"),
            "pillars": r.get("pillars") or [],
        }
        if extra:
            row.update(extra)
        return row

    gap_ready = sum(1 for r in gap if r.get("ready"))
    scanners = [
        _scanner_row(
            "gap", "Gap / Movers",
            enabled=gap_running or engine.trading_active,
            last_ts=gap_ts,
            interval=getattr(engine, "scan_interval_sec", 60),
            result_count=len(gap),
            detail=f"{gap_ready} ready · {gap_up} gap-up · {gap_dn} gap-down · trade at 95%",
            error=gap_err,
            scan_now_id="",
            results=[_slim(r, {"gap_pct": r.get("gap_pct"), "rel_vol": r.get("rel_vol")}) for r in gap[:12]],
        ),
        _scanner_row(
            "orb", "ORB Levels",
            enabled=bool(getattr(engine, "use_orb_strategy", False)),
            last_ts=gap_ts if orb_levels else 0,
            interval=getattr(engine, "scan_interval_sec", 60),
            result_count=len(orb_levels),
            detail=("Levels loaded for " + ", ".join(list(orb_levels)[:6])
                    if orb_levels else "No ORB levels yet — needs the opening range to print (first 15 min of regular session)"),
            scan_now_id="",
        ),
        _scanner_row(
            "rsi", "RSI Mean Reversion",
            enabled=bool(getattr(engine, "rsi_enabled", False)),
            last_ts=float(rsi_data.get("last_scan") or getattr(engine, "_last_rsi_scan", 0) or 0),
            interval=getattr(engine, "rsi_scan_interval_sec", 300),
            result_count=len(rsi_cands),
            detail=f"{rsi_ready} ready / 6 pillars · trade at 95%",
            scan_now_id="rsi",
            results=[_slim(c, {"rsi": c.get("rsi")}) for c in rsi_cands[:12]],
        ),
        _scanner_row(
            "divergence", "Divergence",
            enabled=bool(getattr(engine, "use_div_strategy", False)),
            last_ts=float(getattr(engine, "_last_div_scan", 0) or 0),
            interval=getattr(engine, "_div_scan_interval", 60),
            result_count=len(div_cands),
            detail=f"{sum(1 for c in div_cands if c.get('ready'))} ready / 6 pillars · trade at 95%",
            scan_now_id="divergence",
            results=[_slim(c, {"dist_pct": c.get("dist_pct")}) for c in div_cands[:12]],
        ),
        _scanner_row(
            "swing", "Swing",
            enabled=bool(getattr(engine, "swing_scan_enabled", False)),
            last_ts=float(getattr(engine, "_last_swing_scan", 0) or 0),
            interval=getattr(engine, "_swing_scan_interval", 60),
            result_count=len(swing_cands),
            detail=f"{sum(1 for c in swing_cands if c.get('ready'))} ready / 6 pillars · trade at 95%",
            scan_now_id="swing",
            results=[_slim(c, {"swings": c.get("swing_count"), "bias": c.get("current_bias")}) for c in swing_cands[:12]],
        ),
        _scanner_row(
            "scalp", "Scalp Dip-to-Support",
            enabled=bool(getattr(engine, "scalp_enabled", False)),
            last_ts=float(getattr(engine, "_last_scalp_scan", 0) or getattr(getattr(engine, "scalp_screener", None), "_last_ts", 0) or 0),
            interval=getattr(engine, "scalp_scan_interval_sec", 60),
            result_count=len(scalp_cands),
            detail=f"{sum(1 for c in scalp_cands if c.get('ready'))} ready / 6 pillars · trade at 95%",
            scan_now_id="scalp",
            results=[_slim(c, {"dip_pct": c.get("dip_pct")}) for c in scalp_cands[:12]],
        ),
    ]
    opt_cands = []
    try:
        opt_cands = engine.get_options_results()
    except Exception:
        opt_cands = []
    opt_tab = {}
    try:
        opt_tab = engine.options_strategy.get_tab_data() or {}
    except Exception:
        opt_tab = {}
    scanners.append(_scanner_row(
        "options", "Options Confirm",
        enabled=bool(getattr(engine, "use_options_confirm", False)),
        last_ts=float(opt_tab.get("last_scan") or getattr(engine, "_last_options_scan", 0) or 0),
        interval=getattr(engine, "options_scan_interval_sec", 60),
        result_count=len(opt_cands),
        detail=(
            f"{sum(1 for c in opt_cands if c.get('tradable'))} tradable · "
            f"{sum(1 for c in opt_cands if c.get('ready'))} confirm · "
            f"next {opt_tab.get('expiry') or 'Wed/Fri'} · never 0DTE"
        ),
        scan_now_id="options",
        results=[_slim(c, {
            "support": c.get("put_break") or c.get("support"),
            "resistance": c.get("call_break") or c.get("resistance"),
            "skip": c.get("skip_reason") or c.get("level_skip"),
            "expiry": c.get("expiry"),
        }) for c in opt_cands[:12]],
    ))
    working = sum(1 for s in scanners if s["status"] == "ok")
    return {
        "ok": True,
        "clock": format_12h(now_et(), with_date=True, with_seconds=True),
        "timezone": "America/New_York",
        "trading_active": bool(engine.trading_active),
        "authenticated": bool(engine.is_authenticated()),
        "session": engine.get_session_status(),
        "loop_lag_seconds": loop_lag,
        "working": working,
        "total": len(scanners),
        "trigger_pct": PILLAR_TRIGGER_PCT,
        "pillar_specs": PILLAR_SPECS,
        "scanners": scanners,
    }


@app.route("/api/scanner_health")
def api_scanner_health():
    """Live heartbeat for every scanner — working / stale / off / error."""
    return jsonify(_scanner_health_payload())


@app.route("/api/calendar")
def api_calendar():
    """24-month standard calendar of daily P/L and timestamped trades."""
    months = request.args.get("months", 24)
    try:
        months = max(1, min(int(months), 24))
    except Exception:
        months = 24
    trades = _journal.load_journal_trades()
    payload = _journal.build_calendar(trades, months=months)
    payload["ok"] = True
    return jsonify(payload)


@app.route("/api/paper", methods=["GET"])
def api_paper():
    return jsonify({"ok": True, **engine.paper.snapshot(getattr(engine, "_last_prices", {}) or {})})


@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    engine.paper.reset()
    # flatten in-memory strategy book so it matches the cash reset
    try:
        engine.strategy._open_positions.clear()
        engine.options_strategy._positions = []
        engine.options_strategy._save()
        engine._save_positions()
    except Exception:
        pass
    snap = engine.paper.snapshot({})
    engine.emit("paper", snap)
    engine.emit("log", "[PAPER] book reset to $5,000.00")
    return jsonify({"ok": True, **snap})


@app.route("/api/health")
def api_health():
    """Lightweight health check for monitoring tools and uptime services.
    Returns system vitals: uptime, auth, loop lag, win rate, kill switch state.
    """
    import os as _os_h
    uptime_s   = int(_wtime.time() - _bot_start_time)
    last_loop  = getattr(engine, "_last_loop_time", 0)
    loop_lag   = round(_wtime.time() - last_loop, 1) if last_loop else None
    return jsonify({
        "status":             "ok",
        "uptime_seconds":     uptime_s,
        "uptime_human":       f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m",
        "authenticated":      engine.is_authenticated(),
        "trading_active":     engine.trading_active,
        "sim_mode":           False,
        "dry_run":            True,
        "paper_mode":         True,
        "loop_lag_seconds":   loop_lag,
        "kill_switch_active": getattr(engine, "_kill_switch_tripped", False),
        "open_positions":     len(engine.get_open_positions()),
        "pending_positions":  len(engine.get_pending_positions()),
        "day_pnl":            engine.get_day_pnl(),
        "api_keys_from_env":  bool(_os_h.getenv("SCHWAB_APP_KEY")),
    })
@app.route("/api/balance")
def api_balance():
    return jsonify(engine.get_balance())


@app.route("/api/positions")
def api_positions():
    held = [_serialize_position(p) for p in engine.get_open_positions()]
    pending = [_serialize_position(p) for p in engine.get_pending_positions()]
    closed = [_serialize_position(p) for p in engine.get_today_closed_positions()]
    open_risk = round(sum(float(p.get("stop_loss_dollars") or 0) for p in held), 2)
    return jsonify({
        "pending": pending,
        "held": held,
        "open": held,
        "open_risk_dollars": open_risk,
    })


@app.route("/api/ops_snapshot")
def api_ops_snapshot():
    """Single poll for Ops dashboard: book + strategies + pipeline."""
    held = [_serialize_position(p) for p in engine.get_open_positions()]
    pending = [_serialize_position(p) for p in engine.get_pending_positions()]
    closed = [_serialize_position(p) for p in engine.get_today_closed_positions()]
    open_risk = round(sum(float(p.get("stop_loss_dollars") or 0) for p in held), 2)
    acct = float(getattr(engine, "account_size", 0) or 0)
    day_pnl = engine.get_day_pnl()
    wins = sum(1 for p in closed if float(p.get("pnl") or 0) > 0)
    losses = sum(1 for p in closed if float(p.get("pnl") or 0) < 0)
    return jsonify({
        "ok": True,
        "trading_active": engine.trading_active,
        "authenticated": engine.is_authenticated(),
        "session": engine.get_session_status(),
        "dry_run": engine.dry_run,
        "extended_hours": engine.extended_hours,
        "sim_mode": False,
        "day_pnl": day_pnl,
        "day_pnl_pct": round(day_pnl / acct * 100, 2) if acct > 0 else 0,
        "account_size": acct,
        "open_risk_dollars": open_risk,
        "open_risk_pct": round(open_risk / acct * 100, 2) if acct > 0 else 0,
        "open_positions": len(held),
        "pending_positions": len(pending),
        "max_concurrent": engine.max_concurrent,
        "trades_today": engine.get_trades_today(),
        "closed_wins": wins,
        "closed_losses": losses,
        "strategies": engine.get_strategy_status(),
        "pending": pending,
        "held": held,
        "closed": closed,
        "pipeline": _build_pipeline(),
    })


@app.route("/api/swing_results")
def api_swing_results():
    return jsonify({"results": engine.get_swing_results()})


@app.route("/api/scan_results")
def api_scan_results():
    return jsonify({"results": engine.get_scan_results()})


@app.route("/api/watchlist", methods=["GET", "POST"])
def api_watchlist():
    if request.method == "POST":
        data = request.get_json() or {}
        symbols = data.get("symbols", [])
        if symbols:
            engine.set_watchlist(symbols)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "No symbols provided"}), 400
    return jsonify({"symbols": engine.get_watchlist()})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            engine.set_config(
                risk_pct=float(data.get("risk_pct", engine.risk_pct)),
                orb_minutes=int(data.get("orb_minutes", engine.orb_minutes)),
                max_trades=int(data.get("max_trades", engine.max_trades)),
                max_concurrent=int(data.get("max_concurrent", engine.max_concurrent)),
                rr_target=float(data.get("rr_target", engine.rr_target)),
                entry_cutoff_hour=int(data.get("entry_cutoff_hour", engine.entry_cutoff_hour)),
                require_vwap_above=bool(data.get("require_vwap_above", engine.require_vwap_above)),
                auto_trade=bool(data.get("auto_trade", engine.auto_trade)),
                extended_hours=bool(data.get("extended_hours", engine.extended_hours)),
                limit_entry_buffer=float(data.get("limit_entry_buffer", engine.limit_entry_buffer * 100)) / 100.0,
                trail_trigger_r=float(data.get("trail_trigger_r", engine.trail_trigger_r)),
                trail_vol_mult=float(data.get("trail_vol_mult", engine.trail_vol_mult)),
                trail_pct=float(data.get("trail_pct", engine.trail_pct)),
                scan_interval_sec=float(data.get("scan_interval_sec", engine.scan_interval_sec)),
                adx_min=float(data.get("adx_min", engine.adx_min)),
                runner_target_r=float(data.get("runner_target_r", engine.runner_target_r)),
                use_macd_filter=bool(data.get("use_macd_filter", engine.use_macd_filter)),
                use_adx_filter=bool(data.get("use_adx_filter", engine.use_adx_filter)),
                use_htf_filter=bool(data.get("use_htf_filter", engine.use_htf_filter)),
                use_macd_exit=bool(data.get("use_macd_exit", engine.use_macd_exit)),
                fp_time_limit_hhmm=int(data.get("fp_time_limit_hhmm", engine.fp_time_limit_hhmm)),
                fp_max_stop_cents=float(data.get("fp_max_stop_cents", engine.fp_max_stop_cents)),
                fp_min_pole_pct=float(data.get("fp_min_pole_pct", engine.fp_min_pole_pct)),
            )
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(engine.data_handler.load_config())


def _validate_schwab_keys(key: str, secret: str):
    """Basic format check — Schwab consumer keys are alphanumeric."""
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key or not secret:
        return "App Key and App Secret are required."
    if not VALID_KEY.match(key):
        return (
            "App Key looks invalid. Copy the Consumer Key from developer.schwab.com "
            "(letters/numbers only, no spaces or special characters)."
        )
    if not VALID_KEY.match(secret):
        return (
            "App Secret looks invalid. Copy the Secret from developer.schwab.com "
            "(letters/numbers only)."
        )
    if key.upper().startswith("YOUR_") or "HERE" in key.upper():
        return "Replace the placeholder App Key with your real Schwab Consumer Key."
    return None


VALID_KEY = __import__("re").compile(r"^[A-Za-z0-9]{20,64}$")


@app.route("/api/auth", methods=["POST"])
def api_auth():
    data = request.get_json() or {}
    key    = data.get("app_key", "").strip()
    secret = data.get("app_secret", "").strip()
    err = _validate_schwab_keys(key, secret)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    engine.set_api_keys(key, secret)
    # Authorization opens a browser on the VPS — for VPS use the manual_code flow
    return jsonify({
        "ok": True,
        "message": "Keys saved. Use /api/auth/url to get the authorization URL, then POST the code to /api/auth/code"
    })


@app.route("/api/auth/url")
def api_auth_url():
    """Returns the Schwab authorization URL for manual OAuth on VPS."""
    import urllib.parse
    from schwab_client import SCHWAB_AUTH_URL, REDIRECT_URI
    if not engine._schwab_client.app_key:
        return jsonify({"error": "App key not set. Save your keys first."}), 400
    params = {
        "response_type": "code",
        "client_id":     engine._schwab_client.app_key,
        "redirect_uri":  REDIRECT_URI,
        "scope":         "readonly trading",
    }
    url = SCHWAB_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return jsonify({"url": url, "redirect_uri": REDIRECT_URI})


@app.route("/api/auth/code", methods=["POST"])
def api_auth_code():
    """Accept the OAuth code from the redirect URL after manual browser auth."""
    data = request.get_json() or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "Missing code"}), 400
    result = engine._schwab_client._exchange_code(code)
    if result is True:
        return jsonify({"ok": True})
    if isinstance(result, dict):
        desc = result.get("error_description") or result.get("error") or "Unknown error"
        return jsonify({"ok": False, "error": result.get("error", "auth_error"), "error_description": desc})
    return jsonify({"ok": False, "error": "auth_failed", "error_description": "Token exchange failed"})


@app.route("/api/start", methods=["POST"])
def api_start():
    if not engine.is_authenticated() and not engine.sim_mode:
        return jsonify({"ok": False, "error": "Not authenticated. Connect Schwab for live market data (paper fills still need quotes)."}), 401
    engine.start()
    return jsonify({"ok": True, "status": "running"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    engine.stop()
    return jsonify({"ok": True, "status": "stopped"})


@app.route("/api/trade/buy", methods=["POST"])
def api_manual_buy():
    data = request.get_json() or {}
    sym  = data.get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"ok": False, "error": "Missing symbol"}), 400
    threading.Thread(target=engine.manual_buy, args=(sym,), daemon=True).start()
    return jsonify({"ok": True, "symbol": sym})


@app.route("/api/trade/sell", methods=["POST"])
def api_manual_sell():
    data = request.get_json() or {}
    sym  = data.get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"ok": False, "error": "Missing symbol"}), 400
    threading.Thread(target=engine.manual_sell, args=(sym,), daemon=True).start()
    return jsonify({"ok": True, "symbol": sym})


@app.route("/api/sim", methods=["POST"])
def api_sim():
    """Simulation mode removed. Paper book uses live Schwab data."""
    return jsonify({"ok": False, "error": "Simulation mode was removed. Paper trade uses live data and a $5,000 paper book."}), 410




@app.route("/api/swing", methods=["POST"])
def api_swing():
    """Toggle intraday swing scanner on/off."""
    data = request.get_json() or {}
    enable = bool(data.get("enable", True))
    try:
        engine.set_config(swing_scan_enabled=enable)
        return jsonify({
            "ok": True,
            "swing_enabled": enable,
            "message": "Swing scanner ON" if enable else "Swing scanner OFF",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
@app.route("/api/div", methods=["POST"])
def api_div():
    """Toggle Bollinger-band divergence strategy on/off."""
    data   = request.get_json() or {}
    enable = bool(data.get("enable", True))
    try:
        engine.set_config(use_div_strategy=enable)
        return jsonify({
            "ok": True,
            "div_strategy_enabled": enable,
            "message": "Divergence strategy ON" if enable else "Divergence strategy OFF",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/orb", methods=["POST"])
def api_orb():
    """Toggle ORB intraday breakout strategy on/off."""
    data   = request.get_json() or {}
    enable = bool(data.get("enable", True))
    try:
        engine.set_config(use_orb_strategy=enable)
        return jsonify({
            "ok": True,
            "orb_strategy_enabled": enable,
            "message": "ORB strategy ON" if enable else "ORB strategy OFF",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/scalp", methods=["POST"])
def api_scalp():
    """Configure and toggle scalp mode.  When enabled, all other strategies pause."""
    data = request.get_json() or {}
    try:
        enable = bool(data.get("enable", engine.scalp_enabled))
        kw = {"scalp_enabled": enable}
        for k in ["scalp_symbol","scalp_dollar_per_trade","scalp_session_budget",
                  "scalp_target_pct","scalp_stop_pct","scalp_rr_ratio",
                  "scalp_cooldown_sec","scalp_max_trades","scalp_breakout_bars",
                  "scalp_min_vol_mult","scalp_rsi_min","scalp_rsi_max"]:
            if k in data:
                kw[k] = data[k]
        engine.set_config(**kw)
        if enable:
            # Suspend all other strategies
            engine.set_config(use_div_strategy=False, use_first_pullback=False,
                              swing_scan_enabled=False, use_orb_strategy=False)
        return jsonify({"ok": True, "scalp_enabled": enable,
                        "message": "Scalp ON: " + engine.scalp_symbol if enable else "Scalp OFF"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/scalp/session")
def api_scalp_session():
    """Current scalp session stats (P&L, trades, budget remaining)."""
    return jsonify(engine.scalp_strategy.get_session_stats())

@app.route("/api/journal")
def api_journal():
    """Return cached journal data (or local bot trades if no cache)."""
    import datetime
    cached = _journal.load_cache()
    if cached and cached.get("trades"):
        trades = _journal.enrich_trades(_journal.merge_paper_into(cached["trades"]))
        _stamp_trade_times(trades)
        return jsonify({"ok": True, "source": "cache",
                        "updated": cached.get("updated"),
                        "updated_display": format_12h(cached.get("updated")),
                        "trades": trades,
                        "summary": _journal.compute_summary(trades)})
    trades = _journal.enrich_trades(_journal.merge_paper_into(_journal.load_all_local_trades()))
    _stamp_trade_times(trades)
    return jsonify({"ok": True, "source": "local",
                    "updated": datetime.datetime.utcnow().isoformat() + "Z",
                    "updated_display": format_12h(now_et()),
                    "trades": trades,
                    "summary": _journal.compute_summary(trades)})

@app.route("/api/journal/refresh")
def api_journal_refresh():
    """Fetch fresh data from Schwab, merge with local bot trades, update cache."""
    import datetime
    trades = []
    source = "local"
    if not engine.sim_mode and engine.is_authenticated():
        try:
            raw       = engine.client.get_transactions()
            fills     = _journal.parse_schwab_transactions(raw)
            schwab_tr = _journal.match_round_trips(fills)
            bot_ids   = _journal.get_bot_order_ids()
            schwab_tr = _journal.label_sources(schwab_tr, bot_ids)
            trades    = schwab_tr
            source    = "schwab"
        except Exception as e:
            from utils import log_message
            log_message(f"[JOURNAL] Schwab fetch error: {e}")
    local_trades = _journal.load_all_local_trades()
    if local_trades:
        existing_ids = {t.get("exit_order_id") for t in trades}
        for lt in local_trades:
            if lt.get("exit_order_id") not in existing_ids:
                trades.append(lt)
    if not trades and local_trades:
        trades = local_trades
        source = "local"
    trades = _journal.merge_paper_into(trades)
    trades = _journal.union_with_saved_cache(trades)
    trades.sort(key=lambda x: x.get("exit_time", ""), reverse=True)
    trades = _journal.enrich_trades(trades)
    _stamp_trade_times(trades)
    if trades:
        _journal.save_cache(trades)
    return jsonify({"ok": True, "source": source,
                    "updated": datetime.datetime.utcnow().isoformat() + "Z",
                    "updated_display": format_12h(now_et()),
                    "trades": trades,
                    "summary": _journal.compute_summary(trades)})

@app.route("/api/trade_log")
def api_trade_log():
    return jsonify(engine.data_handler.load_trade_log())


@app.route("/api/candidates")
def api_candidates():
    """Per-symbol criteria met/blocked for stocks the bot is watching."""
    return jsonify({"candidates": engine.get_candidates_report()})


@app.route("/api/news")
def api_news():
    """Recent news headlines for active symbols with sentiment scores."""
    import datetime as _dt
    symbols = list(dict.fromkeys(
        (engine._active_symbols or [])[:5] + (engine._div_symbols or [])[:3]
    ))[:8]
    ns = getattr(engine, "_news_sentiment", None)
    items = []
    for sym in symbols:
        sent = {}
        if ns and getattr(engine, "news_sentiment_enabled", False):
            try:
                sent = ns.get_sentiment(sym)
            except Exception:
                sent = {}
        for headline in (sent.get("headlines") or [])[:4]:
            ts = headline.get("published_at", 0)
            dt_str = (_dt.datetime.fromtimestamp(float(ts)).strftime("%b %d %I:%M %p")
                      if ts else "")
            scored = ns.score_headline(headline.get("title", "")) if ns else {}
            items.append({
                "symbol":  sym,
                "title":   headline.get("title", ""),
                "link":    headline.get("link", ""),
                "source":  headline.get("source", ""),
                "date":    dt_str,
                "score":   scored.get("score", sent.get("score", 0)),
                "blocked": bool(scored.get("blocked", sent.get("blocked", False))),
                "tags":    scored.get("tags", sent.get("tags", [])),
            })
        if not (sent.get("headlines") or []):
            items.append({
                "symbol": sym,
                "title": "(no recent headlines)",
                "link": "",
                "source": "",
                "date": "",
                "score": sent.get("score", 0),
                "blocked": bool(sent.get("blocked", False)),
                "tags": sent.get("tags", []),
            })
    return jsonify(items)


# ------------------------------------------------------------------
# SocketIO events
# ------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    log_message("[WS] Client connected.")
    # Send current state on connect
    emit("status", {"status": "running" if engine.trading_active else "stopped"})
    emit("scan_results", {"results": engine.get_scan_results()})


@socketio.on("disconnect")
def on_disconnect():
    log_message("[WS] Client disconnected.")


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------



@app.route("/api/backtest/start", methods=["POST"])
def api_backtest_start():
    """Launch an offline strategy backtest. Body JSON params:
       symbols   : list of symbols (default ["TSLA","NVDA","AAPL"])
       days      : number of trading days to replay (1-365, default 5)
       strategies: list of "orb","scalp","div","rsi","swing"
       capital   : account size in $ (defaults to live account_size)
       risk_pct  : % risk per trade (defaults to live risk_pct)

    Strategy quality filters, ORB/Div/Scalp knobs always come from the live
    engine config (same values as the strategy tabs / config.json).
    """
    if _backtest_engine.is_running():
        return jsonify({"ok": False, "error": "Backtest already running"}), 409
    params = dict(request.get_json() or {})
    try:
        live = engine._build_config_dict()
        # Never send secrets into BT report payload
        live.pop("app_key", None)
        live.pop("app_secret", None)
        params["live_config"] = live
        if not params.get("capital"):
            params["capital"] = float(live.get("account_size") or 10000)
        if params.get("risk_pct") is None:
            params["risk_pct"] = float(live.get("risk_pct") or 1.0)
    except Exception as e:
        log_message(f"[BT] live_config inject failed: {e}")
    _backtest_engine.start(params)
    return jsonify({
        "ok": True,
        "status": "running",
        "message": "Backtest started using live strategy-tab settings",
        "using_live_settings": True,
    })


@app.route("/api/backtest/status")
def api_backtest_status():
    """Poll backtest progress. Returns status, progress (0-1), message."""
    r = _backtest_engine.report
    return jsonify({
        "status":   r.status,
        "progress": round(r.progress, 3),
        "message":  r.message,
        "running":  _backtest_engine.is_running(),
    })


@app.route("/api/backtest/results")
def api_backtest_results():
    """Return complete backtest report once status == 'complete'."""
    r = _backtest_engine.report
    return jsonify(r.as_dict())

@app.route("/api/backtest/log")
def api_backtest_log():
    """Return the last N lines of the dedicated backtest log."""
    import os
    n = request.args.get("lines", 400)
    try:
        n = max(50, min(int(n), 5000))
    except Exception:
        n = 400
    path = os.path.join("saved_data", "backtest.log")
    if not os.path.isfile(path):
        return jsonify({
            "ok": True,
            "lines": [],
            "path": path,
            "message": "No backtest log yet — run a backtest first.",
        })
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = [ln.rstrip("\n") for ln in all_lines[-n:]]
        return jsonify({
            "ok": True,
            "path": path,
            "total_lines": len(all_lines),
            "returned": len(tail),
            "lines": tail,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500




@app.route("/api/strategies")
def api_strategies():
    return jsonify({"strategies": engine.get_strategy_status()})


@app.route("/api/strategy/<strategy_id>/toggle", methods=["POST"])
def api_strategy_toggle(strategy_id):
    data    = request.get_json() or {}
    enable  = bool(data.get("enable", False))
    sid = "opt_confirm" if strategy_id in ("options", "opt_confirm") else strategy_id
    ok      = engine._strategy_registry.set_enabled(sid, enable)
    if not ok:
        return jsonify({"ok": False, "error": f"Unknown strategy: {strategy_id}"}), 404
    return jsonify({
        "ok": True,
        "strategy_id": sid,
        "enabled": enable,
        "active_strategy": engine.active_strategy(),
        "paper_mode": True,
    })



@app.route("/api/strategy/rsi/candidates")
def api_rsi_candidates():
    data = engine.rsi_strategy.get_tab_data()
    return jsonify({
        "candidates":  data.get("candidates", []),
        "last_scan":   data.get("last_scan", 0),
        "open_count":  data.get("open_count", 0),
        "enabled":     engine.rsi_strategy.enabled,
    })


@app.route("/api/strategy/rsi/config", methods=["GET", "POST"])
def api_rsi_config():
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            kwargs = {}
            for key, cast in [
                ("rsi_entry_threshold",  float),
                ("rsi_partial_exit",     float),
                ("rsi_runner_exit",      float),
                ("rsi_initial_stop_pct", float),
                ("rsi_trail_stop_pct",   float),
                ("rsi_trail_trigger_pct",float),
                ("rsi_max_hold_days",    int),
                ("rsi_scan_interval_sec",float),
                ("rsi_min_avg_vol",      int),
                ("rsi_min_price",        float),
                ("rsi_max_price",        float),
                ("rsi_max_trades",       int),
                ("rsi_max_concurrent",   int),
            ]:
                if key in data:
                    kwargs[key] = cast(data[key])
            if kwargs:
                engine.set_config(**kwargs)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    cfg = engine.rsi_strategy.get_tab_data().get("config", {})
    cfg["enabled"] = engine.rsi_strategy.enabled
    return jsonify(cfg)


@app.route("/api/strategy/rsi/scan", methods=["POST"])
def api_rsi_scan():
    import threading as _t
    def _bg():
        try:
            engine._run_rsi_scan()
        except Exception as exc:
            pass
    _t.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "message": "RSI scan started."})


@app.route("/api/strategy/orb/config", methods=["GET", "POST"])
def api_orb_config():
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            kwargs = {}
            for key, cast in [
                ("orb_minutes",            int),
                ("entry_cutoff_hour",       int),
                ("rr_target",              float),
                ("require_vwap_above",     lambda v: bool(int(v))),
                ("confirm_close",          lambda v: bool(int(v))),
                ("require_volume_confirm", lambda v: bool(int(v))),
                ("min_breakout_rel_vol",   float),
                ("use_rsi_filter",         lambda v: bool(int(v))),
                ("rsi_overbought",         float),
                ("use_macd_filter",        lambda v: bool(int(v))),
                ("use_adx_filter",         lambda v: bool(int(v))),
                ("adx_min",                float),
                ("use_htf_filter",         lambda v: bool(int(v))),
                ("orb_min_range_pct",      float),
                ("orb_max_range_pct",      float),
                ("orb_max_chase_pct",      float),
                ("require_pullback",       lambda v: bool(int(v))),
                ("min_gap_pct",            float),
                ("min_rel_vol",            float),
                ("use_atr_stops",          lambda v: bool(int(v))),
                ("atr_stop_mult",          float),
                ("min_stop_dist",          float),
            ]:
                if key in data:
                    kwargs[key] = cast(data[key])
            if kwargs:
                engine.set_config(**kwargs)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({
        "orb_minutes":            engine.orb_minutes,
        "entry_cutoff_hour":      engine.entry_cutoff_hour,
        "rr_target":              engine.rr_target,
        "require_vwap_above":     engine.require_vwap_above,
        "confirm_close":          engine.confirm_close,
        "require_volume_confirm": engine.require_volume_confirm,
        "min_breakout_rel_vol":   engine.min_breakout_rel_vol,
        "use_rsi_filter":         engine.use_rsi_filter,
        "rsi_overbought":         engine.rsi_overbought,
        "use_macd_filter":        engine.use_macd_filter,
        "use_adx_filter":         engine.use_adx_filter,
        "adx_min":                engine.adx_min,
        "use_htf_filter":         engine.use_htf_filter,
        "orb_min_range_pct":      engine.orb_min_range_pct,
        "orb_max_range_pct":      engine.orb_max_range_pct,
        "orb_max_chase_pct":      engine.orb_max_chase_pct,
        "require_pullback":       engine.require_pullback,
        "min_gap_pct":            engine.min_gap_pct,
        "min_rel_vol":            engine.min_rel_vol,
        "use_atr_stops":          engine.use_atr_stops,
        "atr_stop_mult":          engine.atr_stop_mult,
        "min_stop_dist":          engine.min_stop_dist,
        "enabled":                getattr(engine, "use_orb_strategy", True),
    })


@app.route("/api/strategy/orb/levels")
def api_orb_levels():
    levels = {}
    for sym, lv in engine.strategy._orb_levels.items():
        entry = {"high": lv.get("high"), "low": lv.get("low")}
        entry["vwap"] = engine.strategy._vwap.get(sym)
        ind = engine.strategy._indicators.get(sym, {})
        entry["rsi"]  = ind.get("rsi")
        entry["ema9"] = ind.get("ema9")
        entry["ema20"]= ind.get("ema20")
        entry["triggered"] = bool(engine.strategy._triggered.get(sym))
        entry["open_pos"]  = sym in engine.strategy._open_positions
        packed = getattr(engine.strategy, "_last_pillars", {}).get(sym) or {}
        entry["ready"] = packed.get("ready")
        entry["passed"] = packed.get("passed")
        entry["total"] = packed.get("total")
        entry["score_pct"] = packed.get("score_pct")
        entry["pillars"] = packed.get("pillars") or []
        levels[sym] = entry
    return jsonify({"levels": levels, "active": engine._active_symbols})



@app.route("/api/strategy/divergence/config", methods=["GET", "POST"])
def api_div_config():
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            kwargs = {}
            for key, cast in [
                ("div_bb_period",       int),
                ("div_bb_std_mult",     float),
                ("div_lookback",        int),
                ("div_pivot_bars",      int),
                ("div_min_rsi_div",     float),
                ("div_max_rsi_entry",   float),
                ("div_band_proximity",  float),
                ("div_use_macd_confirm",lambda v: bool(int(v))),
                ("div_stop_buffer_pct", float),
                ("div_min_stop_pct",    float),
                ("div_max_stop_pct",    float),
                ("div_max_hold_days",   int),
                ("div_resample_minutes",int),
                ("div_lookback_days",   int),
                ("div_scan_interval_sec",float),
                ("div_scan_max",        int),
            ]:
                if key in data:
                    kwargs[key] = cast(data[key])
            if kwargs:
                engine.set_config(**kwargs)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({
        "enabled":              engine.use_div_strategy,
        "div_bb_period":        engine.div_bb_period,
        "div_bb_std_mult":      engine.div_bb_std_mult,
        "div_lookback":         engine.div_lookback,
        "div_pivot_bars":       engine.div_pivot_bars,
        "div_min_rsi_div":      engine.div_min_rsi_div,
        "div_max_rsi_entry":    engine.div_max_rsi_entry,
        "div_band_proximity":   engine.div_band_proximity,
        "div_use_macd_confirm": engine.div_use_macd_confirm,
        "div_stop_buffer_pct":  engine.div_stop_buffer_pct,
        "div_min_stop_pct":     engine.div_min_stop_pct,
        "div_max_stop_pct":     engine.div_max_stop_pct,
        "div_max_hold_days":    engine.div_max_hold_days,
        "div_resample_minutes": engine.div_resample_minutes,
        "div_lookback_days":    engine.div_lookback_days,
        "div_scan_interval_sec":engine._div_scan_interval,
        "div_scan_max":         engine.div_scan_max,
    })


@app.route("/api/strategy/divergence/scan", methods=["POST"])
def api_div_scan():
    import threading as _t
    def _bg():
        try: engine._run_div_scan()
        except Exception: pass
    _t.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "message": "Divergence scan started."})


@app.route("/api/strategy/swing/config", methods=["GET", "POST"])
def api_swing_config():
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            kwargs = {}
            for key, cast in [
                ("swing_max_retrace_pct",  float),
                ("swing_scan_interval_sec",float),
                ("swing_min_swings",       int),
                ("swing_min_swing_pct",    float),
                ("swing_min_avg_vol",      int),
            ]:
                if key in data:
                    kwargs[key] = cast(data[key])
            if kwargs:
                engine.set_config(**kwargs)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({
        "enabled":              engine.swing_scan_enabled,
        "swing_max_retrace_pct": engine.swing_max_retrace_pct,
        "swing_scan_interval_sec": engine._swing_scan_interval,
        "swing_min_swings":     engine.swing_min_swings,
        "swing_min_swing_pct":  engine.swing_min_swing_pct,
        "swing_min_avg_vol":    engine.swing_min_avg_vol,
    })


@app.route("/api/strategy/swing/scan", methods=["POST"])
def api_swing_scan():
    import threading as _t
    def _bg():
        try: engine._run_swing_scan()
        except Exception: pass
    _t.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "message": "Swing scan started."})



@app.route("/api/strategy/scalp/config", methods=["GET", "POST"])
def api_scalp_config():
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            kw = {}
            casts = {
                "scalp_symbol":           str,
                "scalp_dollar_per_trade": float,
                "scalp_session_budget":   float,
                "scalp_target_pct":       float,
                "scalp_stop_pct":         float,
                "scalp_rr_ratio":         float,
                "scalp_cooldown_sec":     int,
                "scalp_max_trades":       int,
                "scalp_breakout_bars":    int,
                "scalp_min_vol_mult":     float,
                "scalp_rsi_min":          float,
                "scalp_rsi_max":          float,
            }
            for k, cast in casts.items():
                if k in data:
                    kw[k] = cast(data[k])
            if kw:
                engine.set_config(**kw)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    stats = engine.scalp_strategy.get_session_stats()
    return jsonify({
        "enabled":              engine.scalp_enabled,
        "scalp_symbol":         engine.scalp_symbol,
        "scalp_dollar_per_trade": engine.scalp_dollar_per_trade,
        "scalp_session_budget": engine.scalp_session_budget,
        "scalp_target_pct":     engine.scalp_target_pct,
        "scalp_stop_pct":       engine.scalp_stop_pct,
        "scalp_rr_ratio":       engine.scalp_rr_ratio,
        "scalp_cooldown_sec":   engine.scalp_cooldown_sec,
        "scalp_max_trades":     engine.scalp_max_trades,
        "scalp_breakout_bars":  engine.scalp_breakout_bars,
        "scalp_min_vol_mult":   engine.scalp_min_vol_mult,
        "scalp_rsi_min":        engine.scalp_rsi_min,
        "scalp_rsi_max":        engine.scalp_rsi_max,
        **stats,
    })


@app.route("/api/strategy/scalp/scan", methods=["POST"])
def api_scalp_scan_now():
    import threading as _t
    def _bg():
        try: engine._run_scalp_scan()
        except Exception: pass
    _t.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "message": "Scalp scan started."})


# ─── Polymarket routes ───────────────────────────────────────────────────────

@app.route("/api/polymarket/start", methods=["POST"])
def api_pm_start():
    pm_monitor.start()
    return jsonify({"ok": True})

@app.route("/api/polymarket/stop", methods=["POST"])
def api_pm_stop():
    pm_monitor.stop()
    return jsonify({"ok": True})

@app.route("/api/polymarket/state")
def api_pm_state():
    return jsonify(pm_monitor.get_state())

@app.route("/api/polymarket/wallets")
def api_pm_wallets():
    return jsonify({"wallets": pm_monitor.get_wallets()})

@app.route("/api/polymarket/config", methods=["GET", "POST"])
def api_pm_config():
    if request.method == "POST":
        data = request.get_json() or {}
        casts = {
            "top_wallet_count":     int,
            "poll_interval_seconds":int,
            "min_trade_usd":        float,
            "daily_loss_limit_usd": float,
            "max_positions":        int,
            "dry_run":              bool,
        }
        updates = {}
        for k, cast in casts.items():
            if k in data:
                updates[k] = cast(data[k])
        if updates:
            pm_monitor.save_cfg(updates)
        return jsonify({"ok": True, "config": pm_monitor.get_cfg()})
    return jsonify(pm_monitor.get_cfg())


@app.route("/api/strategy/options/config", methods=["GET", "POST"])
def api_options_config():
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            kw = {}
            casts = {
                "use_options_confirm": bool,
                "options_enabled": bool,
                "options_dry_run": bool,
                "options_expiry_mode": str,
                "options_max_premium": float,
                "options_max_contracts": int,
                "options_max_concurrent": int,
                "options_strike": str,
                "options_eod_flat": bool,
                "options_last_entry_hhmm": int,
                "options_scan_interval_sec": float,
            }
            for k, cast in casts.items():
                if k in data:
                    kw[k] = cast(data[k])
            if "options_watchlist" in data:
                kw["options_watchlist"] = data["options_watchlist"]
            if kw:
                engine.set_config(**kw)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    tab = engine.options_strategy.get_tab_data()
    tab.update({
        "enabled": bool(engine.use_options_confirm),
        "options_dry_run": bool(engine.options_dry_run),
        "dry_run": bool(engine.options_dry_run or engine.dry_run),
        "equity_dry_run": bool(engine.dry_run),
        "auto_trade": bool(engine.auto_trade),
        "scan_interval_sec": engine.options_scan_interval_sec,
        "watchlist": engine.options_watchlist,
    })
    return jsonify(tab)


@app.route("/api/strategy/options/scan", methods=["POST"])
def api_options_scan():
    import threading as _t
    def _bg():
        try:
            engine._run_options_scan()
        except Exception as exc:
            from utils import log_message as _lm
            _lm(f"[OPT] scan error: {exc}")
    _t.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "message": "Options universe scan started."})


@app.route("/api/strategy/options/chain")
def api_options_chain():
    symbol = str(request.args.get("symbol") or "").strip().upper()
    expiry = str(request.args.get("expiry") or "").strip()[:10]
    rebuild = str(request.args.get("rebuild") or "") in ("1", "true", "yes")
    if not symbol:
        return jsonify({"ok": False, "error": "symbol is required", "expiries": [], "strikes": []}), 400
    try:
        data = engine.options_strategy.chain_ticket(symbol, expiry, rebuild=rebuild)
    except Exception as exc:
        from utils import log_message as _lm
        _lm(f"[OPT] chain ticket: {exc}")
        return jsonify({"ok": False, "error": "chain failed", "expiries": [], "strikes": []}), 500
    return jsonify(data)


@app.route("/api/strategy/options/candidates")
def api_options_candidates():
    tab = engine.options_strategy.get_tab_data()
    return jsonify({
        "candidates": tab.get("universe") or engine.get_options_results(),
        "cards": tab.get("cards") or [],
        "last_scan": tab.get("last_scan") or getattr(engine, "_last_options_scan", 0),
        "positions": tab,
        "open": tab.get("open") or [],
        "closed": tab.get("closed") or [],
        "expiry": tab.get("expiry"),
    })


@app.route("/api/sr")
def api_sr():
    sym = (request.args.get("symbol") or "").upper()
    if sym:
        return jsonify({"ok": True, "level": engine.sr_engine.get(sym)})
    return jsonify({"ok": True, "levels": dict(engine.sr_engine._cache)})


def run_server(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    log_message(f"[WEB] Starting dashboard at http://{host}:{port}")
    socketio.run(app, host=host, port=port, debug=debug, use_reloader=False, allow_unsafe_werkzeug=True)
