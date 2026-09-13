"""
scanner_proxy.py
Read-only Schwab market data proxy for the local stock scanner app.
Mounted at /api/scanner on the TOS bot Flask server.
"""

from __future__ import annotations

import os
import re
import threading
import time
from functools import wraps
from typing import Any, Callable

import requests
from flask import Blueprint, jsonify, request

from utils import log_message

scanner_bp = Blueprint("scanner_proxy", __name__, url_prefix="/api/scanner")

_market_block_until = 0.0
_market_block_logged = 0.0

SCANNER_API_KEY = os.environ.get("SCANNER_API_KEY", "")
SCHWAB_MARKET_URL = "https://api.schwabapi.com/marketdata/v1"


def _require_scanner_key(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not SCANNER_API_KEY:
            return jsonify({"error": "Scanner API is not configured on server."}), 503
        provided = request.headers.get("X-Scanner-Key", "")
        if provided != SCANNER_API_KEY:
            return jsonify({"error": "Invalid scanner API key."}), 401
        return view(*args, **kwargs)

    return wrapped


def _client():
    from web_dashboard import engine
    # Always use the real SchwabClient for API calls — engine.client may be
    # SimClient when sim mode is active, which has no _headers()/_ensure_token().
    return getattr(engine, "_schwab_client", engine.client)


def _market_get(path: str, params: dict | None = None) -> tuple[int, Any]:
    global _market_block_until, _market_block_logged
    client = _client()
    req = getattr(client, "request_market", None)
    if callable(req):
        status, data = req(path, params or {}, timeout=20)
        if status in (403, 429):
            _market_block_until = max(
                _market_block_until,
                float(getattr(client, "_api_block_until", 0) or 0),
            )
            if time.time() - _market_block_logged > 15:
                _market_block_logged = time.time()
                log_message(f"[SCANNER-PROXY] {path} {status} (shared backoff)")
        return status, data
    now = time.time()
    if now < _market_block_until:
        return 403, {"error": "Schwab market data is rate-limited. Retry shortly."}
    if not client._ensure_token():
        return 503, {"error": "Schwab is not authenticated on VPS."}
    headers = dict(client._headers())
    headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; DennTechTOSBot/1.0)")
    resp = requests.get(
        f"{SCHWAB_MARKET_URL}{path}",
        headers=headers,
        params=params or {},
        timeout=20,
    )
    if resp.status_code != 200:
        if resp.status_code in (403, 429):
            _market_block_until = time.time() + 45
            if time.time() - _market_block_logged > 15:
                _market_block_logged = time.time()
                log_message(
                    f"[SCANNER-PROXY] {path} {resp.status_code} "
                    f"{(resp.text or '')[:180]}"
                )
        else:
            log_message(f"[SCANNER-PROXY] {path} failed: {resp.status_code}")
        return resp.status_code, {"error": f"Schwab request failed ({resp.status_code})."}
    try:
        return 200, resp.json()
    except Exception:
        return 502, {"error": "Invalid JSON from Schwab."}


_streamer = None
_streamer_lock = threading.Lock()
_header_lock = threading.Lock()
_header_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_HEADER_TTL = 2.0


def _get_streamer():
    """Lazy-start the Schwab WebSocket streamer for L2 books (and L1 cache)."""
    global _streamer
    with _streamer_lock:
        if _streamer is None:
            from schwab_streamer import SchwabStreamer
            _streamer = SchwabStreamer(_client())
            _streamer.start()
        return _streamer


def _kick_header(symbol: str) -> dict[str, Any]:
    now = time.time()
    with _header_lock:
        hit = _header_cache.get(symbol)
        cached = dict(hit[1]) if hit else {}
        stale = not hit or (now - hit[0]) >= _HEADER_TTL

    def work() -> None:
        try:
            h = _book_header(symbol)
            if h:
                with _header_lock:
                    _header_cache[symbol] = (time.time(), h)
        except Exception as exc:
            log_message(f"[SCANNER-PROXY] book header quote failed for {symbol}: {exc}")

    if stale:
        threading.Thread(target=work, daemon=True, name=f"book-hdr-{symbol}").start()
    return cached


def _l1_overlay(streamer, symbol: str, header: dict[str, Any]) -> dict[str, Any]:
    out = dict(header or {})
    raw = streamer.get_quote(symbol) if streamer is not None else None
    q = (raw or {}).get("quote") or {}
    last = float(q.get("lastPrice") or 0) or 0.0
    bid = float(q.get("bidPrice") or 0) or 0.0
    ask = float(q.get("askPrice") or 0) or 0.0
    if last > 0:
        out["last"] = last
        out["after_last"] = last
        rth = float(out.get("rth_last") or 0)
        if rth > 0:
            out["after_change"] = round(last - rth, 4)
            out["after_pct"] = round((last - rth) / rth * 100.0, 2)
    if bid > 0:
        out["bid"] = bid
    if ask > 0:
        out["ask"] = ask
    return out


