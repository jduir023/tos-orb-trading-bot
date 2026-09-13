"""
schwab_streamer.py
Schwab WebSocket Streamer — Level 1 equity quotes and Level 2 books.

Connects to the Schwab Streamer API, subscribes to LEVELONE_EQUITIES plus
NASDAQ_BOOK / NYSE_BOOK, and caches quotes and per-market-maker depth.

Falls back gracefully: if the connection is unavailable the engine uses
REST polling as normal.
"""

import json
import threading
import time
from typing import Any, Dict, List, Optional, Set

import websocket  # websocket-client

from utils import log_message


# LEVELONE_EQUITIES fields we care about
_FIELDS = "0,1,2,3,4,5,8,9,28"

# NASDAQ_BOOK / NYSE_BOOK: symbol, BookTime, Bids, Asks
_BOOK_FIELDS = "0,1,2,3"

_BOOK_SERVICES = ("NASDAQ_BOOK", "NYSE_BOOK")

# Map field index → SchwabClient quote key
_FIELD_MAP: Dict[int, str] = {
    1:  "bidPrice",
    2:  "askPrice",
    3:  "lastPrice",
    4:  "bidSize",
    5:  "askSize",
    8:  "totalVolume",
    9:  "lastSize",
    28: "openPrice",
}


def _num(obj: dict, *keys: Any) -> float:
    for k in keys:
        v = obj.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _pick(obj: dict, *keys: Any) -> Any:
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


