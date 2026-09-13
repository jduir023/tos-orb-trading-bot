"""
schwab_client.py
Schwab API client — OAuth 2.0, REST quotes, order placement, account data.
Mirrors exchange_handler.py from the crypto bot.
"""

import base64
import json
import os
import re
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

import requests

from utils import log_message

SCHWAB_AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
SCHWAB_BASE_URL  = "https://api.schwabapi.com/trader/v1"
SCHWAB_MARKET_URL = "https://api.schwabapi.com/marketdata/v1"
# Live order POST is forbidden. Quotes / chains / history still use the real account keys.
PAPER_LOCK = True
# Schwab market-data ceiling is ~120 req/min. Stay under it and cache the heavy calls.
_MARKET_MIN_GAP = 0.55          # ~110/min serialized
_MARKET_BACKOFF_SEC = 120.0
_QUOTE_TTL = 8.0
_OPT_QUOTE_TTL = 3.0
_CHAIN_TTL = 180.0
_HIST_TTL = 25.0
REDIRECT_URI         = "https://cryptotradebot.info/tos-trading-2026/"
# Local port for the OAuth callback listener (must be free while authorizing)
OAUTH_CALLBACK_PORT  = int(os.environ.get("OAUTH_CALLBACK_PORT", "8080"))


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler to capture the OAuth redirect code."""
    auth_code: Optional[str] = None

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _OAuthCallbackHandler.auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Authorization complete. You may close this tab.</h2>")

    def log_message(self, *args):
        pass  # Silence HTTP server logs


class SchwabClient:
    """
    Handles Schwab OAuth token lifecycle, quotes, account info, and order placement.
    Thread-safe — all public methods acquire _lock.
    """

    def __init__(self, app_key: str, app_secret: str, data_dir: str = "saved_data") -> None:
        self.app_key    = app_key
        self.app_secret = app_secret
        self.data_dir   = data_dir
        self._token_file = os.path.join(data_dir, "schwab_tokens.json")
        self._lock         = threading.Lock()
        self._refresh_lock = threading.Lock()  # serialise concurrent token refresh
        self._acct_lock    = threading.Lock()  # serialise account hash fetch

        self._access_token:  Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._token_expiry:  float = 0.0
        self._account_hash:  Optional[str] = None
        self._api_block_until: float = 0.0
        self._rate_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._last_market_call = 0.0
        self._market_cache: Dict[str, tuple] = {}
        self.paper_lock = True  # quotes/chains live; order POST blocked

        self._load_tokens()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authorize(self) -> bool:
        """Full OAuth flow: opens browser, listens for redirect, exchanges code."""
        params = {
            "response_type": "code",
            "client_id": self.app_key,
            "redirect_uri": REDIRECT_URI,
            "scope": "readonly trading",
        }
        url = SCHWAB_AUTH_URL + "?" + urllib.parse.urlencode(params)
        log_message(f"[AUTH] Opening browser for authorization: {url}")
        webbrowser.open(url)

        # Spin up a temporary local server to catch the redirect
        _OAuthCallbackHandler.auth_code = None
        server = HTTPServer(("127.0.0.1", OAUTH_CALLBACK_PORT), _OAuthCallbackHandler)
        server.timeout = 120
        server.handle_request()

        code = _OAuthCallbackHandler.auth_code
        if not code:
            log_message("[AUTH] No authorization code received.")
            return False

        return self._exchange_code(code)

    def _exchange_code(self, code: str):
        """Exchange auth code for tokens. Returns True on success or a dict with error details."""
        # Strip full redirect URL down to just the code value
        if "?" in code or "code=" in code:
            import urllib.parse as _up
            qs = code.split("?", 1)[-1] if "?" in code else code
            code = _up.parse_qs(qs).get("code", [code])[0]
        creds = base64.b64encode(f"{self.app_key}:{self.app_secret}".encode()).decode()
        resp = requests.post(
            SCHWAB_TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            try:
                err = resp.json()
            except Exception:
                err = {"error": str(resp.status_code), "error_description": resp.text}
            log_message(f"[AUTH] Token exchange failed: {resp.status_code} {resp.text}")
            return err
        return self._store_tokens(resp.json())

    def _refresh_access_token(self) -> bool:
        if not self._refresh_token:
            return False
        creds = base64.b64encode(f"{self.app_key}:{self.app_secret}".encode()).decode()
        resp = requests.post(
            SCHWAB_TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log_message(f"[AUTH] Token refresh failed: {resp.status_code}")
            return False
        return self._store_tokens(resp.json())

    def _store_tokens(self, data: dict) -> bool:
        with self._lock:
            self._access_token  = data.get("access_token")
            self._refresh_token = data.get("refresh_token", self._refresh_token)
            expires_in          = int(data.get("expires_in", 1800))
            self._token_expiry  = time.time() + expires_in - 60
            self._save_tokens()
        log_message("[AUTH] Tokens stored successfully.")
        return True

    def _ensure_token(self) -> bool:
        # Fast path — no lock needed for read-only check on CPython
        if self._access_token and time.time() < self._token_expiry:
            return True
        # Serialize refresh so only one thread refreshes at a time
        with self._refresh_lock:
            if self._access_token and time.time() < self._token_expiry:
                return True  # another thread already refreshed
            return self._refresh_access_token()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; DennTechTOSBot/1.0)",
        }

    # ------------------------------------------------------------------
    # Token persistence
    # ------------------------------------------------------------------

    def _save_tokens(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self._token_file, "w") as f:
            payload = {
                "access_token":  self._access_token,
                "refresh_token": self._refresh_token,
                "expiry":        self._token_expiry,
            }
            if self._account_hash:
                payload["account_hash"] = self._account_hash
            json.dump(payload, f)

    def _load_tokens(self) -> None:
        if not os.path.exists(self._token_file):
            return
        try:
            with open(self._token_file) as f:
                data = json.load(f)
            self._access_token  = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._token_expiry  = float(data.get("expiry", 0))
            if data.get("account_hash"):
                self._account_hash = str(data.get("account_hash"))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def _trip_api_block(self, status: int) -> None:
        if status in (403, 429):
            self._api_block_until = time.time() + _MARKET_BACKOFF_SEC
            log_message(f"[SCHWAB] backing off {int(_MARKET_BACKOFF_SEC)}s after {status}")

    def market_blocked(self) -> bool:
        return time.time() < float(self._api_block_until or 0)

    def _cache_get(self, key: str, ttl: float):
        with self._cache_lock:
            hit = self._market_cache.get(key)
        if not hit:
            return None
        ts, val = hit
        if time.time() - ts <= ttl:
            return val
        return None

    def _cache_peek(self, key: str):
        with self._cache_lock:
            hit = self._market_cache.get(key)
        return None if not hit else hit[1]

    def _cache_put(self, key: str, val: Any) -> None:
        with self._cache_lock:
            self._market_cache[key] = (time.time(), val)

    def _pace_market(self) -> bool:
        """Serialize market-data calls. False = currently backing off a 403/429."""
        if self.market_blocked():
            return False
        with self._rate_lock:
            if self.market_blocked():
                return False
            wait = _MARKET_MIN_GAP - (time.time() - self._last_market_call)
            if wait > 0:
                time.sleep(min(wait, _MARKET_MIN_GAP))
            self._last_market_call = time.time()
            return True

    def request_market(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 20,
    ) -> tuple:
        """GET marketdata. Returns (status, json_or_error). Honors throttle + backoff."""
        if not self._pace_market():
            return 429, {"error": "Schwab market data is rate-limited. Retry shortly."}
        if not self._ensure_token():
            return 503, {"error": "Schwab is not authenticated on VPS."}
        try:
            resp = requests.get(
                f"{SCHWAB_MARKET_URL}{path}",
                headers=self._headers(),
                params=params or {},
                timeout=timeout,
            )
        except Exception as exc:
            log_message(f"[SCHWAB] {path} error: {exc}")
            return 502, {"error": "Schwab request failed."}
        if resp.status_code in (403, 429):
            self._trip_api_block(resp.status_code)
            log_message(f"[SCHWAB] {path} {resp.status_code} — pause market data")
            return resp.status_code, {"error": f"Schwab request failed ({resp.status_code})."}
        if resp.status_code != 200:
            return resp.status_code, {"error": f"Schwab request failed ({resp.status_code})."}
        try:
            return 200, resp.json()
        except Exception:
            return 502, {"error": "Invalid JSON from Schwab."}

    def get_account_hash(self) -> Optional[str]:
        with self._acct_lock:
            if self._account_hash:
                return self._account_hash
        if time.time() < self._api_block_until:
            return None
        if not self._ensure_token():
            return None
        resp = requests.get(
            f"{SCHWAB_BASE_URL}/accounts/accountNumbers",
            headers=self._headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            self._trip_api_block(resp.status_code)
            log_message(f"[ACCT] accountNumbers failed: {resp.status_code}")
            return None
        data = resp.json()
        with self._acct_lock:
            if data:
                self._account_hash = data[0].get("hashValue")
            return self._account_hash

    def get_balance(self) -> Dict[str, float]:
        """Returns {'cash_available': x, 'total_value': x}

        Schwab field names differ by account type:
          Cash account   -> currentBalances.cashAvailableForTrading / liquidationValue
          Margin account -> currentBalances.availableFunds / equity
        We try both so the bot works with either account type.
        """
        if time.time() < self._api_block_until:
            return {}
        if not self._ensure_token():
            return {}
        acct = self.get_account_hash()
        if not acct:
            return {}
        resp = requests.get(
            f"{SCHWAB_BASE_URL}/accounts/{acct}",
            headers=self._headers(),
            params={"fields": "positions"},
            timeout=10,
        )
        if resp.status_code != 200:
            self._trip_api_block(resp.status_code)
            log_message(f"[ACCT] balance fetch failed: {resp.status_code}")
            return {}
        try:
            acct_data = resp.json()["securitiesAccount"]
            bal = acct_data.get("currentBalances", {})
            # Try cash-account fields first, fall back to margin-account fields
            cash = (float(bal.get("cashAvailableForTrading") or 0) or
                    float(bal.get("availableFunds") or 0) or
                    float(bal.get("buyingPower") or 0))
            total = (float(bal.get("liquidationValue") or 0) or
                     float(bal.get("equity") or 0))
            return {"cash_available": cash, "total_value": total}
        except Exception as e:
            log_message(f"[ACCT] balance parse error: {e}")
            return {}

    def get_positions(self) -> List[Dict]:
        if not self._ensure_token():
            return []
        acct = self.get_account_hash()
        if not acct:
            return []
        resp = requests.get(
            f"{SCHWAB_BASE_URL}/accounts/{acct}",
            headers=self._headers(),
            params={"fields": "positions"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        try:
            raw = resp.json()["securitiesAccount"].get("positions", [])
            return [
                {
                    "symbol":       p["instrument"]["symbol"],
                    "quantity":     float(p["longQuantity"]),
                    "avg_price":    float(p["averagePrice"]),
                    "market_value": float(p["marketValue"]),
                }
                for p in raw
            ]
        except Exception as e:
            log_message(f"[ACCT] positions parse error: {e}")
            return []

    def get_streamer_info(self) -> Optional[Dict]:
        """Return streamer connection info from /userPreference.
        Used by SchwabStreamer to get the WebSocket URL and auth tokens.
        Returns dict with streamerSocketUrl, schwabClientCustomerId, etc., or None.
        """
        if not self._ensure_token():
            return None
        resp = requests.get(
            f"{SCHWAB_BASE_URL}/userPreference",
            headers=self._headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            log_message(f"[STREAMER] userPreference failed: {resp.status_code}")
            return None
        try:
            data = resp.json()
            streamer_list = data.get("streamerInfo", [])
            if not streamer_list:
                log_message("[STREAMER] No streamerInfo in response.")
                return None
            return streamer_list[0]  # first account's streamer info
        except Exception as e:
            log_message(f"[STREAMER] userPreference parse error: {e}")
            return None

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Optional[Dict]:
        batch = self.get_quotes([symbol]) if symbol else {}
        return batch.get(symbol) or None

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        cleaned = [str(s).upper().strip() for s in (symbols or []) if s]
        cleaned = list(dict.fromkeys(cleaned))
        if not cleaned:
            return {}
        key = "q:" + ",".join(cleaned)
        hit = self._cache_get(key, _QUOTE_TTL)
        if isinstance(hit, dict):
            return hit
        status, data = self.request_market(
            "/quotes",
            {"symbols": ",".join(cleaned), "fields": "quote,fundamental"},
            timeout=15,
        )
        if status != 200 or not isinstance(data, dict):
            stale = self._cache_peek(key)
            if isinstance(stale, dict):
                return stale
            if status not in (429, 403):
                log_message(f"[QUOTES] batch quote failed: {status}")
            return {}
        self._cache_put(key, data)
        return data

    def get_price_history(
        self,
        symbol: str,
        period_type: str = "day",
        period: int = 1,
        frequency_type: str = "minute",
        frequency: int = 1,
        extended_hours: bool = False,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> List[Dict]:
        ft = (frequency_type or "minute").lower()
        if ft in ("min", "minute", "minutes"):
            ft = "minute"
            inferred_period = "day"
        elif ft in ("daily", "day"):
            ft = "daily"
            inferred_period = "month"
        elif ft in ("weekly", "week"):
            ft = "weekly"
            inferred_period = "year"
        else:
            inferred_period = period_type or "day"
        params: Dict[str, Any] = {
            "symbol":        str(symbol or "").upper(),
            "frequencyType": ft,
            "frequency":     frequency,
            "periodType":    period_type or inferred_period,
            "needExtendedHoursData": str(bool(extended_hours)).lower(),
        }
        if start_ms is not None and end_ms is not None:
            # Date range + periodType (Schwab 400s if periodType is omitted)
            params["startDate"] = int(start_ms)
            params["endDate"]   = int(end_ms)
            params["periodType"] = inferred_period
        else:
            params["period"] = period
        # Cache by bucket, not exact end_ms — otherwise every poll is a miss.
        end_bucket = int((end_ms or 0) // 15000) if end_ms else 0
        key = f"h:{params['symbol']}:{ft}:{params.get('startDate')}:{end_bucket}:{extended_hours}"
        hit = self._cache_get(key, _HIST_TTL)
        if isinstance(hit, list):
            return hit
        status, data = self.request_market("/pricehistory", params, timeout=15)
        if status != 200:
            stale = self._cache_peek(key)
            if isinstance(stale, list):
                return stale
            if status not in (429, 403):
                log_message(f"[HIST] pricehistory failed for {symbol}: {status}")
            return []
        candles = (data or {}).get("candles", []) if isinstance(data, dict) else []
        if not isinstance(candles, list):
            candles = []
        self._cache_put(key, candles)
        return candles

    def get_option_chain(
        self,
        symbol: str,
        contract_type: str = "ALL",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        strike_count: Optional[int] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Full Schwab option chain. Dates are YYYY-MM-DD. Cached 3 minutes."""
        sym = str(symbol or "").upper()
        key = f"c:{sym}:{contract_type}:{from_date}:{to_date}:{strike_count}"
        if not force:
            hit = self._cache_get(key, _CHAIN_TTL)
            if isinstance(hit, dict) and hit:
                return hit
        params: Dict[str, Any] = {
            "symbol": sym,
            "contractType": contract_type,
            "includeUnderlyingQuote": "true",
            "optionType": "S",
        }
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        if strike_count:
            params["strikeCount"] = int(strike_count)
        status, data = self.request_market("/chains", params, timeout=20)
        if status != 200 or not isinstance(data, dict) or not data:
            stale = self._cache_peek(key)
            if isinstance(stale, dict) and stale:
                return stale
            if status not in (429, 403):
                log_message(f"[OPT] chain {sym} failed: {status}")
            return {}
        self._cache_put(key, data)
        return data

    def get_option_quote(self, osi: str) -> Optional[Dict[str, Any]]:
        if not osi:
            return None
        key = f"oq:{str(osi).upper().replace(' ', '')}"
        hit = self._cache_get(key, _OPT_QUOTE_TTL)
        if isinstance(hit, dict) and hit:
            return hit
        status, data = self.request_market("/quotes", {"symbols": osi}, timeout=10)
        if status != 200 or not isinstance(data, dict):
            stale = self._cache_peek(key)
            if isinstance(stale, dict) and stale:
                return stale
            return None
        raw = data.get(osi) or data.get(str(osi).replace(" ", ""))
        if raw is None and data:
            want = str(osi).replace(" ", "").upper()
            for k, v in data.items():
                if str(k).replace(" ", "").upper() == want:
                    raw = v
                    break
            if raw is None:
                raw = next(iter(data.values()), None)
        if not isinstance(raw, dict):
            return None
        self._cache_put(key, raw)
        return raw

    def get_option_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch option quotes. One HTTP call per 20 symbols. ~3s cache."""
        cleaned: List[str] = []
        seen = set()
        for raw in symbols or []:
            s = str(raw or "").upper().replace(" ", "")
            if not s or s in seen:
                continue
            seen.add(s)
            cleaned.append(s)
        out: Dict[str, Dict[str, Any]] = {}
        missing: List[str] = []
        for s in cleaned:
            hit = self._cache_get(f"oq:{s}", _OPT_QUOTE_TTL)
            if isinstance(hit, dict) and hit:
                out[s] = hit
            else:
                missing.append(s)
        for i in range(0, len(missing), 20):
            chunk = missing[i : i + 20]
            osis = []
            for s in chunk:
                m = re.match(r"^([A-Z0-9.\-]{1,6})(\d{6})([CP])(\d{8})$", s)
                osis.append(f"{m.group(1):<6}{m.group(2)}{m.group(3)}{m.group(4)}" if m else s)
            status, data = self.request_market("/quotes", {"symbols": ",".join(osis)}, timeout=12)
            if status != 200 or not isinstance(data, dict):
                for s in chunk:
                    stale = self._cache_peek(f"oq:{s}")
                    if isinstance(stale, dict) and stale:
                        out[s] = stale
                continue
            by_compact: Dict[str, Any] = {}
            for k, v in data.items():
                if isinstance(v, dict):
                    by_compact[str(k).replace(" ", "").upper()] = v
            for s, osi in zip(chunk, osis):
                raw = by_compact.get(s) or data.get(osi) or data.get(s)
                if isinstance(raw, dict):
                    out[s] = raw
                    self._cache_put(f"oq:{s}", raw)
        return out

    def place_option_limit(
        self,
        occ_symbol: str,
        quantity: int,
        price: float,
        instruction: str = "BUY_TO_OPEN",
    ) -> Optional[str]:
        """Always LIMIT, RTH NORMAL. Reuses the click-ticket OPTION JSON."""
        return self.place_option_order(
            occ_symbol, quantity, instruction, limit_price=price, market=False, extended_hours=False
        )

    def close_option_limit(self, occ_symbol: str, quantity: int, price: float) -> Optional[str]:
        return self.place_option_limit(occ_symbol, quantity, price, "SELL_TO_CLOSE")

    def place_option_order(
        self,
        osi: str,
        quantity: int,
        instruction: str = "BUY_TO_OPEN",
        *,
        limit_price: Optional[float] = None,
        market: bool = False,
        extended_hours: bool = False,
    ) -> Optional[str]:
        """BUY_TO_OPEN / SELL_TO_CLOSE. Market sells use an aggressive limit if Schwab rejects MARKET."""
        if PAPER_LOCK or getattr(self, "paper_lock", True):
            log_message("[PAPER] blocked live option order POST")
            return None
        if not self._ensure_token():
            return None
        acct = self.get_account_hash()
        if not acct:
            return None
        instruction = instruction.upper()
        qty = max(1, int(quantity))
        session = "NORMAL"
        market = False  # options sleeve: always LIMIT (wide prints fake MARKET fills)

        def _submit(order_type: str, price: Optional[float]) -> tuple:
            order: Dict[str, Any] = {
                "orderType": order_type,
                "session": session,
                "duration": "DAY",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [{
                    "instruction": instruction,
                    "quantity": qty,
                    "instrument": {"symbol": osi, "assetType": "OPTION"},
                }],
            }
            if price is not None:
                order["price"] = round(float(price), 2) if price >= 3 else round(float(price), 2 if price >= 1 else 2)
                # Options < $3 often trade in $0.01; >= $3 in $0.05. Round to 2dp always.
                order["price"] = round(float(price), 2)
            resp = requests.post(
                f"{SCHWAB_BASE_URL}/accounts/{acct}/orders",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=order,
                timeout=12,
            )
            return resp.status_code, resp

        px = limit_price
        if px is None or market:
            q = self.get_option_quote(osi) or {}
            qq = q.get("quote") or q
            bid = float(qq.get("bidPrice") or qq.get("bid") or 0)
            ask = float(qq.get("askPrice") or qq.get("ask") or 0)
            last = float(qq.get("lastPrice") or qq.get("mark") or qq.get("last") or 0)
            if instruction.startswith("BUY"):
                ref = ask if ask > 0 else last
                px = ref * 1.04 if ref else 0
            else:
                ref = bid if bid > 0 else last
                px = ref * 0.96 if ref else 0
        if not px or px <= 0:
            log_message(f"[OPT] no price for {osi} — order aborted")
            return None

        if market and instruction.startswith("SELL"):
            code, resp = _submit("MARKET", None)
            if code in (200, 201):
                oid = resp.headers.get("Location", "").split("/")[-1]
                log_message(f"[OPT] MARKET {instruction} {qty} {osi} ID={oid}")
                return oid
            log_message(f"[OPT] MARKET rejected {code}, falling back to aggressive limit")

        code, resp = _submit("LIMIT", px)
        if code in (200, 201):
            oid = resp.headers.get("Location", "").split("/")[-1]
            log_message(f"[OPT] LIMIT {instruction} {qty} {osi} @ {px} ID={oid}")
            return oid
        log_message(f"[OPT] place failed {code} {(resp.text or '')[:200]}")
        return None

    def get_movers(
        self,
        indices: Optional[List[str]] = None,
        direction: str = "up",
        change: str = "PERCENT",
    ) -> List[str]:
        """Return symbols of top percentage gainers across multiple indices.

        Queries Schwab's movers endpoint for each index and returns a deduplicated
        list of symbols, sorted by largest % gain first.  Used to discover
        gap-up candidates across the whole market without a pre-defined watchlist.

        indices: Schwab market symbols, e.g. ['$COMPX', '$SPX.X', '$DJI']
        Returns list of ticker symbols (strings).
        """
        if not self._ensure_token():
            return []
        if indices is None:
            # $COMPX/$SPX.X/$DJI only return stocks IN those large-cap indices.
            # Adding NASDAQ and NYSE covers ALL exchange-listed stocks,
            # catching micro/nano-cap movers (EDBL, BATL, WFF, etc.) that
            # never appear in the S&P 500 or NASDAQ Composite endpoints.
            indices = ["$COMPX", "$SPX.X", "$DJI", "NASDAQ", "NYSE"]

        seen: dict = {}
        for index in indices:
            try:
                resp = requests.get(
                    f"{SCHWAB_MARKET_URL}/movers/{urllib.parse.quote(index, safe='')}",
                    headers=self._headers(),
                    params={"direction": direction, "change": change},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                for item in resp.json().get("screeners", []):
                    sym = item.get("symbol", "").strip().upper()
                    pct = float(item.get("netPercentChange", 0) or 0)
                    if sym and sym not in seen:
                        seen[sym] = pct
            except Exception as e:
                log_message(f"[SCANNER] get_movers error for {index}: {e}")

        # Return deduplicated symbols sorted by largest % gain
        return [s for s, _ in sorted(seen.items(), key=lambda x: x[1], reverse=True)]

    def search_instruments(self, query: str, projection: str = "symbol-search") -> List[Dict]:
        if not self._ensure_token():
            return []
        resp = requests.get(
            f"{SCHWAB_MARKET_URL}/instruments",
            headers=self._headers(),
            params={"symbol": query, "projection": projection},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        try:
            return list(resp.json().values())
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_session(extended_hours: bool) -> str:
        """
        NORMAL  = regular market hours only (9:30-16:00 ET)
        SEAMLESS = pre-market (4:00-9:30) + regular + after-hours (16:00-20:00)
        """
        return "SEAMLESS" if extended_hours else "NORMAL"

    def place_market_order(
        self,
        symbol: str,
        quantity: int,
        instruction: str = "BUY",
        extended_hours: bool = False,
        limit_buffer_pct: float = 0.005,
    ) -> Optional[str]:
        """
        Always places a LIMIT order to control slippage on low-float stocks.
        Regular session: limit at ask*(1+buffer) for BUY, bid*(1-buffer) for SELL.
        Extended hours: same — Schwab requires LIMIT orders outside regular hours.
        """
        if PAPER_LOCK or getattr(self, "paper_lock", True):
            log_message("[PAPER] blocked live equity order POST")
            return None
        if not self._ensure_token():
            return None
        acct = self.get_account_hash()
        if not acct:
            return None
        session = self._resolve_session(extended_hours)

        # Fetch live quote to set limit price
        quote = self.get_quote(symbol)
        if not quote:
            log_message(f"[ORDER] Cannot get quote for {symbol} — order aborted.")
            return None
        bid = float(quote.get("quote", {}).get("bidPrice", 0) or 0)
        ask = float(quote.get("quote", {}).get("askPrice", 0) or 0)
        last = float(quote.get("quote", {}).get("lastPrice", 0) or 0)

        if instruction.upper() == "BUY":
            ref = ask if ask > 0 else last
            limit_price = round(ref * (1 + limit_buffer_pct), 2)
        else:
            ref = bid if bid > 0 else last
            limit_price = round(ref * (1 - limit_buffer_pct), 2)

        if limit_price <= 0:
            log_message(f"[ORDER] Invalid limit price for {symbol} — order aborted.")
            return None

        order = {
            "orderType":   "LIMIT",
            "session":     session,
            "duration":    "DAY",
            "price":       limit_price,
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": instruction.upper(),
                "quantity":    quantity,
                "instrument":  {"symbol": symbol, "assetType": "EQUITY"},
            }],
        }
        resp = requests.post(
            f"{SCHWAB_BASE_URL}/accounts/{acct}/orders",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=order,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            order_id = resp.headers.get("Location", "").split("/")[-1]
            log_message(f"[ORDER] LIMIT {instruction} {quantity} {symbol} @ {limit_price} ({session}) — ID: {order_id}")
            return order_id
        import json as _json
        log_message(f"[ORDER] place_market_order failed: {resp.status_code} {resp.text}")
        log_message(f"[ORDER] Payload sent: {_json.dumps(order)}")
        return None

    def place_limit_order(
        self,
        symbol: str,
        quantity: int,
        price: float,
        instruction: str = "BUY",
        extended_hours: bool = False,
    ) -> Optional[str]:
        if PAPER_LOCK or getattr(self, "paper_lock", True):
            log_message("[PAPER] blocked live limit order POST")
            return None
        if not self._ensure_token():
            return None
        acct = self.get_account_hash()
        if not acct:
            return None
        order = {
            "orderType":   "LIMIT",
            "session":     self._resolve_session(extended_hours),
            "duration":    "DAY",
            "price":       round(price, 2),
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "orderLegType": "EQUITY",
                "instruction": instruction.upper(),
                "quantity":    quantity,
                "instrument":  {"symbol": symbol, "type": "EQUITY"},
            }],
        }
        resp = requests.post(
            f"{SCHWAB_BASE_URL}/accounts/{acct}/orders",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=order,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            order_id = resp.headers.get("Location", "").split("/")[-1]
            log_message(f"[ORDER] LIMIT {instruction} {quantity} {symbol} @ {price} — ID: {order_id}")
            return order_id
        log_message(f"[ORDER] place_limit_order failed: {resp.status_code} {resp.text}")
        return None

    def _oco_stop_child(
        self,
        symbol: str,
        quantity: int,
        stop_price: float,
        session: str,
        extended_hours: bool,
    ) -> Dict[str, Any]:
        """Build the stop-loss leg for an exit OCO bracket."""
        leg = {
            "instruction": "SELL",
            "quantity":    quantity,
            "instrument":  {"symbol": symbol, "assetType": "EQUITY"},
        }
        if extended_hours:
            # Schwab SEAMLESS rejects STOP orders — only LIMIT is allowed.
            # A limit sell below market rests until price falls to the stop level.
            return {
                "orderType":   "LIMIT",
                "session":     session,
                "duration":    "DAY",
                "price":       round(stop_price, 2),
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [leg],
            }
        return {
            "orderType":   "STOP",
            "session":     session,
            "duration":    "DAY",
            "stopPrice":   round(stop_price, 2),
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [leg],
        }

    def place_oco_order(
        self,
        symbol: str,
        quantity: int,
        stop_price: float,
        limit_price: float,
        extended_hours: bool = False,
    ) -> Optional[str]:
        """One-Cancels-Other: stop loss + limit take profit after entry fill."""
        if PAPER_LOCK or getattr(self, "paper_lock", True):
            log_message("[PAPER] blocked live OCO POST")
            return None
        if not self._ensure_token():
            return None
        acct = self.get_account_hash()
        if not acct:
            return None
        session = self._resolve_session(extended_hours)
        stop_child = self._oco_stop_child(symbol, quantity, stop_price, session, extended_hours)
        order = {
            "orderStrategyType": "OCO",
            "childOrderStrategies": [
                stop_child,
                {
                    "orderType":   "LIMIT",
                    "session":     session,
                    "duration":    "DAY",
                    "price":       round(limit_price, 2),
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [{
                        "instruction": "SELL",
                        "quantity":    quantity,
                        "instrument":  {"symbol": symbol, "assetType": "EQUITY"},
                    }],
                },
            ],
        }
        resp = requests.post(
            f"{SCHWAB_BASE_URL}/accounts/{acct}/orders",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=order,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            order_id = resp.headers.get("Location", "").split("/")[-1]
            mode = "LIMIT-stop" if extended_hours else "STOP"
            log_message(
                f"[ORDER] OCO SELL {quantity} {symbol} "
                f"stop={stop_price} target={limit_price} ({mode}) — ID: {order_id}"
            )
            return order_id
        import json as _json
        log_message(f"[ORDER] place_oco_order failed: {resp.status_code} {resp.text}")
        log_message(f"[ORDER] OCO payload: {_json.dumps(order)}")
        return None

    def place_trailing_stop_order(
        self,
        symbol: str,
        quantity: int,
        trail_pct: float,
        extended_hours: bool = False,
    ) -> Optional[str]:
        """Trailing stop SELL — trails the last price by trail_pct%."""
        if PAPER_LOCK or getattr(self, "paper_lock", True):
            log_message("[PAPER] blocked live trailing-stop POST")
            return None
        if not self._ensure_token():
            return None
        acct = self.get_account_hash()
        if not acct:
            return None
        order = {
            "orderType":          "TRAILING_STOP",
            "session":            self._resolve_session(extended_hours),
            "duration":           "DAY",
            "stopPriceLinkBasis": "LAST",
            "stopPriceLinkType":  "PERCENT",
            "stopPriceOffset":    round(trail_pct, 2),
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "orderLegType": "EQUITY",
                "instruction": "SELL",
                "quantity":    quantity,
                "instrument":  {"symbol": symbol, "type": "EQUITY"},
            }],
        }
        resp = requests.post(
            f"{SCHWAB_BASE_URL}/accounts/{acct}/orders",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=order,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            order_id = resp.headers.get("Location", "").split("/")[-1]
            log_message(f"[ORDER] TRAILING_STOP SELL {quantity} {symbol} trail={trail_pct}% — ID: {order_id}")
            return order_id
        log_message(f"[ORDER] place_trailing_stop_order failed: {resp.status_code} {resp.text}")
        if extended_hours:
            quote = self.get_quote(symbol)
            last = float((quote or {}).get("quote", {}).get("lastPrice", 0) or 0)
            if last > 0:
                floor = round(last * (1 - trail_pct / 100.0), 2)
                log_message(
                    f"[ORDER] TRAILING_STOP unavailable in extended hours — "
                    f"falling back to LIMIT SELL @ {floor}"
                )
                return self.place_limit_order(symbol, quantity, floor, "SELL", extended_hours=True)
        return None

    def cancel_order(self, order_id: str) -> bool:
        if PAPER_LOCK or getattr(self, "paper_lock", True):
            log_message("[PAPER] blocked live cancel POST")
            return False
        if not self._ensure_token():
            return False
        acct = self.get_account_hash()
        if not acct:
            return False
        resp = requests.delete(
            f"{SCHWAB_BASE_URL}/accounts/{acct}/orders/{order_id}",
            headers=self._headers(),
            timeout=10,
        )
        return resp.status_code in (200, 204)

    def get_orders(self) -> List[Dict]:
        if not self._ensure_token():
            return []
        acct = self.get_account_hash()
        if not acct:
            return []
        resp = requests.get(
            f"{SCHWAB_BASE_URL}/accounts/{acct}/orders",
            headers=self._headers(),
            params={"status": "WORKING"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        try:
            return resp.json()
        except Exception:
            return []


    def get_transactions(
        self,
        start_date: str = None,
        end_date:   str = None,
        types:      str = "TRADE",
    ) -> list:
        """Fetch account transaction history from Schwab.
        Retrieves ALL activity including manual TOS trades and bot trades.
        start_date / end_date: ISO-8601 strings e.g. '2026-01-01T00:00:00.000Z'
        Default window: last 90 days.
        """
        import datetime as _dt
        if not self._ensure_token():
            return []
        acct = self.get_account_hash()
        if not acct:
            return []
        now = _dt.datetime.utcnow()
        if not end_date:
            end_date   = now.strftime("%Y-%m-%dT23:59:59.999Z")
        if not start_date:
            start_date = (now - _dt.timedelta(days=90)).strftime("%Y-%m-%dT00:00:00.000Z")
        try:
            resp = requests.get(
                f"{SCHWAB_BASE_URL}/accounts/{acct}/transactions",
                headers=self._headers(),
                params={"startDate": start_date, "endDate": end_date, "types": types},
                timeout=30,
            )
            if resp.status_code != 200:
                from utils import log_message
                log_message(f"[JOURNAL] transactions HTTP {resp.status_code}")
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            from utils import log_message
            log_message(f"[JOURNAL] transactions error: {e}")
            return []

    def is_authenticated(self) -> bool:
        return bool(self._access_token) and time.time() < self._token_expiry