def _book_header(symbol: str) -> dict[str, Any]:
    status, data = _market_get(
        "/quotes",
        params={"symbols": symbol, "fields": "quote,extended,regular"},
    )
    if status != 200 or not isinstance(data, dict):
        return {}
    raw = data.get(symbol)
    if not isinstance(raw, dict) and data:
        raw = next(iter(data.values()), {})
    if not isinstance(raw, dict):
        return {}
    q = raw.get("quote") or {}
    ext = raw.get("extended") or {}
    reg = raw.get("regular") or {}
    last = float(q.get("lastPrice") or ext.get("lastPrice") or 0) or 0.0
    rth_last = float(reg.get("regularMarketLastPrice") or 0) or last
    prev = float(q.get("closePrice") or 0) or 0.0
    after_last = float(ext.get("lastPrice") or last or rth_last) or 0.0
    bid = float(q.get("bidPrice") or ext.get("bidPrice") or 0) or 0.0
    ask = float(q.get("askPrice") or ext.get("askPrice") or 0) or 0.0
    reg_chg = (rth_last - prev) if prev > 0 and rth_last > 0 else float(q.get("netChange") or 0) or 0.0
    reg_pct = (reg_chg / prev * 100.0) if prev > 0 else float(q.get("netPercentChange") or 0) or 0.0
    ah_chg = (after_last - rth_last) if rth_last > 0 and after_last > 0 else 0.0
    ah_pct = (ah_chg / rth_last * 100.0) if rth_last > 0 and after_last > 0 else 0.0
    spread_cents = int(round((ask - bid) * 100)) if bid > 0 and ask > 0 else 0
    return {
        "last": last,
        "bid": bid,
        "ask": ask,
        "bid_size": int(q.get("bidSize") or 0),
        "ask_size": int(q.get("askSize") or 0),
        "prev_close": prev,
        "rth_last": rth_last,
        "after_last": after_last,
        "reg_change": round(reg_chg, 4),
        "reg_pct": round(reg_pct, 2),
        "after_change": round(ah_chg, 4),
        "after_pct": round(ah_pct, 2),
        "spread_cents": spread_cents,
    }


@scanner_bp.route("/health")
@_require_scanner_key
def health():
    client = _client()
    authenticated = False
    try:
        authenticated = client._ensure_token()
    except Exception:
        authenticated = False
    streamer_ok = False
    book_symbols: list[str] = []
    try:
        if _streamer is not None:
            streamer_ok = bool(_streamer.is_connected)
            book_symbols = _streamer.book_symbols
    except Exception:
        pass
    return jsonify({
        "status": "ok",
        "authenticated": authenticated,
        "service": "tos-bot-scanner-proxy",
        "streamer": streamer_ok,
        "book_symbols": book_symbols,
    })


@scanner_bp.route("/book")
@_require_scanner_key
def book():
    """Per-market-maker Level 2 from Schwab NASDAQ_BOOK / NYSE_BOOK."""
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol or not symbol.replace(".", "").isalnum() or len(symbol) > 10:
        return jsonify({"ok": False, "error": "symbol is required"}), 400
    try:
        limit = int(request.args.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(8, min(limit, 200))
    try:
        streamer = _get_streamer()
    except Exception as exc:
        log_message(f"[SCANNER-PROXY] streamer start failed: {exc}")
        return jsonify({"ok": False, "error": "Schwab streamer failed to start.", "symbol": symbol}), 502

    streamer.subscribe_book([symbol])
    try:
        streamer.subscribe([symbol])
    except Exception:
        pass
    data = streamer.get_book(symbol, limit)
    header = _l1_overlay(streamer, symbol, _kick_header(symbol))
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    best_bid = float(bids[0]["price"]) if bids else float(header.get("bid") or 0)
    best_ask = float(asks[0]["price"]) if asks else float(header.get("ask") or 0)
    spread_cents = header.get("spread_cents") or 0
    if best_bid > 0 and best_ask > 0:
        spread_cents = int(round((best_ask - best_bid) * 100))
    payload = {
        "ok": True,
        "source": "schwab-book",
        "symbol": symbol,
        "connected": bool(streamer.is_connected),
        "services": data.get("services") or [],
        "book_time": data.get("book_time") or 0,
        "bids": bids,
        "asks": asks,
        "spread_cents": spread_cents,
        "ts": time.time(),
    }
    payload.update(header)
    if best_bid > 0:
        payload["bid"] = best_bid
    if best_ask > 0:
        payload["ask"] = best_ask
    if not streamer.is_connected:
        payload["note"] = "Schwab streamer connecting — book will fill once logged in."
    elif not bids and not asks:
        payload["note"] = "No L2 book yet (closed session, OTC, or entitlement)."
    return jsonify(payload)


@scanner_bp.route("/movers")
@_require_scanner_key
def movers():
    index = request.args.get("index", "$COMPX") or "$COMPX"
    direction = request.args.get("direction", "up")
    change = request.args.get("change", "percent")

    movers_out: list[dict[str, Any]] = []
    directions = ["up", "down"] if direction == "both" else [direction]
    for dir_ in directions:
        sort = "PERCENT_CHANGE_UP" if dir_ == "up" else "PERCENT_CHANGE_DOWN"
        if change == "volume":
            sort = "VOLUME"
        status, data = _market_get(
            f"/movers/{index}",
            params={"sort": sort, "frequency": 0},
        )
        if status != 200:
            return jsonify(data), status
        if isinstance(data, dict):
            movers_out.extend(data.get("screeners", []))

    return jsonify({"movers": movers_out})


@scanner_bp.route("/quotes")
@_require_scanner_key
def quotes():
    symbols_raw = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    if not symbols:
        return jsonify({"quotes": {}})

    combined: dict[str, Any] = {}
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i : i + chunk_size]
        status, data = _market_get(
            "/quotes",
            params={"symbols": ",".join(chunk), "fields": "quote,fundamental,extended,regular"},
        )
        if status != 200:
            return jsonify(data), status
        if isinstance(data, dict):
            combined.update(data)
    return jsonify({"quotes": combined})


