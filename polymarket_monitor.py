"""
polymarket_monitor.py — Polymarket copy-signal engine for the TOS bot.

Watches top Polymarket wallets and routes stock signals to the existing
trading engine (engine.manual_buy / engine.manual_sell).

Runs as a daemon thread; all network calls use requests (sync).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

import requests

log = logging.getLogger("polymarket")

DATA_API = "https://data-api.polymarket.com"

# ── Stock symbol detection ────────────────────────────────────────────────────
# Ordered longest→shortest so "bank of america" wins over "america"

STOCK_MAP: list[tuple[str, str]] = [
    ("bank of america",   "BAC"),
    ("goldman sachs",     "GS"),
    ("jp morgan",         "JPM"),
    ("jpmorgan",          "JPM"),
    ("johnson & johnson", "JNJ"),
    ("johnson and johnson","JNJ"),
    ("berkshire hathaway","BRK.B"),
    ("s&p 500",           "SPY"),
    ("sp 500",            "SPY"),
    ("sp500",             "SPY"),
    ("s&p500",            "SPY"),
    ("nasdaq 100",        "QQQ"),
    ("dow jones",         "DIA"),
    ("trump media",       "DJT"),
    ("bitcoin etf",       "IBIT"),
    ("spot bitcoin etf",  "IBIT"),
    ("palantir",          "PLTR"),
    ("microsoft",         "MSFT"),
    ("alphabet",          "GOOGL"),
    ("gamestop",          "GME"),
    ("coinbase",          "COIN"),
    ("robinhood",         "HOOD"),
    ("nvidia",            "NVDA"),
    ("amazon",            "AMZN"),
    ("netflix",           "NFLX"),
    ("twitter",           "X"),
    ("tesla",             "TSLA"),
    ("google",            "GOOGL"),
    ("facebook",          "META"),
    ("apple",             "AAPL"),
    ("exxon",             "XOM"),
    ("chevron",           "CVX"),
    ("pfizer",            "PFE"),
    ("walmart",           "WMT"),
    ("disney",            "DIS"),
    ("intel",             "INTC"),
    ("amd",               "AMD"),
    ("paypal",            "PYPL"),
    ("uber",              "UBER"),
    ("airbnb",            "ABNB"),
    ("openai",            "N/A"),     # not public — skip below
    # Ticker-only fallbacks
    ("nvda",  "NVDA"), ("msft", "MSFT"), ("tsla", "TSLA"),
    ("aapl",  "AAPL"), ("amzn", "AMZN"), ("meta", "META"),
    ("googl", "GOOGL"),("nflx", "NFLX"), ("amd",  "AMD"),
    ("intc",  "INTC"), ("pltr", "PLTR"), ("coin", "COIN"),
    ("spy",   "SPY"),  ("qqq",  "QQQ"),  ("djt",  "DJT"),
    ("ibit",  "IBIT"), ("gme",  "GME"),
]


def detect_stock_symbol(title: str) -> Optional[str]:
    """Return a tradable ticker from a Polymarket market title, or None."""
    low = title.lower()
    for keyword, ticker in STOCK_MAP:
        if keyword in low:
            return None if ticker == "N/A" else ticker
    return None


# ── Monitor class ─────────────────────────────────────────────────────────────

_DEFAULT_CFG: dict[str, Any] = {
    "top_wallet_count":     10,
    "poll_interval_seconds":30,
    "min_trade_usd":        5.0,
    "daily_loss_limit_usd": 200.0,
    "max_positions":        10,
    "dry_run":              True,   # safe default
}

_CFG_FILE = os.path.join(os.path.dirname(__file__), "saved_data", "pm_config.json")


class PolymarketMonitor:
    """
    Background thread that polls Polymarket top wallets and emits stock
    trade signals to the TOS engine.
    """

    def __init__(self, engine, emit_callback: Optional[Callable] = None):
        self._engine        = engine                 # TOS TradingEngine instance
        self._emit          = emit_callback or (lambda ev, d: None)
        self._config        = self._load_cfg()
        self._running       = False
        self._stop_ev       = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._top_wallets:  list[dict] = []
        self._watermarks:   dict[str, int] = {}
        self._open_pos:     dict[str, dict] = {}     # token_id → position info
        self._seen_hashes:  set[str] = set()
        self._daily_pnl:    float = 0.0
        self._total_pnl:    float = 0.0
        self._signals_today:int = 0
        self._last_day:     int = 0                  # day-of-year for reset
        self._last_wallet_refresh: float = 0.0

    # ── config persistence ────────────────────────────────────────────────────

    def _load_cfg(self) -> dict:
        cfg = dict(_DEFAULT_CFG)
        try:
            if os.path.exists(_CFG_FILE):
                with open(_CFG_FILE) as f:
                    cfg.update(json.load(f))
        except Exception:
            pass
        return cfg

    def save_cfg(self, updates: dict) -> None:
        self._config.update(updates)
        os.makedirs(os.path.dirname(_CFG_FILE), exist_ok=True)
        with open(_CFG_FILE, "w") as f:
            json.dump(self._config, f, indent=2)

    def get_cfg(self) -> dict:
        return dict(self._config)

    # ── control ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._stop_ev.clear()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="pm-monitor")
        self._thread.start()
        log.info("PolymarketMonitor started")
        self._emit("pm_status", {"running": True, "msg": "Monitor started"})

    def stop(self) -> None:
        self._stop_ev.set()
        self._running = False
        log.info("PolymarketMonitor stop requested")
        self._emit("pm_status", {"running": False, "msg": "Monitor stopping…"})

    @property
    def running(self) -> bool:
        return self._running

    def get_state(self) -> dict:
        return {
            "running":       self._running,
            "daily_pnl":     self._daily_pnl,
            "total_pnl":     self._total_pnl,
            "signals_today": self._signals_today,
            "open_positions":list(self._open_pos.values()),
            "wallet_count":  len(self._top_wallets),
        }

    def get_wallets(self) -> list[dict]:
        return list(self._top_wallets)

    # ── main loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        log.info("PolymarketMonitor loop starting…")
        while not self._stop_ev.is_set():
            try:
                self._maybe_reset_daily()

                # Refresh wallet rankings every 6 h
                if time.time() - self._last_wallet_refresh > 21_600:
                    self._refresh_wallets()

                if not self._top_wallets:
                    self._stop_ev.wait(60)
                    continue

                # Poll each wallet for new trades
                for w in self._top_wallets:
                    if self._stop_ev.is_set():
                        break
                    self._poll_wallet(w)

                self._emit("pm_state", self.get_state())

            except Exception as exc:
                log.error(f"PolymarketMonitor loop error: {exc}")

            self._stop_ev.wait(self._config["poll_interval_seconds"])

        self._running = False
        log.info("PolymarketMonitor loop exited")
        self._emit("pm_status", {"running": False, "msg": "Monitor stopped"})

    # ── wallet ranking ────────────────────────────────────────────────────────

    def _refresh_wallets(self) -> None:
        log.info("Fetching Polymarket leaderboard…")
        try:
            all_time = self._get_leaderboard("ALL",   50)
            monthly  = self._get_leaderboard("MONTH", 50)

            addr_map: dict[str, dict] = {}
            for e in all_time:
                a = (e.get("proxyWallet") or "").lower()
                if a:
                    addr_map[a] = {**e, "pnl_all": float(e.get("pnl") or 0),
                                   "vol_all": float(e.get("vol") or 0),
                                   "pnl_month": 0.0, "trades_90d": 0, "score": 0.0,
                                   "username": e.get("userName") or a[:10]}
            for e in monthly:
                a = (e.get("proxyWallet") or "").lower()
                if a and a in addr_map:
                    addr_map[a]["pnl_month"] = float(e.get("pnl") or 0)

            # Fetch 90-day trade count per wallet (batched, limited)
            start_90d = int(time.time()) - 90 * 86_400
            for a, info in addr_map.items():
                if self._stop_ev.is_set():
                    return
                try:
                    trades = self._get_trades(a, start_90d, limit=500)
                    info["trades_90d"]  = len(trades)
                    info["market_divs"] = len({t.get("conditionId") for t in trades})
                except Exception:
                    pass

            # Score and rank
            max_pnl_all   = max((v["pnl_all"]   for v in addr_map.values()), default=1) or 1
            max_pnl_month = max((v["pnl_month"]  for v in addr_map.values()), default=1) or 1
            max_trades    = max((v["trades_90d"] for v in addr_map.values()), default=1) or 1
            max_div       = max((v.get("market_divs", 0) for v in addr_map.values()), default=1) or 1

            for info in addr_map.values():
                info["score"] = (
                    0.50 * max(info["pnl_all"],   0) / max_pnl_all
                    + 0.30 * max(info["pnl_month"], 0) / max_pnl_month
                    + 0.10 * info["trades_90d"] / max_trades
                    + 0.10 * info.get("market_divs", 0) / max_div
                )

            ranked = sorted(addr_map.values(), key=lambda x: x["score"], reverse=True)
            top_n  = self._config["top_wallet_count"]
            self._top_wallets = ranked[:top_n]

            # Reset watermarks for any new wallet
            now_ts = int(time.time()) - 180
            for w in self._top_wallets:
                self._watermarks.setdefault(w["proxyWallet"].lower(), now_ts)

            self._last_wallet_refresh = time.time()
            log.info(f"Leaderboard refreshed — tracking {len(self._top_wallets)} wallets")
            self._emit("pm_wallets", self._top_wallets)

        except Exception as exc:
            log.error(f"Wallet refresh failed: {exc}")

    # ── wallet polling ────────────────────────────────────────────────────────

    def _poll_wallet(self, wallet: dict) -> None:
        addr  = (wallet.get("proxyWallet") or "").lower()
        since = self._watermarks.get(addr, int(time.time()) - 180)

        try:
            trades = self._get_trades(addr, since, limit=100)
        except Exception as exc:
            log.debug(f"Poll error {addr[:10]}: {exc}")
            return

        new_max = since
        for raw in trades:
            ts = int(raw.get("timestamp") or 0)
            if ts <= since:
                continue
            new_max = max(new_max, ts)
            self._handle_trade(raw, wallet)

        self._watermarks[addr] = new_max

    def _handle_trade(self, raw: dict, wallet: dict) -> None:
        tx      = (raw.get("transactionHash") or "").strip()
        key     = tx or f"{wallet.get('proxyWallet','')}:{raw.get('asset','')}:{raw.get('timestamp','')}"
        if key in self._seen_hashes:
            return
        self._seen_hashes.add(key)
        if len(self._seen_hashes) > 100_000:
            self._seen_hashes = set(list(self._seen_hashes)[-50_000:])

        side      = (raw.get("side") or "").upper()
        title     = raw.get("title") or ""
        token_id  = raw.get("asset") or ""
        price     = float(raw.get("price") or 0)
        size      = float(raw.get("size") or 0)
        usd_value = price * size
        ts        = int(raw.get("timestamp") or time.time())

        if side == "BUY":
            if usd_value < self._config["min_trade_usd"]:
                return
            if len(self._open_pos) >= self._config["max_positions"]:
                return
            if self._daily_pnl <= -self._config["daily_loss_limit_usd"]:
                log.warning("Daily loss limit hit — pausing buys")
                return
            if token_id in self._open_pos:
                return

            symbol = detect_stock_symbol(title)
            if not symbol:
                return

            log.info(f"▲ BUY  {title[:55]}  →  {symbol}  src={wallet.get('username','?')}")
            self._signals_today += 1

            sig_payload = {
                "time":    time.strftime("%H:%M:%S"),
                "side":    "BUY",
                "symbol":  symbol,
                "title":   title[:70],
                "price":   price,
                "source":  wallet.get("username", wallet.get("proxyWallet", "")[:10]),
                "dry_run": self._config["dry_run"],
            }
            self._emit("pm_signal", sig_payload)

            if not self._config["dry_run"]:
                try:
                    self._engine.manual_buy(symbol)
                    self._open_pos[token_id] = {
                        "token_id": token_id, "symbol": symbol,
                        "title": title, "buy_price": price,
                        "opened_at": ts,
                    }
                except Exception as exc:
                    log.error(f"manual_buy({symbol}) failed: {exc}")
            else:
                self._open_pos[token_id] = {
                    "token_id": token_id, "symbol": symbol,
                    "title": title, "buy_price": price,
                    "opened_at": ts, "dry_run": True,
                }

        elif side == "SELL":
            pos = self._open_pos.get(token_id)
            if not pos:
                return

            symbol = pos["symbol"]
            log.info(f"▼ SELL {title[:55]}  →  {symbol}  src={wallet.get('username','?')}")

            sig_payload = {
                "time":    time.strftime("%H:%M:%S"),
                "side":    "SELL",
                "symbol":  symbol,
                "title":   title[:70],
                "price":   price,
                "source":  wallet.get("username", "?"),
                "dry_run": self._config["dry_run"],
            }
            self._emit("pm_signal", sig_payload)

            if not self._config["dry_run"]:
                try:
                    self._engine.manual_sell(symbol)
                except Exception as exc:
                    log.error(f"manual_sell({symbol}) failed: {exc}")

            del self._open_pos[token_id]

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_leaderboard(self, period: str, limit: int) -> list[dict]:
        r = requests.get(
            f"{DATA_API}/v1/leaderboard",
            params={"timePeriod": period, "orderBy": "PNL", "limit": min(limit, 50)},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def _get_trades(self, address: str, since_ts: int, limit: int = 100) -> list[dict]:
        r = requests.get(
            f"{DATA_API}/trades",
            params={"user": address, "start": since_ts,
                    "limit": limit, "takerOnly": "false"},
            timeout=12,
        )
        if not r.ok:
            return []
        return r.json()

    def _maybe_reset_daily(self) -> None:
        today = time.localtime().tm_yday
        if today != self._last_day:
            self._daily_pnl    = 0.0
            self._signals_today= 0
            self._last_day     = today
