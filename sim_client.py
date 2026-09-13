"""
sim_client.py
Synthetic market data client — zero API calls, zero credentials.

Drop-in replacement for SchwabClient that generates realistic intraday
price data for any symbol. Used to validate the full trading loop
(scanner → ORB calculation → signal → order execution) before API access.

Pattern per symbol (deterministic by symbol + date seed):
  - Minutes 0-14  (ORB window): tight consolidation range
  - Minutes 15-25 (breakout):   steady push above ORB high, volume spike on bar 16
  - Minutes 26-60 (trend):      up-trend with healthy pullbacks
  - Minutes 61+   (fade):       sideways / gradual fade
"""

import datetime
import hashlib
import math
import random
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from utils import log_message, now_et as _now_et

_ET = ZoneInfo("America/New_York")


def _sym_seed(symbol: str) -> int:
    """Deterministic, symbol+date unique seed — same candles all day, every run.

    Uses an MD5 digest rather than the builtin hash() (which is randomized per
    process via PYTHONHASHSEED) so simulation results are reproducible.
    """
    today = _now_et().date().isoformat()
    digest = hashlib.md5(f"{symbol}|{today}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


class SimClient:
    """
    Synthetic Schwab API client.
    All market data is procedurally generated — no network calls.
    Orders are accepted and returned with SIM-XXXX IDs.
    """

    def __init__(self):
        self.app_key        = "SIM_KEY"
        self.app_secret     = "SIM_SECRET"
        self._order_counter = 1000
        self._account_hash  = "SIM_ACCOUNT"
        self._access_token  = "sim_token"
        self._refresh_token = "sim_refresh"
        self._token_expiry  = 9_999_999_999.0
        self._cash          = 7111.00
        # --- Synthetic broker state (fill engine) ---
        # symbol -> {"quantity": int, "avg_price": float}
        self._sim_positions: Dict[str, Dict] = {}
        # order_id -> {"symbol","type","quantity","stop","target","trail_pct","peak","status"}
        self._sim_orders: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # Auth stubs
    # ------------------------------------------------------------------

    def is_authenticated(self) -> bool:
        return True

    def _ensure_token(self) -> bool:
        return True

    def get_account_hash(self) -> str:
        return self._account_hash

    # ------------------------------------------------------------------
    # Account data
    # ------------------------------------------------------------------

    def get_balance(self) -> Dict:
        invested = sum(p["quantity"] * p["avg_price"] for p in self._sim_positions.values())
        return {"cash_available": round(self._cash - invested, 2), "total_value": 7111.00}

    def _current_price(self, symbol: str) -> float:
        try:
            return float(self.get_quote(symbol)["quote"]["lastPrice"])
        except Exception:
            return 0.0

    def _check_fills(self) -> None:
        """Resolve any working stop/target/trailing orders against the current
        synthetic price. Filled orders close the position so get_positions()
        reflects the exit — this is what the engine's reconciliation reads."""
        for oid, order in list(self._sim_orders.items()):
            if order.get("status") != "WORKING":
                continue
            symbol = order["symbol"]
            price  = self._current_price(symbol)
            if price <= 0:
                continue

            filled = False
            if order["type"] == "OCO":
                if order.get("stop") and price <= order["stop"]:
                    filled = True
                elif order.get("target") and price >= order["target"]:
                    filled = True
            elif order["type"] == "TRAIL":
                peak = max(order.get("peak", price), price)
                order["peak"] = peak
                stop_level = peak * (1 - order["trail_pct"] / 100.0)
                if price <= stop_level:
                    filled = True

            if filled:
                order["status"] = "FILLED"
                self._reduce_position(symbol, order["quantity"])
                log_message(f"[SIM] Exit order {oid} FILLED for {symbol} @ ~{price:.4f}")

    def _reduce_position(self, symbol: str, quantity: int) -> None:
        pos = self._sim_positions.get(symbol)
        if not pos:
            return
        pos["quantity"] -= quantity
        if pos["quantity"] <= 0:
            self._sim_positions.pop(symbol, None)

    def get_positions(self) -> List:
        self._check_fills()
        out = []
        for symbol, pos in self._sim_positions.items():
            if pos["quantity"] <= 0:
                continue
            out.append({
                "symbol":       symbol,
                "quantity":     pos["quantity"],
                "avg_price":    pos["avg_price"],
                "market_value": round(pos["quantity"] * self._current_price(symbol), 2),
            })
        return out

    def get_orders(self) -> List:
        self._check_fills()
        return [
            {"orderId": oid, "symbol": o["symbol"], "status": o["status"],
             "quantity": o["quantity"]}
            for oid, o in self._sim_orders.items()
            if o["status"] == "WORKING"
        ]

    def cancel_order(self, order_id: str) -> bool:
        order = self._sim_orders.get(order_id)
        if order and order["status"] == "WORKING":
            order["status"] = "CANCELED"
        log_message(f"[SIM] Cancel order {order_id}")
        return True

    # ------------------------------------------------------------------
    # Quote generation
    # ------------------------------------------------------------------

    def _base_price(self, symbol: str) -> float:
        rng = random.Random(_sym_seed(symbol))
        return round(rng.uniform(2.20, 8.50), 2)

    def get_quote(self, symbol: str) -> Dict:
        rng   = random.Random(_sym_seed(symbol))
        base  = self._base_price(symbol)
        prev  = round(base * rng.uniform(0.88, 0.93), 4)   # gap-up 7-14%
        open_ = round(base, 4)

        # Simulate some intraday drift
        _now = _now_et()
        elapsed  = max(0, (_now.hour * 60 + _now.minute) - (9 * 60 + 30))
        drift    = min(elapsed * 0.0003, 0.08) * rng.uniform(0.5, 1.5)
        last     = round(open_ * (1 + drift), 4)
        bid      = round(last - 0.01, 4)
        ask      = round(last + 0.01, 4)

        avg_vol  = int(rng.uniform(180_000, 450_000))
        rel_vol  = rng.uniform(2.8, 5.5)
        volume   = int(avg_vol * rel_vol)

        return {
            "quote": {
                "lastPrice":   last,
                "bidPrice":    bid,
                "askPrice":    ask,
                "closePrice":  prev,
                "openPrice":   open_,
                "totalVolume": volume,
            },
            "fundamental": {
                "vol10DayAvg": avg_vol,
                "vol1YearAvg": avg_vol,
                "sharesFloat": int(rng.uniform(20_000_000, 75_000_000)),
            },
        }

    def get_quotes(self, symbols: List[str]) -> Dict:
        return {s: self.get_quote(s) for s in symbols}

    # ------------------------------------------------------------------
    # Price history — 1-min candles with realistic ORB + breakout
    # ------------------------------------------------------------------

    @staticmethod
    def _day_candles(
        symbol: str,
        trade_date: datetime.date,
        start_price: float,
        extended_hours: bool = False,
        cap_at_now: bool = False,
        cutoff_time: "Optional[datetime.datetime]" = None,
    ) -> "List[Dict]":
        """Generate a full day of synthetic 1-min candles for one trading date.

        Uses a per-day seed (symbol + date) so the same call always returns the
        same data, enabling deterministic replay across lookback days.
        """
        date_str = trade_date.isoformat()
        digest   = hashlib.md5(f"{symbol}|{date_str}".encode()).hexdigest()
        rng      = random.Random(int(digest[:8], 16))

        _et          = _ET
        market_open  = datetime.datetime(trade_date.year, trade_date.month, trade_date.day, 9, 30, 0, tzinfo=_et)
        market_close = datetime.datetime(trade_date.year, trade_date.month, trade_date.day, 16, 0, 0, tzinfo=_et)
        premarket_open = datetime.datetime(trade_date.year, trade_date.month, trade_date.day, 4, 0, 0, tzinfo=_et)
        cutoff = (cutoff_time if cutoff_time is not None
                  else _now_et() if cap_at_now
                  else market_close)

        candles = []
        # Daily trend direction: slightly bullish/bearish/flat
        daily_bias = rng.uniform(-0.0003, 0.0006)  # per-minute drift bias

        if extended_hours:
            price = round(start_price * rng.uniform(0.99, 1.01), 4)
            t = premarket_open
            while t < market_open and t <= cutoff:
                drift = rng.uniform(-0.0020, 0.0030)
                price = max(0.50, price * (1 + drift))
                vol   = int(rng.uniform(5_000, 40_000))
                candles.append({"datetime": int(t.timestamp()*1000),
                    "open": round(price*(1+rng.uniform(-0.001,0.001)),4),
                    "high": round(price*(1+rng.uniform(0,0.003)),4),
                    "low":  round(price*(1-rng.uniform(0,0.003)),4),
                    "close": round(price,4), "volume": vol})
                t += datetime.timedelta(minutes=1)

        price = start_price
        i, t  = 0, market_open
        while t < market_close and t <= cutoff:
            if i < 15:
                drift = rng.uniform(-0.0015, 0.0015)          # ORB consolidation
            elif i < 18:
                drift = rng.uniform(0.003, 0.010)             # initial push
            elif i < 40:
                drift = daily_bias + rng.uniform(-0.003, 0.006)  # trend + noise
            elif i < 80:
                drift = daily_bias + rng.uniform(-0.004, 0.004)  # chop
            else:
                drift = daily_bias + rng.uniform(-0.005, 0.003)  # fade

            price = max(0.50, price * (1 + drift))
            vol   = int(rng.uniform(80_000, 280_000))
            if i == 16:
                vol = int(vol * rng.uniform(3.0, 4.5))

            candles.append({"datetime": int(t.timestamp()*1000),
                "open":  round(price*(1+rng.uniform(-0.001,0.001)),4),
                "high":  round(price*(1+rng.uniform(0.000,0.004)),4),
                "low":   round(price*(1-rng.uniform(0.000,0.004)),4),
                "close": round(price,4), "volume": vol})
            t += datetime.timedelta(minutes=1)
            i += 1

        return candles

    def get_price_history(
        self,
        symbol: str,
        period_type: str = "day",
        period: int = 1,
        frequency_type: str = "minute",
        frequency: int = 1,
        extended_hours: bool = False,
        **kwargs,
    ) -> List[Dict]:
        start_ms = kwargs.get('start_ms')
        end_ms   = kwargs.get('end_ms')
        base     = self._base_price(symbol)
        _now     = _now_et()
        today_et = _now.date()

        if start_ms:
            # Multi-day mode: generate candles from start_date to today.
            # Each trading day is independently seeded (symbol + date) so the
            # result is deterministic and reproducible for the same symbol/day.
            start_dt = datetime.datetime.fromtimestamp(
                start_ms / 1000,
                tz=_ET
            ).date()
            all_candles: List[Dict] = []
            price = base  # carry price across days for continuity
            cur   = start_dt
            sim_now = _now  # sim clock at call time
            while cur <= today_et:
                if cur.weekday() < 5:  # skip weekends
                    is_today = (cur == today_et)
                    # Pass the sim clock as the cutoff for today so candles
                    # only go up to the current sim time, not real-clock end-of-day.
                    day_c = self._day_candles(
                        symbol, cur, price,
                        extended_hours=extended_hours,
                        cap_at_now=is_today,
                        cutoff_time=sim_now if is_today else None,
                    )
                    if day_c:
                        price = day_c[-1]['close']  # next day opens near today's close
                        all_candles.extend(day_c)
                cur += datetime.timedelta(days=1)
            # Also apply end_ms as a hard upper boundary
            if end_ms:
                all_candles = [c for c in all_candles if c['datetime'] <= end_ms]
            return all_candles

        # Single-day mode (original behaviour): today's candles only
        return self._day_candles(
            symbol, today_et, base,
            extended_hours=extended_hours, cap_at_now=True,
            cutoff_time=_now,
        )

    def search_instruments(self, query: str, projection: str = "symbol-search") -> List:
        return []

    def get_movers(self, indices=None, direction: str = "up", change: str = "PERCENT") -> List[str]:
        """Sim stub — return a set of synthetic gap-up candidates covering
        a variety of price ranges and sectors so the sim has realistic variety."""
        return [
            "NIO", "LCID", "RIVN", "SPCE", "SOFI", "HOOD", "PLTR",
            "AMC", "GME", "MVIS", "CLOV", "WISH", "SNDL", "BBAI",
            "IONQ", "RGTI", "QBTS", "ACHR", "JOBY", "LILM",
        ]

    # ------------------------------------------------------------------
    # Order placement stubs
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        self._order_counter += 1
        return f"SIM-{self._order_counter}"

    def place_market_order(
        self,
        symbol: str,
        quantity: int,
        instruction: str = "BUY",
        extended_hours: bool = False,
        limit_buffer_pct: float = 0.005,
    ) -> Optional[str]:
        oid = self._next_id()
        session = "SEAMLESS" if extended_hours else "NORMAL"
        fill_price = self._current_price(symbol)
        if instruction.upper().startswith("BUY"):
            pos = self._sim_positions.get(symbol)
            if pos:
                total_qty = pos["quantity"] + quantity
                pos["avg_price"] = round(
                    (pos["quantity"] * pos["avg_price"] + quantity * fill_price) / total_qty, 4
                )
                pos["quantity"] = total_qty
            else:
                self._sim_positions[symbol] = {"quantity": quantity, "avg_price": fill_price}
        else:  # SELL
            self._reduce_position(symbol, quantity)
        log_message(f"[SIM] {instruction} {quantity} {symbol} @ ~{fill_price:.4f} ({session}) → {oid}")
        return oid

    def place_limit_order(
        self,
        symbol: str,
        quantity: int,
        price: float,
        instruction: str = "BUY",
        extended_hours: bool = False,
    ) -> Optional[str]:
        oid = self._next_id()
        if instruction.upper().startswith("BUY"):
            pos = self._sim_positions.get(symbol)
            if pos:
                total_qty = pos["quantity"] + quantity
                pos["avg_price"] = round(
                    (pos["quantity"] * pos["avg_price"] + quantity * price) / total_qty, 4
                )
                pos["quantity"] = total_qty
            else:
                self._sim_positions[symbol] = {"quantity": quantity, "avg_price": price}
        else:
            self._reduce_position(symbol, quantity)
        log_message(f"[SIM] LIMIT {instruction} {quantity} {symbol} @ {price} → {oid}")
        return oid

    def place_oco_order(
        self,
        symbol: str,
        quantity: int,
        stop_price: float,
        limit_price: float,
        extended_hours: bool = False,
    ) -> Optional[str]:
        oid = self._next_id()
        self._sim_orders[oid] = {
            "symbol": symbol, "type": "OCO", "quantity": quantity,
            "stop": float(stop_price), "target": float(limit_price),
            "trail_pct": 0.0, "peak": 0.0, "status": "WORKING",
        }
        log_message(f"[SIM] OCO {symbol} stop={stop_price} target={limit_price} → {oid}")
        return oid

    def place_trailing_stop_order(
        self,
        symbol: str,
        quantity: int,
        trail_pct: float,
        extended_hours: bool = False,
    ) -> Optional[str]:
        oid = self._next_id()
        self._sim_orders[oid] = {
            "symbol": symbol, "type": "TRAIL", "quantity": quantity,
            "stop": 0.0, "target": 0.0, "trail_pct": float(trail_pct),
            "peak": self._current_price(symbol), "status": "WORKING",
        }
        log_message(f"[SIM] TRAIL STOP {symbol} {trail_pct}% → {oid}")
        return oid