@scanner_bp.route("/pricehistory")
@_require_scanner_key
def pricehistory():
    symbol = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    params = {
        "symbol": symbol,
        "periodType": request.args.get("periodType", "year"),
        "period": int(request.args.get("period", 1)),
        "frequencyType": request.args.get("frequencyType", "daily"),
        "frequency": int(request.args.get("frequency", 1)),
        "needExtendedHoursData": request.args.get("needExtendedHoursData", "false"),
    }
    status, data = _market_get("/pricehistory", params=params)
    if status != 200:
        return jsonify(data), status
    candles = data.get("candles", []) if isinstance(data, dict) else []
    return jsonify({"candles": candles})


@scanner_bp.route("/news")
@_require_scanner_key
def news():
    import datetime as _dt
    import json as _json
    import urllib.request

    symbol = request.args.get("symbol", "").upper()
    limit = int(request.args.get("limit", 10))
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    articles: list[dict[str, Any]] = []
    try:
        url = (
            "https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={symbol}&quotesCount=0&newsCount={limit}&enableFuzzyQuery=false"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        for item in data.get("news", [])[:limit]:
            ts = item.get("providerPublishTime", 0)
            articles.append({
                "headline": item.get("title", ""),
                "title": item.get("title", ""),
                "summary": item.get("summary", ""),
                "url": item.get("link", ""),
                "source": item.get("publisher", ""),
                "datetime": int(ts) if ts else 0,
                "date": (
                    _dt.datetime.fromtimestamp(float(ts)).strftime("%b %d %I:%M %p")
                    if ts else ""
                ),
            })
    except Exception as exc:
        log_message(f"[SCANNER-PROXY] news fallback failed for {symbol}: {exc}")
    return jsonify({"articles": articles})


# ---------------------------------------------------------------------------
# Click-ticket trading (limit-as-market for premarket / after-hours)
# ---------------------------------------------------------------------------

SCHWAB_TRADER_URL = "https://api.schwabapi.com/trader/v1"
_trader_http = requests.Session()
try:
    from requests.adapters import HTTPAdapter as _HTTPAdapter

    _trader_ad = _HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
    _trader_http.mount("https://", _trader_ad)
    _trader_http.mount("http://", _trader_ad)
except Exception:
    pass
CLICK_MAX_SHARES = int(os.environ.get("CLICK_MAX_SHARES", "5000"))
CLICK_MAX_NOTIONAL = float(os.environ.get("CLICK_MAX_NOTIONAL", "50000"))
_VALID_SESSIONS = {"NORMAL", "AM", "PM", "SEAMLESS", "OVERNIGHT"}
_VALID_INSTRUCTIONS = {"BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"}
_VALID_PRICE_MODES = {"touch", "last", "chase"}


def _trader_request(method: str, path: str, *, params=None, json_body=None) -> tuple[int, Any, dict]:
    client = _client()
    if not client._ensure_token():
        return 503, {"error": "Schwab is not authenticated on VPS."}, {}
    headers = dict(client._headers())
    headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; DennTechTOSBot/1.0)")
    method_u = str(method or "GET").upper()
    kwargs: dict[str, Any] = {
        "headers": headers,
        "params": params or {},
        "timeout": (1.2, 8.0) if method_u in ("POST", "DELETE") else 15,
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        kwargs["json"] = json_body
    resp = _trader_http.request(method, f"{SCHWAB_TRADER_URL}{path}", **kwargs)
    out_headers = dict(resp.headers)
    if resp.status_code in (204,):
        return resp.status_code, {}, out_headers
    try:
        return resp.status_code, resp.json(), out_headers
    except Exception:
        return resp.status_code, {"error": (resp.text or "")[:500]}, out_headers


def _order_reject_message(status: int, data: Any, kind: str = "order") -> str:
    """Surface the real Schwab/trader reason. Do not treat every 403 as a rate-limit."""
    blob = str(data)
    detail = ""
    if isinstance(data, dict):
        detail = str(data.get("error") or data.get("message") or "").strip()
        if not detail:
            errs = data.get("errors")
            if isinstance(errs, list) and errs:
                first = errs[0]
                if isinstance(first, dict):
                    detail = str(first.get("error") or first.get("message") or first)
                else:
                    detail = str(first)
            elif isinstance(errs, dict):
                detail = str(errs.get("error") or errs.get("message") or errs)
    low = f"{blob} {detail}".lower()
    if "paper-only" in low or "live broker orders are disabled" in low:
        return "Live broker orders are disabled on the VPS bot."
    if status == 429 or "too many requests" in low or "rate limit" in low:
        return "Schwab is rate-limiting the VPS. Wait about a minute and click Buy again."
    if detail:
        return f"Schwab rejected the {kind} ({status}): {detail}"
    if "access denied" in low:
        return f"Schwab rejected the {kind} ({status}): Access Denied."
    return f"Schwab rejected the {kind} ({status})."


def _round_equity_price(price: float) -> float:
    if price <= 0:
        return 0.0
    if price < 1:
        return round(price + 1e-12, 4)
    return round(price + 1e-12, 2)


def _extract_quote_prices(payload: dict) -> dict:
    q = payload.get("quote") or {}
    ext = payload.get("extended") or {}
    bid = float(q.get("bidPrice") or ext.get("bidPrice") or 0) or 0.0
    ask = float(q.get("askPrice") or ext.get("askPrice") or 0) or 0.0
    last = float(q.get("lastPrice") or ext.get("lastPrice") or q.get("mark") or 0) or 0.0
    return {
        "bid": bid,
        "ask": ask,
        "last": last,
        "mark": float(q.get("mark") or last or 0) or 0.0,
        "bid_size": int(q.get("bidSize") or 0),
        "ask_size": int(q.get("askSize") or 0),
        "volume": int(q.get("totalVolume") or 0),
        "description": (payload.get("reference") or {}).get("description") or "",
    }


def _compute_limit_price(
    instruction: str,
    prices: dict,
    price_mode: str = "touch",
    extra_cents: float = 0.0,
    buffer_pct: float = 0.0,
) -> float:
    is_buy = instruction.upper() in ("BUY", "BUY_TO_COVER")
    bid = float(prices.get("bid") or 0)
    ask = float(prices.get("ask") or 0)
    last = float(prices.get("last") or 0)
    if price_mode == "last":
        ref = last or (ask if is_buy else bid)
    elif is_buy:
        ref = ask or last
    else:
        ref = bid or last
    if ref <= 0:
        return 0.0
    extra = float(extra_cents or 0) / 100.0
    buf = float(buffer_pct or 0) / 100.0
    if is_buy:
        px = ref * (1.0 + buf) + extra
    else:
        px = ref * (1.0 - buf) - extra
    return _round_equity_price(max(px, 0.0001))


def _quote_for_symbol(symbol: str) -> tuple[int, dict]:
    status, data = _market_get(
        "/quotes",
        params={"symbols": symbol, "fields": "quote,extended,regular,reference"},
    )
    if status != 200:
        return status, data if isinstance(data, dict) else {"error": "quote failed"}
    raw = data.get(symbol) if isinstance(data, dict) else None
    if not raw:
        # Schwab sometimes keys quotes with a slightly different symbol
        if isinstance(data, dict) and data:
            raw = next(iter(data.values()))
        else:
            return 404, {"error": f"No quote for {symbol}"}
    prices = _extract_quote_prices(raw if isinstance(raw, dict) else {})
    prices["symbol"] = symbol
    return 200, prices


@scanner_bp.route("/account")
@_require_scanner_key
def click_account():
    client = _client()
    try:
        bal = client.get_balance() or {}
        positions = []
        acct = client.get_account_hash()
        raw_acct = None
        if acct:
            status, data, _ = _trader_request(
                "GET", f"/accounts/{acct}", params={"fields": "positions"}
            )
            if status == 200 and isinstance(data, dict):
                raw_acct = (data.get("securitiesAccount") or {})
        if raw_acct is not None:
            for p in raw_acct.get("positions") or []:
                inst = p.get("instrument") or {}
                long_qty = float(p.get("longQuantity") or 0)
                short_qty = float(p.get("shortQuantity") or 0)
                qty = long_qty - short_qty
                if qty == 0:
                    continue
                avg = (
                    float(p.get("taxLotAverageLongPrice") or 0)
                    or float(p.get("averageLongPrice") or 0)
                    or float(p.get("averagePrice") or 0)
                    or float(p.get("averageShortPrice") or 0)
                )
                mv = float(p.get("marketValue") or 0)
                open_pl = float(p.get("longOpenProfitLoss") or 0) + float(
                    p.get("shortOpenProfitLoss") or 0
                )
                if abs(open_pl) < 1e-9 and avg and qty:
                    open_pl = mv - avg * qty
                positions.append({
                    "symbol": inst.get("symbol", ""),
                    "asset_type": inst.get("assetType") or inst.get("type") or "EQUITY",
                    "description": inst.get("description") or "",
                    "underlying": inst.get("underlyingSymbol") or "",
                    "quantity": qty,
                    "long_quantity": long_qty,
                    "short_quantity": short_qty,
                    "avg_price": avg,
                    "market_value": mv,
                    "open_pl": open_pl,
                    "day_pl": float(p.get("currentDayProfitLoss") or 0),
                    "has_broker_pl": True,
                })
            bal_raw = raw_acct.get("currentBalances") or {}
            cash = (
                float(bal_raw.get("cashAvailableForTrading") or 0)
                or float(bal_raw.get("availableFunds") or 0)
                or float(bal.get("cash_available") or 0)
            )
            total = (
                float(bal_raw.get("liquidationValue") or 0)
                or float(bal_raw.get("equity") or 0)
                or float(bal.get("total_value") or 0)
            )
        else:
            for p in client.get_positions() or []:
                qty = float(p.get("quantity") or 0)
                if qty == 0:
                    continue
                mv = float(p.get("market_value") or 0)
                avg = float(p.get("avg_price") or 0)
                positions.append({
                    "symbol": p.get("symbol", ""),
                    "asset_type": p.get("asset_type") or p.get("assetType") or "EQUITY",
                    "description": p.get("description") or "",
                    "underlying": p.get("underlying") or "",
                    "quantity": qty,
                    "avg_price": avg,
                    "market_value": mv,
                    "open_pl": (mv - avg * qty) if avg else 0.0,
                    "day_pl": 0.0,
                    "has_broker_pl": False,
                })
            cash = float(bal.get("cash_available") or 0)
            total = float(bal.get("total_value") or 0)
        return jsonify({
            "cash_available": cash,
            "buying_power": cash,
            "total_value": total,
            "positions": positions,
        })
    except Exception as exc:
        log_message(f"[CLICK] account error: {exc}")
        return jsonify({"error": "Failed to load account."}), 502


@scanner_bp.route("/orders")
@_require_scanner_key
def click_orders():
    client = _client()
    try:
        data = client.get_orders() or []
    except Exception as exc:
        log_message(f"[CLICK] orders error: {exc}")
        return jsonify({"orders": [], "error": "Failed to load orders."}), 502
    rows = []
    for o in data if isinstance(data, list) else []:
        legs = o.get("orderLegCollection") or []
        leg = legs[0] if legs else {}
        inst = leg.get("instrument") or {}
        rows.append({
            "order_id": str(o.get("orderId") or ""),
            "status": o.get("status"),
            "symbol": inst.get("symbol", ""),
            "instruction": leg.get("instruction"),
            "quantity": float(leg.get("quantity") or o.get("quantity") or 0),
            "filled": float(o.get("filledQuantity") or 0),
            "price": float(o.get("price") or 0),
            "order_type": o.get("orderType"),
            "session": o.get("session"),
            "duration": o.get("duration"),
            "entered": o.get("enteredTime"),
        })
    return jsonify({"orders": rows})


@scanner_bp.route("/preview", methods=["POST"])
@_require_scanner_key
def click_preview():
    body = request.get_json(silent=True) or {}
    built, err, code = _build_click_order(body)
    if err:
        return jsonify(err), code
    built["dry_run"] = True
    return jsonify(built)


@scanner_bp.route("/place", methods=["POST"])
@_require_scanner_key
def click_place():
    body = request.get_json(silent=True) or {}
    built, err, code = _build_click_order(body)
    if err:
        return jsonify(err), code
    if body.get("dry_run"):
        built["dry_run"] = True
        built["ok"] = True
        return jsonify(built)

    client = _client()
    acct = client.get_account_hash()
    if not acct:
        return jsonify({"error": "Could not resolve Schwab account."}), 503
    order = {
        "orderType": built["order_type"],
        "session": built["session"],
        "duration": built["duration"],
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": built["instruction"],
            "quantity": built["quantity"],
            "instrument": {"symbol": built["symbol"], "assetType": "EQUITY"},
        }],
    }
    if built["order_type"] != "MARKET":
        order["price"] = built["limit_price"]
    status, data, headers = _trader_request(
        "POST", f"/accounts/{acct}/orders", json_body=order
    )
    if status not in (200, 201):
        log_message(f"[CLICK] place failed {status}: {data}")
        return jsonify({
            "ok": False,
            "error": _order_reject_message(status, data, "order"),
            "detail": data,
            **built,
        }), 400
    loc = headers.get("Location") or headers.get("location") or ""
    order_id = loc.rstrip("/").split("/")[-1] if loc else ""
    log_message(
        f"[CLICK] {built['order_type']} {built['instruction']} {built['quantity']} "
        f"{built['symbol']} @ {built['limit_price']} ({built['session']}) ID={order_id}"
    )
    built["ok"] = True
    built["order_id"] = order_id
    return jsonify(built), 201