class SchwabStreamer:
    """
    Real-time Schwab Streamer WebSocket client.

    Usage::
        streamer = SchwabStreamer(schwab_client)
        streamer.start()                      # connects in background thread
        streamer.subscribe(["AAPL", "TSLA"])  # subscribe at any time
        quotes = streamer.get_quotes(symbols) # same format as client.get_quotes()
        streamer.subscribe_book(["AAPL"])
        book = streamer.get_book("AAPL")
        streamer.stop()
    """

    def __init__(self, client) -> None:  # client: SchwabClient
        self.client = client
        self._lock             = threading.Lock()
        # Cache mirrors SchwabClient.get_quotes() format:
        #   { "AAPL": {"quote": {"lastPrice": 182.0, "bidPrice": ..., ...}}, ... }
        self._quote_cache: Dict[str, Dict] = {}
        self._subscribed:  Set[str]        = set()
        self._pending_sub: List[str]       = []   # subscribed before login confirmed

        # Level 2: symbol -> service -> {bids, asks, book_time, ts}
        self._book_cache: Dict[str, Dict[str, dict]] = {}
        self._book_subscribed: Set[str] = set()
        self._pending_book: List[str] = []

        self._ws:       Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread]       = None
        self._running   = False
        self._connected = False
        self._logged_in = False
        self._req_id    = 0
        self._streamer_url:  Optional[str] = None
        self._streamer_info:  dict = {}
        self._reconnect_delay = 15   # seconds; doubles on each failure (cap 120)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn background thread that connects and maintains the stream."""
        if self._running:
            return
        self._running = True
        self._ws_thread = threading.Thread(
            target=self._connect_loop, daemon=True, name="schwab_streamer"
        )
        self._ws_thread.start()
        log_message("[STREAMER] Streamer started.")

    def stop(self) -> None:
        """Disconnect cleanly and stop reconnect loop."""
        self._running   = False
        self._connected = False
        self._logged_in = False
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        log_message("[STREAMER] Streamer stopped.")

    def subscribe(self, symbols: List[str]) -> None:
        """
        Subscribe to L1 quotes for symbols.
        Can be called before or after the stream is connected — pending
        subscriptions are flushed immediately after login.
        """
        new = [s.upper() for s in symbols if s.upper() not in self._subscribed]
        if not new:
            return
        if self._logged_in:
            self._send_subscribe(new)
        else:
            with self._lock:
                for s in new:
                    if s not in self._pending_sub:
                        self._pending_sub.append(s)

    def unsubscribe(self, symbols: List[str]) -> None:
        """Remove symbols from the active subscription."""
        keys = [s.upper() for s in symbols if s.upper() in self._subscribed]
        if not keys or not self._logged_in:
            return
        self._send_requests([{
            "service":   "LEVELONE_EQUITIES",
            "command":   "UNSUBS",
            "parameters": {"keys": ",".join(keys)},
        }])
        for k in keys:
            self._subscribed.discard(k)

    def subscribe_book(self, symbols: List[str]) -> bool:
        """
        Subscribe to NASDAQ_BOOK + NYSE_BOOK for symbols.

        SUBS replaces the previous book keys (desktop L2 is one symbol).
        Returns True if a new subscribe was sent (or queued).
        """
        keys = [s.upper() for s in symbols if s and s.strip()]
        if not keys:
            return False
        with self._lock:
            same = set(keys) == self._book_subscribed and bool(self._book_subscribed)
        if same and self._logged_in:
            return False
        with self._lock:
            self._book_subscribed = set(keys)
        if self._logged_in:
            self._send_book_subscribe(keys)
        else:
            with self._lock:
                self._pending_book = list(keys)
        return True

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        Return streamed quotes for requested symbols in the same format as
        SchwabClient.get_quotes().  Only symbols already in the cache are
        returned — caller should REST-fill any gaps.
        """
        upper = [s.upper() for s in symbols]
        with self._lock:
            return {s: self._quote_cache[s] for s in upper if s in self._quote_cache}

    def get_quote(self, symbol: str) -> Optional[Dict]:
        with self._lock:
            return self._quote_cache.get(symbol.upper())

    def get_book(self, symbol: str, limit: int = 200) -> dict:
        """Merged per-MM book for symbol (NASDAQ_BOOK ∪ NYSE_BOOK)."""
        symbol = (symbol or "").upper()
        with self._lock:
            by_svc = dict(self._book_cache.get(symbol) or {})
        bids: list[dict] = []
        asks: list[dict] = []
        book_time = 0
        services: list[str] = []
        seen_bid: set[tuple] = set()
        seen_ask: set[tuple] = set()
        for svc, payload in by_svc.items():
            services.append(svc)
            book_time = max(book_time, int(payload.get("book_time") or 0))
            for row in payload.get("bids") or []:
                key = (row.get("mmid") or "", round(float(row.get("price") or 0), 4), int(row.get("size") or 0))
                if key in seen_bid:
                    continue
                seen_bid.add(key)
                bids.append({**row, "service": svc})
            for row in payload.get("asks") or []:
                key = (row.get("mmid") or "", round(float(row.get("price") or 0), 4), int(row.get("size") or 0))
                if key in seen_ask:
                    continue
                seen_ask.add(key)
                asks.append({**row, "service": svc})
        bids.sort(key=lambda r: (-float(r.get("price") or 0), -int(r.get("size") or 0)))
        asks.sort(key=lambda r: (float(r.get("price") or 0), -int(r.get("size") or 0)))
        cap = max(8, min(int(limit or 200), 200))
        return {
            "symbol": symbol,
            "bids": bids[:cap],
            "asks": asks[:cap],
            "book_time": book_time,
            "services": services,
            "connected": self.is_connected,
        }

    @property
    def is_connected(self) -> bool:
        return self._connected and self._logged_in

    @property
    def book_symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._book_subscribed)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _connect_loop(self) -> None:
        delay = self._reconnect_delay
        while self._running:
            try:
                info = self.client.get_streamer_info()
                if not info:
                    log_message("[STREAMER] Could not get streamer info — retrying in 30s.")
                    time.sleep(30)
                    continue
                self._streamer_url  = info.get("streamerSocketUrl", "")
                self._streamer_info = info
                if not self._streamer_url:
                    log_message("[STREAMER] Empty streamer URL — retrying in 60s.")
                    time.sleep(60)
                    continue
                log_message(f"[STREAMER] Connecting to {self._streamer_url}")
                delay = self._reconnect_delay  # reset on success
                self._run_ws()
            except Exception as e:
                log_message(f"[STREAMER] Connection error: {e}")
            if self._running:
                delay = min(delay * 2, 120)
                log_message(f"[STREAMER] Reconnecting in {delay}s…")
                time.sleep(delay)

    def _run_ws(self) -> None:
        self._connected = False
        self._logged_in = False
        self._req_id    = 0
        self._ws = websocket.WebSocketApp(
            self._streamer_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        # run_forever blocks until the socket closes
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    # ------------------------------------------------------------------
    # WebSocket callbacks
    # ------------------------------------------------------------------

    def _on_open(self, ws) -> None:
        self._connected = True
        log_message("[STREAMER] WebSocket open — sending LOGIN.")
        self._send_login()

    def _on_close(self, ws, code, msg) -> None:
        self._connected = False
        self._logged_in = False
        log_message(f"[STREAMER] WebSocket closed (code={code}, msg={msg}).")

    def _on_error(self, ws, error) -> None:
        log_message(f"[STREAMER] WebSocket error: {error}")

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return

        # --- LOGIN / SUBS responses ---
        if "response" in msg:
            for resp in msg.get("response", []):
                service = str(resp.get("service") or "")
                command = str(resp.get("command") or "")
                content = resp.get("content") or {}
                raw_code = content.get("code")
                try:
                    code = int(raw_code) if raw_code is not None else -1
                except (TypeError, ValueError):
                    code = -1
                if service == "ADMIN" and command == "LOGIN":
                    if code == 0:
                        self._logged_in = True
                        log_message("[STREAMER] Logged in to Schwab Streamer.")
                        self._flush_pending()
                    else:
                        msg_txt = content.get("msg", "")
                        log_message(f"[STREAMER] Login failed (code={code}): {msg_txt}")
                elif service in _BOOK_SERVICES:
                    log_message(
                        f"[STREAMER] {service} {command} code={code} {content.get('msg', '')}"
                    )

        if "data" in msg:
            self._handle_stream_items(msg.get("data") or [])
        if "snapshot" in msg:
            self._handle_stream_items(msg.get("snapshot") or [])

    def _flush_pending(self) -> None:
        with self._lock:
            pending = list(self._pending_sub)
            self._pending_sub.clear()
            already = list(self._subscribed)
            pending_book = list(self._pending_book)
            self._pending_book.clear()
            already_book = list(self._book_subscribed)
        l1 = sorted(set(pending + already))
        if l1:
            self._send_subscribe(l1)
        books = sorted(set(pending_book + already_book))
        if books:
            self._send_book_subscribe(books)

    def _handle_stream_items(self, items: List[dict]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            svc = item.get("service")
            content = item.get("content") or []
            if svc == "LEVELONE_EQUITIES":
                self._handle_l1(content)
            elif svc in _BOOK_SERVICES:
                self._handle_book(str(svc), content)

    def _handle_l1(self, content: List[Dict]) -> None:
        """Merge delta updates into the quote cache."""
        with self._lock:
            for entry in content:
                symbol = entry.get("key", "").upper()
                if not symbol:
                    continue
                existing = self._quote_cache.get(symbol, {}).get("quote", {}).copy()
                for field_id, field_name in _FIELD_MAP.items():
                    # Schwab sends field IDs as both int and string keys
                    val = entry.get(field_id, entry.get(str(field_id)))
                    if val is not None:
                        try:
                            existing[field_name] = float(val)
                        except (TypeError, ValueError):
                            existing[field_name] = val
                self._quote_cache[symbol] = {"quote": existing}

    def _handle_book(self, service: str, content: List[Dict]) -> None:
        for entry in content:
            if not isinstance(entry, dict):
                continue
            symbol = str(entry.get("key") or _pick(entry, 0, "0", "SYMBOL") or "").upper()
            if not symbol:
                continue
            book_time = int(_num(entry, 1, "1", "BOOK_TIME"))
            bids = _parse_book_side(_pick(entry, 2, "2", "BIDS") or [])
            asks = _parse_book_side(_pick(entry, 3, "3", "ASKS") or [])
            with self._lock:
                slot = self._book_cache.setdefault(symbol, {})
                first = service not in slot
                slot[service] = {
                    "bids": bids,
                    "asks": asks,
                    "book_time": book_time,
                    "ts": time.time(),
                }
            if first:
                log_message(
                    f"[STREAMER] {service} {symbol} bids={len(bids)} asks={len(asks)}"
                )

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        self._req_id += 1
        return str(self._req_id)

    def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws and self._connected:
            try:
                ws.send(json.dumps(payload))
            except Exception as e:
                log_message(f"[STREAMER] Send error: {e}")

    def _send_requests(self, requests: List[dict]) -> None:
        info = self._streamer_info or {}
        enveloped = []
        for req in requests:
            enveloped.append({
                "service": req["service"],
                "command": req["command"],
                "requestid": self._next_id(),
                "SchwabClientCustomerId": info.get("schwabClientCustomerId", ""),
                "SchwabClientCorrelId": info.get("schwabClientCorrelId", ""),
                "parameters": req.get("parameters") or {},
            })
        self._send({"requests": enveloped})

    def _send_login(self) -> None:
        token   = getattr(self.client, "_access_token", None) or ""
        info    = self._streamer_info
        cust_id = info.get("schwabClientCustomerId", "")
        corr_id = info.get("schwabClientCorrelId", "")
        channel = info.get("schwabClientChannel", "")
        func_id = info.get("schwabClientFunctionId", "")
        self._send({
            "requests": [{
                "service":                 "ADMIN",
                "command":                 "LOGIN",
                "requestid":               self._next_id(),
                "SchwabClientCustomerId":  cust_id,
                "SchwabClientCorrelId":    corr_id,
                "parameters": {
                    "Authorization":           token,
                    "SchwabClientChannel":     channel,
                    "SchwabClientFunctionId":  func_id,
                },
            }]
        })

    def _send_subscribe(self, symbols: List[str]) -> None:
        keys = ",".join(s.upper() for s in symbols)
        self._send_requests([{
            "service":   "LEVELONE_EQUITIES",
            "command":   "SUBS",
            "parameters": {
                "keys":   keys,
                "fields": _FIELDS,
            },
        }])
        for s in symbols:
            self._subscribed.add(s.upper())
        log_message(f"[STREAMER] Subscribed L1: {symbols}")

    def _send_book_subscribe(self, symbols: List[str]) -> None:
        keys = ",".join(s.upper() for s in symbols)
        self._send_requests([
            {
                "service": svc,
                "command": "SUBS",
                "parameters": {"keys": keys, "fields": _BOOK_FIELDS},
            }
            for svc in _BOOK_SERVICES
        ])
        log_message(f"[STREAMER] Subscribed NASDAQ_BOOK+NYSE_BOOK: {symbols}")


def _parse_book_side(levels: Any) -> List[dict]:
    rows: List[dict] = []
    if not isinstance(levels, list):
        return rows
    for lvl in levels:
        if not isinstance(lvl, dict):
            continue
        price = _num(lvl, 0, "0", "BID_PRICE", "ASK_PRICE", "PRICE")
        agg = int(_num(lvl, 1, "1", "TOTAL_VOLUME", "SIZE"))
        mm_list = _pick(lvl, 3, "3", "BIDS", "ASKS") or []
        if not isinstance(mm_list, list) or not mm_list:
            if price > 0 and agg > 0:
                rows.append({"price": price, "size": agg, "mmid": ""})
            continue
        for mm in mm_list:
            if not isinstance(mm, dict):
                continue
            mmid = str(
                _pick(mm, 0, "0", "EXCHANGE", "MMID", "MARKET_MAKER") or ""
            ).strip().upper()
            sz = int(_num(mm, 1, "1", "BID_VOLUME", "ASK_VOLUME", "SIZE", "VOLUME"))
            if price > 0 and sz > 0:
                rows.append({"price": price, "size": sz, "mmid": mmid[:8]})
    return rows
