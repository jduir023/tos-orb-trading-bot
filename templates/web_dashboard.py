"""
web_dashboard.py
Flask + SocketIO web dashboard for the TOS ORB Bot.
Runs on VPS — access from any browser at http://your-vps-ip:5000
"""

import threading
from flask import Flask, jsonify, make_response, render_template, request
from flask_socketio import SocketIO, emit

from trading_engine import TradingEngine
from utils import log_message

app = Flask(__name__)
import os as _os
app.config["SECRET_KEY"] = _os.environ.get("TOS_SECRET_KEY", "tosbot-secret-change-me")
app.config["TEMPLATES_AUTO_RELOAD"] = True
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# When running behind a reverse proxy at a subpath, set SOCKET_PATH env var
# e.g. SOCKET_PATH=/tos-trading-2026/socket.io
_SOCKET_PATH  = _os.environ.get("SOCKET_PATH", "/socket.io")
_BASE_PATH    = _os.environ.get("BOT_BASE_PATH", "")

engine = TradingEngine()


# ------------------------------------------------------------------
# Wire engine events → SocketIO broadcasts
# ------------------------------------------------------------------

def _setup_engine_events():
    engine.on("log",          lambda m: socketio.emit("log",          {"msg": str(m)}))
    engine.on("balance",      lambda d: socketio.emit("balance",      d or {}))
    engine.on("positions",    lambda d: socketio.emit("positions",    d or {}))
    engine.on("scan_results", lambda r: socketio.emit("scan_results", {"results": r or []}))
    engine.on("swing_results",lambda r: socketio.emit("swing_results",{"results": r or []}))
    engine.on("trade_placed", lambda d: socketio.emit("trade_placed", d or {}))
    engine.on("trade_upgraded",  lambda d: socketio.emit("trade_upgraded",  d or {}))
    engine.on("day_pnl",         lambda v: socketio.emit("day_pnl",         {"pnl": float(v or 0)}))
    engine.on("status",       lambda s: socketio.emit("status",       {"status": str(s)}))
    engine.on("sim_mode",     lambda v: socketio.emit("sim_mode",     {"enabled": bool(v)}))
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
    return jsonify({
        "trading_active":           engine.trading_active,
        "authenticated":            engine.is_authenticated(),
        "dry_run":                  engine.dry_run,
        "extended_hours":           engine.extended_hours,
        "swing_enabled":            engine.swing_scan_enabled,
        "trades_today":             engine.get_trades_today(),
        "day_pnl":                  engine.get_day_pnl(),
        "active_symbols":           engine._active_symbols,
        "open_positions":           len(engine.get_open_positions()),
        "pending_positions":        len(engine.get_pending_positions()),
        "max_concurrent":           engine.max_concurrent,
        "session":                  engine.get_session_status(),
        "sim_mode":                 getattr(engine, "sim_mode", False),
    })


@app.route("/api/balance")
def api_balance():
    return jsonify(engine.get_balance())


@app.route("/api/positions")
def api_positions():
    return jsonify({
        "pending": [vars(p) for p in engine.get_pending_positions()],
        "held":    [vars(p) for p in engine.get_open_positions()],
        "open":    [vars(p) for p in engine.get_open_positions()],
        "closed":  [vars(p) for p in engine.get_closed_positions()],
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
                account_size=float(data.get("account_size", engine.account_size)),
                risk_pct=float(data.get("risk_pct", engine.risk_pct)),
                orb_minutes=int(data.get("orb_minutes", engine.orb_minutes)),
                max_trades=int(data.get("max_trades", engine.max_trades)),
                max_concurrent=int(data.get("max_concurrent", engine.max_concurrent)),
                rr_target=float(data.get("rr_target", engine.rr_target)),
                entry_cutoff_hour=int(data.get("entry_cutoff_hour", engine.entry_cutoff_hour)),
                require_vwap_above=bool(data.get("require_vwap_above", engine.require_vwap_above)),
                auto_trade=bool(data.get("auto_trade", engine.auto_trade)),
                dry_run=bool(data.get("dry_run", engine.dry_run)),
                extended_hours=bool(data.get("extended_hours", engine.extended_hours)),
                swing_scan_enabled=bool(data.get("swing_scan_enabled", engine.swing_scan_enabled)),
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
                use_first_pullback=bool(data.get("use_first_pullback", engine.use_first_pullback)),
                fp_time_limit_hhmm=int(data.get("fp_time_limit_hhmm", engine.fp_time_limit_hhmm)),
                fp_max_stop_cents=float(data.get("fp_max_stop_cents", engine.fp_max_stop_cents)),
                fp_min_pole_pct=float(data.get("fp_min_pole_pct", engine.fp_min_pole_pct)),
            )
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(engine.data_handler.load_config())


@app.route("/api/auth", methods=["POST"])
def api_auth():
    data = request.get_json() or {}
    key    = data.get("app_key", "").strip()
    secret = data.get("app_secret", "").strip()
    if not key or not secret:
        return jsonify({"ok": False, "error": "Missing keys"}), 400
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
    if not engine.client.app_key:
        return jsonify({"error": "App key not set. Save your keys first."}), 400
    params = {
        "response_type": "code",
        "client_id":     engine.client.app_key,
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
    result = engine.client._exchange_code(code)
    return jsonify({"ok": result})


@app.route("/api/start", methods=["POST"])
def api_start():
    if not engine.is_authenticated() and not engine.dry_run and not engine.sim_mode:
        return jsonify({"ok": False, "error": "Not authenticated. Enable Dry Run or Simulation Mode first."}), 401
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
    """Toggle simulation mode on/off."""
    import json as _json
    raw = request.get_data(cache=True)
    try:
        data = _json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    enable = bool(data.get("enable", True))
    try:
        if enable:
            engine.enable_simulation()
            return jsonify({"ok": True, "sim_mode": True, "message": "Simulation mode ON"})
        else:
            engine.disable_simulation()
            return jsonify({"ok": True, "sim_mode": False, "message": "Simulation mode OFF"})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/trade_log")
def api_trade_log():
    return jsonify(engine.data_handler.load_trade_log())


@app.route("/api/analytics")
def api_analytics():
    """Performance analytics: overall + by setup / hour / gap bucket."""
    try:
        return jsonify(engine.get_analytics())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news")
def api_news():
    """Recent news headlines for the active scanned symbols via Yahoo Finance."""
    import urllib.request
    import json
    import datetime as _dt
    symbols = engine._active_symbols[:5]
    items = []
    for sym in symbols:
        try:
            url = (f"https://query1.finance.yahoo.com/v1/finance/search"
                   f"?q={sym}&quotesCount=0&newsCount=4&enableFuzzyQuery=false")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
            for n in data.get("news", [])[:4]:
                ts = n.get("providerPublishTime", 0)
                dt_str = (_dt.datetime.fromtimestamp(float(ts)).strftime("%b %d %I:%M %p")
                          if ts else "")
                items.append({
                    "symbol":  sym,
                    "title":   n.get("title", ""),
                    "link":    n.get("link", ""),
                    "source":  n.get("publisher", ""),
                    "date":    dt_str,
                })
        except Exception:
            pass
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

def run_server(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    log_message(f"[WEB] Starting dashboard at http://{host}:{port}")
    socketio.run(app, host=host, port=port, debug=debug, use_reloader=False, allow_unsafe_werkzeug=True)