@scanner_bp.route("/cancel", methods=["POST"])
@_require_scanner_key
def click_cancel():
    body = request.get_json(silent=True) or {}
    order_id = str(body.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    client = _client()
    acct = client.get_account_hash()
    if not acct:
        return jsonify({"error": "Could not resolve Schwab account."}), 503
    status, data, _ = _trader_request(
        "DELETE", f"/accounts/{acct}/orders/{order_id}"
    )
    if status not in (200, 201, 204):
        return jsonify({"ok": False, "error": "Cancel failed.", "detail": data}), status
    log_message(f"[CLICK] cancelled order {order_id}")
    return jsonify({"ok": True, "order_id": order_id})


def _build_click_order(body: dict) -> tuple[dict | None, dict | None, int]:
    symbol = str(body.get("symbol") or "").strip().upper()
    instruction = str(body.get("instruction") or "").strip().upper()
    session = str(body.get("session") or "SEAMLESS").strip().upper()
    duration = str(body.get("duration") or "DAY").strip().upper()
    price_mode = str(body.get("price_mode") or "touch").strip().lower()
    if not symbol or not symbol.replace(".", "").isalnum() or len(symbol) > 10:
        return None, {"error": "Invalid symbol."}, 400
    if instruction not in _VALID_INSTRUCTIONS:
        return None, {"error": "instruction must be BUY, SELL, SELL_SHORT, or BUY_TO_COVER."}, 400
    if session not in _VALID_SESSIONS:
        return None, {"error": f"session must be one of {sorted(_VALID_SESSIONS)}."}, 400
    if price_mode not in _VALID_PRICE_MODES:
        price_mode = "touch"
    if duration not in {"DAY", "GTC", "FILL_OR_KILL", "IMMEDIATE_OR_CANCEL"}:
        duration = "DAY"

    try:
        quantity = int(body.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        return None, {"error": "quantity must be a positive integer."}, 400
    if quantity > CLICK_MAX_SHARES:
        return None, {"error": f"quantity exceeds max {CLICK_MAX_SHARES} shares."}, 400

    extra_cents = float(body.get("extra_cents") or 0)
    buffer_pct = float(body.get("buffer_pct") or 0)
    try:
        client_px = float(body.get("limit_price") or 0)
    except (TypeError, ValueError):
        client_px = 0.0

    # Ticket already has a live quote — skip a second Schwab fetch so the
    # click can go straight to place_order.
    prices: dict = body.get("quote") if isinstance(body.get("quote"), dict) else {}
    if client_px > 0:
        limit_price = _round_equity_price(client_px)
    else:
        q_status, quoted = _quote_for_symbol(symbol)
        if q_status != 200:
            return None, quoted if isinstance(quoted, dict) else {"error": "Quote failed."}, q_status
        prices = quoted
        limit_price = _compute_limit_price(
            instruction, prices, price_mode, extra_cents, buffer_pct
        )
    if limit_price <= 0:
        return None, {"error": f"No usable bid/ask/last for {symbol}."}, 400

    notional = limit_price * quantity
    if notional > CLICK_MAX_NOTIONAL:
        return None, {
            "error": f"Order value ${notional:,.2f} exceeds max ${CLICK_MAX_NOTIONAL:,.0f}."
        }, 400

    order_type = "MARKET" if str(body.get("order_type") or "LIMIT").upper() == "MARKET" else "LIMIT"
    if order_type == "MARKET":
        session = "NORMAL"
    return {
        "symbol": symbol,
        "instruction": instruction,
        "quantity": quantity,
        "limit_price": limit_price,
        "session": session,
        "duration": duration,
        "price_mode": price_mode,
        "extra_cents": extra_cents,
        "buffer_pct": buffer_pct,
        "notional": round(notional, 2),
        "quote": prices,
        "order_type": order_type,
    }, None, 200


# ---------------------------------------------------------------------------
# UOA option ticket — single-leg BUY_TO_OPEN / SELL_TO_CLOSE
# ---------------------------------------------------------------------------
_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
CLICK_MAX_CONTRACTS = int(os.environ.get("CLICK_MAX_CONTRACTS", "20"))
CLICK_MAX_OPTION_NOTIONAL = float(os.environ.get("CLICK_MAX_OPTION_NOTIONAL", "15000"))
_VALID_OPTION_INSTRUCTIONS = {"BUY_TO_OPEN", "SELL_TO_CLOSE"}


def _occ_norm(occ: str) -> str:
    return str(occ or "").upper().replace(" ", "")


def _occ_to_osi(occ: str) -> str:
    s = _occ_norm(occ)
    m = _OCC_RE.match(s)
    if m:
        root, ymd, cp, strike = m.groups()
        return f"{root:<6}{ymd}{cp}{strike}"
    raw = str(occ or "").upper()
    if len(raw) >= 15:
        return raw
    return s


def _round_option_price(price: float) -> float:
    if price <= 0:
        return 0.0
    return round(float(price) + 1e-12, 2)


def _quote_option(osi: str) -> tuple[int, dict]:
    status, data = _market_get(
        "/quotes",
        params={"symbols": osi, "fields": "quote,extended,reference"},
    )
    if status != 200:
        return status, data if isinstance(data, dict) else {"error": "option quote failed"}
    raw = None
    if isinstance(data, dict):
        raw = data.get(osi) or data.get(osi.replace(" ", ""))
        if raw is None:
            for k, v in data.items():
                if str(k).replace(" ", "").upper() == osi.replace(" ", "").upper():
                    raw = v
                    break
        if raw is None and data:
            raw = next(iter(data.values()))
    if not raw or not isinstance(raw, dict):
        return 404, {"error": f"No option quote for {osi}"}
    prices = _extract_quote_prices(raw)
    prices["symbol"] = osi
    return 200, prices


@scanner_bp.route("/option-quote")
@_require_scanner_key
def option_quote():
    symbol = str(request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    osi = _occ_to_osi(symbol)
    status, data = _quote_option(osi)
    if status != 200:
        return jsonify(data if isinstance(data, dict) else {"error": "quote failed"}), status
    return jsonify(data)


@scanner_bp.route("/option-place", methods=["POST"])
@_require_scanner_key
def option_place():
    body = request.get_json(silent=True) or {}
    occ = str(body.get("symbol") or body.get("occ") or "").strip().upper()
    instruction = str(body.get("instruction") or "").strip().upper()
    if instruction not in _VALID_OPTION_INSTRUCTIONS:
        return jsonify({"error": "instruction must be BUY_TO_OPEN or SELL_TO_CLOSE."}), 400
    try:
        quantity = int(body.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        return jsonify({"error": "quantity must be a positive integer."}), 400
    if quantity > CLICK_MAX_CONTRACTS:
        return jsonify({"error": f"quantity exceeds max {CLICK_MAX_CONTRACTS} contracts."}), 400

    osi = _occ_to_osi(occ)
    compact = _occ_norm(osi)
    if not _OCC_RE.match(compact):
        return jsonify({"error": "Invalid option symbol."}), 400

    try:
        client_px = float(body.get("limit_price") or 0)
    except (TypeError, ValueError):
        client_px = 0.0

    # Desktop already quoted + confirmed. Skip a second Schwab /quotes call.
    prices: dict = body.get("quote") if isinstance(body.get("quote"), dict) else {}
    is_buy = instruction == "BUY_TO_OPEN"
    if client_px > 0:
        limit_price = _round_option_price(client_px)
    else:
        q_status, quoted = _quote_option(osi)
        if q_status != 200:
            msg = (quoted or {}).get("error") if isinstance(quoted, dict) else "Quote failed."
            code = 400 if q_status in (403, 429) else q_status
            return jsonify({"error": f"Could not quote {osi}: {msg}"}), code
        prices = quoted if isinstance(quoted, dict) else {}
        ask = float(prices.get("ask") or 0)
        bid = float(prices.get("bid") or 0)
        last = float(prices.get("last") or prices.get("mark") or 0)
        if is_buy:
            ref = ask or last
            limit_price = _round_option_price(ref * 1.02 if ref else 0)
        else:
            ref = bid or last
            limit_price = _round_option_price(ref * 0.98 if ref else 0)
    if limit_price <= 0:
        return jsonify({"error": f"No usable bid/ask/last for {osi}."}), 400

    notional = limit_price * 100.0 * quantity
    if notional > CLICK_MAX_OPTION_NOTIONAL:
        return jsonify({
            "error": f"Order value ${notional:,.2f} exceeds max ${CLICK_MAX_OPTION_NOTIONAL:,.0f}."
        }), 400

    client = _client()
    acct = client.get_account_hash()
    if not acct:
        return jsonify({"error": "Could not resolve Schwab account."}), 503
    order = {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "price": limit_price,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": instruction,
            "quantity": quantity,
            "instrument": {"symbol": osi, "assetType": "OPTION"},
        }],
    }
    status, data, headers = _trader_request(
        "POST", f"/accounts/{acct}/orders", json_body=order
    )
    if status not in (200, 201):
        log_message(f"[UOA-OPT] place failed {status}: {data}")
        err = _order_reject_message(status, data, "option order")
        return jsonify({
            "ok": False,
            "error": err,
            "detail": data,
            "symbol": osi,
            "instruction": instruction,
            "quantity": quantity,
            "limit_price": limit_price,
        }), 400
    loc = headers.get("Location") or headers.get("location") or ""
    order_id = loc.rstrip("/").split("/")[-1] if loc else ""
    log_message(
        f"[UOA-OPT] LIMIT {instruction} {quantity} {osi} @ {limit_price} ID={order_id}"
    )
    return jsonify({
        "ok": True,
        "order_id": order_id,
        "symbol": osi,
        "instruction": instruction,
        "quantity": quantity,
        "limit_price": limit_price,
        "notional": round(notional, 2),
        "quote": prices,
        "order_type": "LIMIT",
        "session": "NORMAL",
    }), 201


# ---------------------------------------------------------------------------
# Trade journal / calendar for the desktop app (same payloads as web_dashboard)
# ---------------------------------------------------------------------------

def _stamp_trade_times(trades):
    from utils import format_12h
    for t in trades or []:
        t["entry_time_display"] = format_12h(t.get("entry_time"))
        t["exit_time_display"] = format_12h(t.get("exit_time"))
    return trades


def _journal_payload(refresh: bool = False) -> tuple[dict, int]:
    """Build the /api/journal JSON. Refresh hits Schwab on this process."""
    import datetime
    import journal as _journal
    from utils import format_12h, now_et

    if not refresh:
        cached = _journal.load_cache()
        if cached and cached.get("trades"):
            trades = _journal.enrich_trades(_journal.merge_paper_into(cached["trades"]))
            _stamp_trade_times(trades)
            return {
                "ok": True,
                "source": "cache",
                "updated": cached.get("updated"),
                "updated_display": format_12h(cached.get("updated")),
                "trades": trades,
                "summary": _journal.compute_summary(trades),
            }, 200
        trades = _journal.enrich_trades(_journal.merge_paper_into(_journal.load_all_local_trades()))
        _stamp_trade_times(trades)
        return {
            "ok": True,
            "source": "local",
            "updated": datetime.datetime.utcnow().isoformat() + "Z",
            "updated_display": format_12h(now_et()),
            "trades": trades,
            "summary": _journal.compute_summary(trades),
        }, 200

    trades = []
    source = "local"
    from web_dashboard import engine
    if not engine.sim_mode and engine.is_authenticated():
        try:
            raw = engine.client.get_transactions()
            fills = _journal.parse_schwab_transactions(raw)
            schwab_tr = _journal.match_round_trips(fills)
            bot_ids = _journal.get_bot_order_ids()
            schwab_tr = _journal.label_sources(schwab_tr, bot_ids)
            trades = schwab_tr
            source = "schwab"
        except Exception as e:
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
    return {
        "ok": True,
        "source": source,
        "updated": datetime.datetime.utcnow().isoformat() + "Z",
        "updated_display": format_12h(now_et()),
        "trades": trades,
        "summary": _journal.compute_summary(trades),
    }, 200


@scanner_bp.route("/journal")
@_require_scanner_key
def scanner_journal():
    payload, status = _journal_payload(refresh=False)
    return jsonify(payload), status


@scanner_bp.route("/journal/refresh", methods=["GET", "POST"])
@_require_scanner_key
def scanner_journal_refresh():
    payload, status = _journal_payload(refresh=True)
    return jsonify(payload), status


@scanner_bp.route("/calendar")
@_require_scanner_key
def scanner_calendar():
    import journal as _journal
    months = request.args.get("months", 24)
    try:
        months = max(1, min(int(months), 24))
    except Exception:
        months = 24
    trades = _journal.load_journal_trades()
    payload = _journal.build_calendar(trades, months=months)
    payload["ok"] = True
    return jsonify(payload)