"""
backtest_engine.py
Fast offline strategy backtester — replays synthetic OHLC data at max speed.
No market hours, no API calls, no credentials required.

Strategies supported:
  orb   — Opening Range Breakout (15-min ORB window)
  scalp — Scalp Breakout / ORB-momentum scalp
  div   — Divergence (30-min Std Dev Channel + RSI divergence, multi-day)
  rsi   — RSI Mean Reversion (daily uptrend + RSI≤entry, multi-day hold)
  swing — Intraday swing range entries near support

Usage:
  from backtest_engine import BacktestEngine
  bt = BacktestEngine()
  report = bt.run({"symbols": ["TSLA", "NVDA"], "days": 5,
                   "strategies": ["orb", "scalp", "div", "rsi", "swing"],
                   "capital": 10000.0})
"""

import datetime
import os
import hashlib
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from utils import set_sim_now, log_message

_ET = ZoneInfo("America/New_York")

# ─────────────────────────────────────────────────────────────────
# Dedicated backtest log  (saved_data/backtest.log)
# ─────────────────────────────────────────────────────────────────

BT_LOG_FILE = os.path.join("saved_data", "backtest.log")
_BT_LOG_MAX = 4 * 1024 * 1024   # 4 MB
_BT_LOG_BACKUPS = 5
_bt_log_lock = threading.Lock()


def _bt_rotate_if_needed() -> None:
    try:
        if os.path.getsize(BT_LOG_FILE) < _BT_LOG_MAX:
            return
    except OSError:
        return
    oldest = f"{BT_LOG_FILE}.{_BT_LOG_BACKUPS}"
    if os.path.exists(oldest):
        try:
            os.remove(oldest)
        except OSError:
            pass
    for i in range(_BT_LOG_BACKUPS - 1, 0, -1):
        a, b = f"{BT_LOG_FILE}.{i}", f"{BT_LOG_FILE}.{i + 1}"
        if os.path.exists(a):
            try:
                os.replace(a, b)
            except OSError:
                pass
    try:
        os.replace(BT_LOG_FILE, f"{BT_LOG_FILE}.1")
    except OSError:
        pass


def bt_log(msg: str, also_bot: bool = True) -> None:
    """Write to saved_data/backtest.log (and bot.log by default)."""
    if not msg.startswith("[BT"):
        msg = f"[BT] {msg}"
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        os.makedirs("saved_data", exist_ok=True)
        with _bt_log_lock:
            _bt_rotate_if_needed()
            with open(BT_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
    if also_bot:
        try:
            log_message(msg)
        except Exception:
            print(line)


def bt_log_section(title: str) -> None:
    bar = "=" * 64
    bt_log(bar, also_bot=False)
    bt_log(title, also_bot=False)
    bt_log(bar, also_bot=False)


def bt_log_run_summary(report, strategies, symbols, days, capital, risk_pct,
                       live_cfg, data_note: str, real_count: int, total_combos: int) -> None:
    """Structured end-of-run dump for tuning decisions."""
    bt_log_section("BACKTEST RUN COMPLETE")
    bt_log(f"message: {report.message}", also_bot=False)
    bt_log(
        f"universe: symbols={symbols} days={days} strategies={strategies} "
        f"capital=${capital:.0f} risk={risk_pct}%",
        also_bot=False,
    )
    bt_log(f"data: {data_note} (real_sessions={real_count}/{total_combos})", also_bot=False)
    # Live settings snapshot (key knobs only)
    keys = [
        "orb_minutes", "require_pullback", "min_breakout_rel_vol", "use_macd_filter",
        "use_adx_filter", "use_htf_filter", "atr_stop_mult", "rsi_overbought",
        "div_max_rsi_entry", "div_band_proximity", "div_use_macd_confirm",
        "scalp_dollar_per_trade", "scalp_stop_pct", "scalp_rr_ratio",
        "rsi_entry_threshold", "rsi_initial_stop_pct", "rsi_timeframe",
        "swing_min_swings", "swing_max_retrace_pct", "rr_target", "entry_cutoff_hour",
    ]
    snap = {k: live_cfg.get(k) for k in keys if k in live_cfg}
    bt_log(f"settings_snapshot: {snap}", also_bot=False)

    s = report.summary or {}
    bt_log(
        f"OVERALL: trades={s.get('trades')} W/L={s.get('wins')}/{s.get('losses')} "
        f"WR={s.get('win_rate')}% pnl=${s.get('gross_pnl')} PF={s.get('profit_factor')} "
        f"avgR={s.get('avg_r')} maxDD=${s.get('max_drawdown')}",
        also_bot=False,
    )
    for strat, st in (report.by_strategy or {}).items():
        bt_log(
            f"  STRAT {strat}: n={st.get('trades')} WR={st.get('win_rate')}% "
            f"pnl=${st.get('gross_pnl')} PF={st.get('profit_factor')} "
            f"avgR={st.get('avg_r')} avgW=${st.get('avg_win')} avgL=${st.get('avg_loss')}",
            also_bot=False,
        )
        # Exit reason breakdown
        reasons: Dict[str, int] = {}
        for t in (report.all_trades or []):
            if getattr(t, "strategy", None) != strat:
                continue
            r = getattr(t, "exit_reason", "?") or "?"
            reasons[r] = reasons.get(r, 0) + 1
        if reasons:
            bt_log(f"    exits: {reasons}", also_bot=False)
        # Last few trades for this strategy
        strat_trades = [t for t in (report.all_trades or []) if getattr(t, "strategy", None) == strat]
        for t in strat_trades[-8:]:
            bt_log(
                f"    trade {t.date} {t.symbol} entry={t.entry_price} exit={t.exit_price} "
                f"pnl=${t.pnl} R={t.r_multiple} reason={t.exit_reason} "
                f"{t.entry_time}->{t.exit_time}",
                also_bot=False,
            )

    # Per-symbol tops/bottoms
    by_sym = report.by_symbol or {}
    ranked = sorted(by_sym.items(), key=lambda kv: (kv[1] or {}).get("gross_pnl", 0) or 0, reverse=True)
    if ranked:
        bt_log("by_symbol top/bottom:", also_bot=False)
        for sym, st in ranked[:5]:
            bt_log(f"  + {sym}: n={st.get('trades')} pnl=${st.get('gross_pnl')} WR={st.get('win_rate')}%", also_bot=False)
        for sym, st in ranked[-5:]:
            if st.get("gross_pnl", 0) < 0:
                bt_log(f"  - {sym}: n={st.get('trades')} pnl=${st.get('gross_pnl')} WR={st.get('win_rate')}%", also_bot=False)

    bt_log_section("END BACKTEST RUN")
    # Also one-liner to bot.log
    bt_log(f"Run complete — see saved_data/backtest.log for full summary | {report.message}")



# ─────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    symbol:       str
    strategy:     str
    date:         str
    entry_price:  float
    exit_price:   float
    stop:         float
    target:       float
    shares:       int
    pnl:          float
    pnl_pct:      float
    r_multiple:   float
    exit_reason:  str   # 'stop' | 'target' | 'eod'
    bars_held:    int
    entry_time:   str
    exit_time:    str

    def as_dict(self) -> dict:
        return {
            "symbol":      self.symbol,
            "strategy":    self.strategy,
            "date":        self.date,
            "entry":       round(self.entry_price, 4),
            "exit":        round(self.exit_price, 4),
            "stop":        round(self.stop, 4),
            "target":      round(self.target, 4),
            "shares":      self.shares,
            "pnl":         round(self.pnl, 2),
            "pnl_pct":     round(self.pnl_pct, 2),
            "r_multiple":  round(self.r_multiple, 2),
            "exit_reason": self.exit_reason,
            "bars_held":   self.bars_held,
            "entry_time":  self.entry_time,
            "exit_time":   self.exit_time,
        }


@dataclass
class BacktestReport:
    params:         dict       = field(default_factory=dict)
    status:         str        = "idle"   # idle | running | complete | error
    progress:       float      = 0.0
    message:        str        = ""
    all_trades:     List[BacktestTrade] = field(default_factory=list)
    by_strategy:    Dict[str, dict] = field(default_factory=dict)
    by_symbol:      Dict[str, dict] = field(default_factory=dict)
    summary:        dict       = field(default_factory=dict)
    elapsed_sec:    float      = 0.0

    def as_dict(self) -> dict:
        return {
            "params":      self.params,
            "status":      self.status,
            "progress":    round(self.progress, 3),
            "message":     self.message,
            "all_trades":  [t.as_dict() for t in self.all_trades],
            "by_strategy": self.by_strategy,
            "by_symbol":   self.by_symbol,
            "summary":     self.summary,
            "elapsed_sec": round(self.elapsed_sec, 1),
        }


# ─────────────────────────────────────────────────────────────────
# Candle generation helpers
# ─────────────────────────────────────────────────────────────────

# ── Realistic execution costs ────────────────────────────────────────────────
# Each trade pays slippage on entry + exit (long: buy high / sell low)
# plus a flat commission.  Adjust these to match your actual broker.
_SLIPPAGE_PCT = 0.0005   # 0.05% per side  (10 cents on a $100 stock)
_COMMISSION   = 1.00     # $1.00 flat per trade (round-trip)

# ── Real market data cache ────────────────────────────────────────────────────
# Populated by _prefetch_real_data() at the start of each backtest run.
# Key: (SYMBOL_UPPER, 'YYYY-MM-DD')  Value: list[dict] or None (unavailable)
_real_candle_cache: Dict[tuple, object] = {}

# Daily OHLCV cache — populated by _prefetch_daily_data(), used to anchor
# synthetic intraday generation for dates beyond the 5m/1m availability window.
# Key: SYMBOL_UPPER  Value: dict[date_str -> {open,high,low,close,vol}]
_daily_ohlcv_cache: Dict[str, Dict[str, Dict]] = {}

def _trading_dates_back(n_days: int) -> List[datetime.date]:
    """Return the last n_days weekdays ending today (ET), most-recent last."""
    today = datetime.datetime.now(_ET).date()
    dates = []
    d = today
    while len(dates) < n_days:
        if d.weekday() < 5:   # Mon-Fri
            dates.append(d)
        d -= datetime.timedelta(days=1)
    return list(reversed(dates))


def _prefetch_daily_data(symbols: List[str], n_days: int = 365) -> None:
    """Fetch up to n_days of daily OHLCV from Yahoo Finance for each symbol.
    Populates _daily_ohlcv_cache.  Daily data is available for years of history,
    providing real price levels and direction for the anchored intraday generator.
    """
    global _daily_ohlcv_cache
    try:
        import yfinance as yf
    except ImportError:
        return
    # Add buffer so we have enough bars including weekends/holidays
    period_days = max(n_days + 60, 400)
    for sym in symbols:
        upper = sym.upper()
        if upper in _daily_ohlcv_cache:
            continue
        try:
            ticker = yf.Ticker(upper)
            df = ticker.history(period=f"{period_days}d", interval="1d",
                                auto_adjust=True, prepost=False)
            if df is None or df.empty:
                _daily_ohlcv_cache[upper] = {}
                continue
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert(_ET)
            daily: Dict[str, Dict] = {}
            for ts, row in df.iterrows():
                date_str = ts.date().isoformat()
                o = float(row["Open"])
                if o <= 0:
                    continue
                daily[date_str] = {
                    "open":  round(o, 4),
                    "high":  round(float(row["High"]), 4),
                    "low":   round(float(row["Low"]),  4),
                    "close": round(float(row["Close"]), 4),
                    "vol":   max(1, int(row["Volume"])),
                }
            _daily_ohlcv_cache[upper] = daily
            bt_log(f"[BT] {upper}: {len(daily)} daily bars cached")
        except Exception as exc:
            _daily_ohlcv_cache[upper] = {}
            bt_log(f"[BT] Daily fetch error for {upper}: {exc}")


def _gen_day_from_daily_ohlcv(
    symbol: str,
    trade_date: datetime.date,
    d_open: float, d_high: float, d_low: float, d_close: float, d_vol: int,
) -> List[Dict]:
    """Generate synthetic 1-min candles anchored to REAL daily OHLCV.

    Guarantees:
      • Opening bar opens at d_open
      • Closing bar closes at d_close
      • Intraday path visits d_high and d_low
      • Volatility scaled to actual (d_high - d_low) range
      • Day direction (up/down/chop) from real move_pct
    """
    seed = int(hashlib.md5(
        f"{symbol}|{trade_date.isoformat()}|anchored".encode()
    ).hexdigest()[:8], 16)
    rng = random.Random(seed)

    move_pct  = (d_close - d_open) / d_open if d_open > 0 else 0.0
    range_pct = (d_high  - d_low)  / d_open if d_open > 0 else 0.01
    # One-bar volatility estimate: ~10% of daily range
    bar_sigma = max(d_open * 0.001, (d_high - d_low) * 0.08)

    # ORB period: consolidates in a tight band around open (25% of daily range)
    orb_half = (d_high - d_low) * rng.uniform(0.15, 0.30)
    orb_hi = min(d_high, d_open + orb_half)
    orb_lo = max(d_low,  d_open - orb_half)

    # Time bars where daily high/low are hit (deterministic per seed)
    if move_pct >= 0.005:           # up day: high early, low near close
        high_bar = rng.randint(2, 12)
        low_bar  = rng.randint(15, 25) if rng.random() > 0.3 else rng.randint(0, 1)
    elif move_pct <= -0.005:        # down day: low early, high near open
        low_bar  = rng.randint(2, 12)
        high_bar = rng.randint(0, 1)
    else:                           # chop: scattered
        high_bar = rng.randint(3, 20)
        low_bar  = rng.randint(3, 20)
        if abs(high_bar - low_bar) < 2:
            low_bar = (high_bar + 7) % 25

    market_open = datetime.datetime(
        trade_date.year, trade_date.month, trade_date.day, 9, 30, 0, tzinfo=_ET)

    price = d_open
    candles = []
    for i in range(26):              # 26 × 15-min bars = 9:30 → 16:00
        t = market_open + datetime.timedelta(minutes=i * 15)
        progress = i / 25.0

        # Force high/low at their designated bars
        if i == high_bar:
            price = d_high
        elif i == low_bar:
            price = d_low
        elif i == 25:
            price = d_close
        else:
            # Post-ORB push at bar 1 (9:45) in direction of day
            if i == 1:
                if move_pct >= 0.005:
                    price = min(d_high, orb_hi + bar_sigma * rng.uniform(0.5, 2.0))
                elif move_pct <= -0.005:
                    price = max(d_low, orb_lo - bar_sigma * rng.uniform(0.5, 2.0))
            # Drift toward target + noise
            target = d_open + (d_close - d_open) * progress
            drift  = (target - price) * 0.12 + rng.gauss(0, bar_sigma)
            price  = max(d_low, min(d_high, price + drift))

        # Build OHLC bar around price
        spread = max(0.001, abs(rng.gauss(0, bar_sigma * 0.6)))
        o = round(max(d_low, min(d_high, price + rng.uniform(-spread * 0.5, spread * 0.5))), 4)
        h = round(min(d_high, price + abs(rng.gauss(0, spread))), 4)
        l = round(max(d_low,  price - abs(rng.gauss(0, spread))), 4)
        c = round(price, 4)
        h = max(h, o, c)
        l = min(l, o, c)
        # Volume: U-shaped (higher at open/close)
        vol_w = 2.5 if i <= 1 else (1.5 if i >= 23 else 1.0)
        vol = max(100, int(d_vol / 26 * rng.uniform(0.4, 2.0) * vol_w))
        candles.append({
            "datetime": int(t.timestamp() * 1000),
            "open": o, "high": h, "low": l, "close": c, "volume": vol,
        })
    return candles


def _clear_real_15m_cache() -> None:
    """Drop per-run so each backtest re-fetches the latest historic window."""
    global _real_candle_cache
    _real_candle_cache = {}


def _df_to_rth_15m_by_date(df) -> Dict[str, List[Dict]]:
    """Split a Yahoo 15m dataframe into RTH day buckets (ET)."""
    out: Dict[str, List[Dict]] = {}
    if df is None or getattr(df, "empty", True):
        return out
    if df.index.tzinfo is None:
        df = df.copy()
        df.index = df.index.tz_localize("UTC").tz_convert(_ET)
    else:
        df = df.copy()
        df.index = df.index.tz_convert(_ET)
    for d in sorted(set(df.index.date)):
        day_df = df[df.index.date == d]
        mask = (
            (day_df.index.time >= datetime.time(9, 30))
            & (day_df.index.time <= datetime.time(15, 45))
        )
        day_df = day_df[mask]
        if len(day_df) < 5:
            continue
        candles = [
            {
                "datetime": int(ts.timestamp() * 1000),
                "open":   round(float(row["Open"]), 4),
                "high":   round(float(row["High"]), 4),
                "low":    round(float(row["Low"]), 4),
                "close":  round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            }
            for ts, row in day_df.iterrows()
        ]
        out[d.isoformat()] = candles
    return out


def _prefetch_real_data(symbols: List[str], dates: List[datetime.date]) -> None:
    """Fetch the maximum available real 15-min history from Yahoo Finance.

    Yahoo limits 15m data to ~60 calendar days. We always pull that full window
    for each symbol (plus any requested dates inside it) so RSI/swing/ORB share
    the same historic candles. Older dates stay None → daily-anchored synthetic.
    """
    global _real_candle_cache
    try:
        import yfinance as yf
    except ImportError:
        bt_log("[BT] yfinance not available — all days will use synthetic data")
        return

    today = datetime.date.today()
    # Yahoo 15m window is ~59–60 days; keep a small safety margin
    yf_start = today - datetime.timedelta(days=59)
    # Union of requested dates and the full Yahoo window so warm-up days exist
    want = set(dates)
    d = yf_start
    while d <= today:
        if d.weekday() < 5:
            want.add(d)
        d += datetime.timedelta(days=1)

    for sym in symbols:
        upper = sym.upper()
        # Skip if we already have any real bars for this symbol in-window
        have_real = any(
            isinstance(_real_candle_cache.get((upper, dd.isoformat())), list)
            for dd in want
            if dd >= yf_start
        )
        if have_real:
            # Still mark out-of-window requested dates
            for dd in dates:
                if dd < yf_start and (upper, dd.isoformat()) not in _real_candle_cache:
                    _real_candle_cache[(upper, dd.isoformat())] = None
            continue

        for dd in want:
            if dd < yf_start:
                _real_candle_cache.setdefault((upper, dd.isoformat()), None)

        try:
            ticker = yf.Ticker(upper)
            # Single wide pull for the full available 15m window
            df = ticker.history(
                start=yf_start,
                end=today + datetime.timedelta(days=2),
                interval="15m",
                auto_adjust=True,
                prepost=False,
            )
            by_day = _df_to_rth_15m_by_date(df)
            if not by_day:
                bt_log(f"[BT] {upper}: no 15m historic data — synthetic fallback")
                for dd in want:
                    if dd >= yf_start:
                        _real_candle_cache.setdefault((upper, dd.isoformat()), None)
                continue
            n_bars = 0
            for ds, candles in by_day.items():
                _real_candle_cache[(upper, ds)] = candles
                n_bars += len(candles)
            # Mark trading days in window with no bars as unavailable
            for dd in want:
                if dd >= yf_start and (upper, dd.isoformat()) not in _real_candle_cache:
                    _real_candle_cache[(upper, dd.isoformat())] = None
            bt_log(f"[BT] {upper}: historic 15m loaded — {len(by_day)} sessions, "
                f"{n_bars} bars (window {yf_start} → {today})"
            )
        except Exception as exc:
            bt_log(f"[BT] 15m historic fetch error for {upper}: {exc}")
            for dd in want:
                if dd >= yf_start:
                    _real_candle_cache.setdefault((upper, dd.isoformat()), None)


def _continuous_15m(symbol: str, dates: List[datetime.date],
                    candles_db: Dict[str, Dict[str, List[Dict]]]) -> List[Dict]:
    """Flatten day buckets into one chronological 15m series for a symbol."""
    bars: List[Dict] = []
    for d in sorted(dates, key=lambda x: x.isoformat()):
        day = candles_db.get(symbol, {}).get(d.isoformat()) or []
        bars.extend(day)
    bars.sort(key=lambda b: b.get("datetime", 0))
    return bars


def _gen_day_candles(
    symbol: str,
    trade_date: datetime.date,
    prev_close: Optional[float] = None,
) -> List[Dict]:
    """Generate a full day of deterministic 15-min candles for one trading date (26 bars).

    Seed = symbol + date so results are reproducible.  Five day types are chosen
    deterministically from the hash so each symbol/date combo is fixed:
      35% trend_up   — clean ORB breakout with sustained follow-through
      20% chop       — ranging open, no sustained breakout, mean-reverts
      15% trend_down — opens weak, breaks below ORB low, grinds lower
      15% reversal   — initial ORB breakout then sharp reversal (trap)
      15% moderate   — weak breakout, marginal follow-through
    """
    # ── Real data: check cache populated by _prefetch_real_data ──
    _cache_hit = _real_candle_cache.get((symbol.upper(), trade_date.isoformat()), "MISS")
    if _cache_hit != "MISS" and _cache_hit is not None:
        return _cache_hit  # type: ignore[return-value]
    # None in cache means real data was unavailable — try daily-anchored next

    # ── Daily-anchored synthetic: use real price levels + direction ──
    date_str = trade_date.isoformat()
    _daily_day = _daily_ohlcv_cache.get(symbol.upper(), {}).get(date_str)
    if _daily_day:
        return _gen_day_from_daily_ohlcv(
            symbol, trade_date,
            _daily_day["open"], _daily_day["high"],
            _daily_day["low"],  _daily_day["close"],
            _daily_day["vol"],
        )

    digest   = hashlib.md5(f"{symbol}|{date_str}".encode()).hexdigest()
    rng      = random.Random(int(digest[:8], 16))

    # Price base: keep in realistic $5-$50 range to prevent $0.50 drift
    if prev_close is not None and 2.0 < prev_close < 80.0:
        base = prev_close
    else:
        base_digest = hashlib.md5(f"{symbol}|base".encode()).hexdigest()
        base = round(random.Random(int(base_digest[:8], 16)).uniform(5.0, 30.0), 2)

    # Day type — separate hash so it's independent of daily drift seed
    type_val = int(hashlib.md5(f"{symbol}|{date_str}|type".encode()).hexdigest()[:4], 16) % 100
    if type_val < 35:
        day_type = "trend_up"
    elif type_val < 55:
        day_type = "chop"
    elif type_val < 70:
        day_type = "trend_down"
    elif type_val < 85:
        day_type = "reversal"
    else:
        day_type = "moderate"

    # Post-ORB directional bias per day type
    if day_type == "trend_up":
        post_bias    = rng.uniform(0.004, 0.009)   # per 15-min bar
        reversal_bar = 9999
    elif day_type == "trend_down":
        post_bias    = rng.uniform(-0.009, -0.004)
        reversal_bar = 9999
    elif day_type == "chop":
        post_bias    = rng.uniform(-0.002, 0.002)
        reversal_bar = 9999
    elif day_type == "reversal":
        post_bias    = rng.uniform(0.004, 0.008)   # looks bullish early
        reversal_bar = rng.randint(3, 7)            # bar 3–7 = 45 min–1h45 after open
    else:  # moderate
        post_bias    = rng.uniform(0.002, 0.004)
        reversal_bar = 9999

    market_open  = datetime.datetime(
        trade_date.year, trade_date.month, trade_date.day, 9, 30, 0, tzinfo=_ET)

    price = base
    candles = []
    for i in range(26):              # 26 × 15-min bars = 9:30 → 16:00
        t = market_open + datetime.timedelta(minutes=i * 15)

        if i < 1:
            # ORB bar (9:30–9:45): tight consolidation, elevated open volume
            drift    = rng.uniform(-0.003, 0.003)
            vol_mult = 2.5
        elif i < 3:
            # Post-ORB trigger (bars 1–2 = 9:45–10:15)
            if day_type == "trend_up":
                drift = rng.uniform(0.010, 0.022)
            elif day_type == "trend_down":
                drift = rng.uniform(-0.020, -0.008)
            elif day_type == "chop":
                drift = rng.uniform(-0.005, 0.005)   # ambiguous
            elif day_type == "reversal":
                drift = rng.uniform(0.008, 0.018)    # looks bullish
            else:  # moderate
                drift = rng.uniform(0.003, 0.009)
            vol_mult = rng.uniform(2.0, 4.0) if i == 1 else 1.2
        elif i >= reversal_bar:
            # Reversal: sharp counter-move
            drift    = rng.uniform(-0.014, -0.005)
            vol_mult = rng.uniform(1.5, 2.5)
        elif i < 15:
            drift    = post_bias + rng.uniform(-0.006, 0.006)
            vol_mult = 1.0
        else:
            # Late-day drift toward VWAP
            drift    = -post_bias * 0.4 + rng.uniform(-0.004, 0.004)
            vol_mult = 0.9

        price = max(base * 0.40, price * (1 + drift))
        vol   = int(rng.uniform(600_000, 2_400_000) * vol_mult)

        o = round(price * (1 + rng.uniform(-0.002, 0.002)), 4)
        h = round(price * (1 + rng.uniform(0.001, 0.007)), 4)
        l = round(price * (1 - rng.uniform(0.001, 0.007)), 4)
        c = round(price, 4)
        h = max(h, o, c)
        l = min(l, o, c)
        candles.append({
            "datetime": int(t.timestamp() * 1000),
            "open": o, "high": h, "low": l, "close": c, "volume": vol,
        })

    return candles


def _resample_30min(candles_1min: List[Dict]) -> List[Dict]:
    """Resample 1-min candles to 30-min bars."""
    buckets: Dict[int, List[Dict]] = {}
    for c in candles_1min:
        ts = datetime.datetime.fromtimestamp(c["datetime"] / 1000, tz=_ET)
        bucket_min = (ts.hour * 60 + ts.minute) // 30 * 30
        bucket_ts  = int(datetime.datetime(
            ts.year, ts.month, ts.day,
            bucket_min // 60, bucket_min % 60, tzinfo=_ET
        ).timestamp() * 1000)
        buckets.setdefault(bucket_ts, []).append(c)

    result = []
    for ts_ms in sorted(buckets):
        bars = buckets[ts_ms]
        result.append({
            "datetime": ts_ms,
            "open":     bars[0]["open"],
            "high":     max(b["high"] for b in bars),
            "low":      min(b["low"]  for b in bars),
            "close":    bars[-1]["close"],
            "volume":   sum(b["volume"] for b in bars),
        })
    return result


# ─────────────────────────────────────────────────────────────────
# Indicator helpers (pure, no network calls)
# ─────────────────────────────────────────────────────────────────

def _compute_ema(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return round(ema, 6)


def _compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - 100 / (1 + rs), 2)


def _compute_macd(closes: List[float]) -> Optional[Dict]:
    if len(closes) < 35:
        return None
    def _ema_series(vals, p):
        k = 2.0 / (p + 1)
        e = sum(vals[:p]) / p
        out = [e]
        for v in vals[p:]:
            e = v * k + e * (1 - k)
            out.append(e)
        return out
    e12 = _ema_series(closes, 12)
    e26 = _ema_series(closes, 26)
    n = min(len(e12), len(e26))
    macd_line = [e12[len(e12) - n + i] - e26[len(e26) - n + i] for i in range(n)]
    if len(macd_line) < 9:
        return None
    sig_val = sum(macd_line[-9:]) / 9
    hist = macd_line[-1] - sig_val
    return {"macd": round(macd_line[-1], 6), "signal": round(sig_val, 6), "hist": round(hist, 6)}


def _compute_atr(candles: List[Dict], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        prev_c = candles[i - 1]["close"]
        h, l = candles[i]["high"], candles[i]["low"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 6)


def _compute_vwap(candles: List[Dict]) -> Optional[float]:
    cum_pv = cum_v = 0.0
    for c in candles:
        typ = (c["high"] + c["low"] + c["close"]) / 3.0
        vol = c.get("volume", 0)
        cum_pv += typ * vol
        cum_v  += vol
    return round(cum_pv / cum_v, 4) if cum_v > 0 else None


def _compute_indicators(candles: List[Dict], orb_high: Optional[float] = None) -> Dict:
    closes = [c["close"] for c in candles]
    vols   = [c.get("volume", 0) for c in candles]
    avg_vol = sum(vols) / len(vols) if vols else 0

    bvr = 0.0
    if orb_high and avg_vol > 0:
        for c in candles:
            if c["close"] > orb_high:
                bvr = max(bvr, c["volume"] / avg_vol)

    # Build simple 5-min closes for HTF filter
    htf_closes: List[float] = []
    buf: List[Dict] = []
    for idx, c in enumerate(candles):
        buf.append(c)
        if (idx + 1) % 5 == 0:
            htf_closes.append(buf[-1]["close"])
            buf = []

    htf_ema9  = _compute_ema(htf_closes, 9)
    htf_ema20 = _compute_ema(htf_closes, 20)
    htf_up    = (htf_ema9 > htf_ema20) if (htf_ema9 and htf_ema20) else None

    macd = _compute_macd(closes)
    return {
        "ema9":               _compute_ema(closes, 9),
        "ema20":              _compute_ema(closes, 20),
        "rsi":                _compute_rsi(closes, 14),
        "atr":                _compute_atr(candles, 14),
        "vwap":               _compute_vwap(candles),
        "last_closed_close":  closes[-1] if closes else 0,
        "breakout_vol_ratio": round(bvr, 2),
        "macd":               (macd or {}).get("macd"),
        "macd_signal":        (macd or {}).get("signal"),
        "macd_hist":          (macd or {}).get("hist"),
        "adx":                None,
        "htf_uptrend":        htf_up,
        "htf_sma10":          None,
        "bars_5m":            [],
        "candles":            list(candles),
    }


# ─────────────────────────────────────────────────────────────────
# Trade recording helper
# ─────────────────────────────────────────────────────────────────

def _make_trade(pos: Dict, exit_price: float, exit_reason: str, exit_bar: int) -> BacktestTrade:
    entry  = pos["entry"]
    stop   = pos["stop"]
    shares = pos["shares"]
    # Apply realistic execution costs: 0.05% slippage each side + $1 commission
    # Long entry: filled slightly above signal price
    # Long exit: filled slightly below exit price (whether stop or target)
    entry_fill = entry * (1 + _SLIPPAGE_PCT)
    exit_fill  = exit_price * (1 - _SLIPPAGE_PCT)
    pnl    = (exit_fill - entry_fill) * shares - _COMMISSION
    risk   = abs((entry - stop) * shares) if (entry - stop) != 0 else 1
    r_mul  = pnl / risk if risk > 0 else 0.0
    pnl_pct = ((exit_fill - entry_fill) / entry_fill * 100) if entry_fill > 0 else 0.0
    return BacktestTrade(
        symbol      = pos["symbol"],
        strategy    = pos["strategy"],
        date        = pos["date"],
        entry_price = round(entry_fill, 4),
        exit_price  = round(exit_fill, 4),
        stop        = stop,
        target      = pos["target"],
        shares      = shares,
        pnl         = round(pnl, 2),
        pnl_pct     = round(pnl_pct, 2),
        r_multiple  = round(r_mul, 2),
        exit_reason = exit_reason,
        bars_held   = exit_bar - pos["entry_bar"],
        entry_time  = pos["entry_time"],
        exit_time   = pos.get("exit_time", ""),
    )


# ─────────────────────────────────────────────────────────────────
# Per-strategy backtest runners
# ─────────────────────────────────────────────────────────────────

def _run_orb_day(
    symbol: str,
    date: str,
    candles_1min: List[Dict],
    strategy,
    orb_minutes: int = 15,
    capital: float = 10000.0,
) -> List[BacktestTrade]:
    trades: List[BacktestTrade] = []
    positions: Dict[str, Dict] = {}
    orb_high = orb_low = None
    # Set sim clock to the start of this trading day BEFORE reset_day()
    # so the strategy's _trade_date is set correctly and won't re-clear
    # ORB levels inside evaluate().
    if candles_1min:
        set_sim_now(datetime.datetime.fromtimestamp(
            candles_1min[0]["datetime"] / 1000, tz=_ET))
    strategy.reset_day()

    for i, bar in enumerate(candles_1min):
        bar_time = datetime.datetime.fromtimestamp(bar["datetime"] / 1000, tz=_ET)
        set_sim_now(bar_time)
        price = bar["close"]
        hm = bar_time.hour * 100 + bar_time.minute

        # Check exits
        for sym in list(positions.keys()):
            pos = positions[sym]
            exit_price = exit_reason = None
            if bar["low"] <= pos["stop"]:
                exit_price, exit_reason = pos["stop"], "stop"
            elif bar["high"] >= pos["target"]:
                exit_price, exit_reason = pos["target"], "target"
            elif hm >= 1555:
                exit_price, exit_reason = price, "eod"
            if exit_price is not None:
                pos["exit_time"] = bar_time.strftime("%H:%M")
                trade = _make_trade(pos, exit_price, exit_reason, i)
                trades.append(trade)
                if hasattr(strategy, "mark_trade_closed"):  # ScalpStrategy only
                    strategy.mark_trade_closed(trade.pnl)
                del positions[sym]
                try:
                    strategy.record_exit(sym, exit_price, exit_reason)
                except Exception:
                    pass

        # Set ORB levels
        if i == orb_minutes - 1 and orb_high is None:
            orb_high = max(c["high"] for c in candles_1min[:orb_minutes])
            orb_low  = min(c["low"]  for c in candles_1min[:orb_minutes])
            strategy.set_orb_levels(symbol, {"high": orb_high, "low": orb_low, "mid": round((orb_high + orb_low) / 2, 4)})

        if i < orb_minutes or orb_high is None:
            continue

        ind = _compute_indicators(candles_1min[:i + 1], orb_high)
        strategy.set_indicators(symbol, ind)

        if symbol not in positions:
            # For ORB breakout: if intrabar high clears orb_high, evaluate at
            # the orb_high level (standard breakout entry practice).
            eval_price = price
            if orb_high and bar["high"] > orb_high and price <= orb_high:
                eval_price = round(orb_high * 1.001, 4)
            try:
                signal = strategy.evaluate(symbol, eval_price)
            except Exception:
                signal = None
            if signal:
                try:
                    shares, _risk, _ = strategy.calc_position_size(
                        signal.entry_price, signal.stop_price)
                except Exception:
                    shares = 1
                # Cap: max 20% of capital per position
                if signal.entry_price > 0:
                    shares = min(shares, max(1, int(capital * 0.20 / signal.entry_price)))
                if shares > 0:
                    positions[symbol] = {
                        "symbol":    symbol,
                        "strategy":  "orb",
                        "date":      date,
                        "entry":     signal.entry_price,
                        "stop":      signal.stop_price,
                        "target":    signal.target_r2,
                        "shares":    shares,
                        "entry_bar": i,
                        "entry_time": bar_time.strftime("%H:%M"),
                        "exit_time":  "",
                    }
                    try:
                        strategy.record_entry(signal, f"BT-ORB-{i:04d}")
                    except Exception:
                        pass

    # EOD flatten
    for sym, pos in positions.items():
        ep = candles_1min[-1]["close"]
        pos["exit_time"] = "16:00"
        trades.append(_make_trade(pos, ep, "eod", len(candles_1min)))
        try:
            strategy.record_exit(sym, ep, "eod")
        except Exception:
            pass

    return trades


def _run_scalp_day(
    symbol: str,
    date: str,
    candles_1min: List[Dict],
    strategy,
    capital: float,
) -> List[BacktestTrade]:
    trades: List[BacktestTrade] = []
    positions: Dict[str, Dict] = {}

    for i, bar in enumerate(candles_1min):
        bar_time = datetime.datetime.fromtimestamp(bar["datetime"] / 1000, tz=_ET)
        set_sim_now(bar_time)
        price = bar["close"]
        hm = bar_time.hour * 100 + bar_time.minute

        for sym in list(positions.keys()):
            pos = positions[sym]
            exit_price = exit_reason = None
            if bar["low"] <= pos["stop"]:
                exit_price, exit_reason = pos["stop"], "stop"
            elif bar["high"] >= pos["target"]:
                exit_price, exit_reason = pos["target"], "target"
            elif hm >= 1555:
                exit_price, exit_reason = price, "eod"
            if exit_price is not None:
                pos["exit_time"] = bar_time.strftime("%H:%M")
                trade = _make_trade(pos, exit_price, exit_reason, i)
                trades.append(trade)
                if hasattr(strategy, "mark_trade_closed"):  # ScalpStrategy only
                    strategy.mark_trade_closed(trade.pnl)
                del positions[sym]

        if i < 2:
            continue

        ind = _compute_indicators(candles_1min[:i + 1])
        ind["candles"] = candles_1min[:i + 1]

        # ORB-alignment filter: only scalp above the first bar's high (day uptrend)
        orb_hi_0 = candles_1min[0]["high"] if candles_1min else 0.0
        if symbol not in positions and price > orb_hi_0 and bar["close"] >= bar["open"]:
            try:
                scalp_sig = strategy.evaluate(symbol, ind, {}, price, capital)
            except Exception:
                scalp_sig = None
            if scalp_sig:
                entry  = getattr(scalp_sig, "entry_price", price)
                stop   = getattr(scalp_sig, "stop_price", price * 0.985)
                target = getattr(scalp_sig, "target_r2", price * 1.01)
                # Use the strategy's own sizing ($200/price, capped at 25% of capital)
                shares = max(1, getattr(scalp_sig, "shares", 1))
                positions[symbol] = {
                    "symbol":    symbol,
                    "strategy":  "scalp",
                    "date":      date,
                    "entry":     entry,
                    "stop":      stop,
                    "target":    target,
                    "shares":    shares,
                    "entry_bar": i,
                    "entry_time": bar_time.strftime("%H:%M"),
                    "exit_time":  "",
                }

    for sym, pos in positions.items():
        ep = candles_1min[-1]["close"]
        pos["exit_time"] = "16:00"
        trades.append(_make_trade(pos, ep, "eod", len(candles_1min)))

    return trades


def _run_scalp_orb_day(
    symbol: str,
    date: str,
    candles_15m: List[Dict],
    capital: float,
    dollar_per_trade: float = 200.0,
    stop_pct: float = 1.5,
    rr_ratio: float = 2.0,
    session_budget: float = 500.0,
) -> List[BacktestTrade]:
    """ORB-momentum scalp on 15-min bars using live scalp sizing/stop/RR.

    Entry rule: close of bar-1 (9:45) must clear bar-0 high (ORB high)
    with a bullish body and meaningful range.  One trade per day only.
    Stop / target / size come from scalp tab settings when provided.
    """
    if len(candles_15m) < 2:
        return []

    orb_high = candles_15m[0]["high"]
    orb_low  = candles_15m[0]["low"]
    bar1     = candles_15m[1]
    b1_time  = datetime.datetime.fromtimestamp(bar1["datetime"] / 1000, tz=_ET)
    set_sim_now(b1_time)

    # Entry conditions — bar 1 must be a quality bullish breakout
    orb_range = orb_high - orb_low
    if bar1["close"] <= orb_high:
        return []  # no breakout
    if bar1["close"] <= bar1["open"]:
        return []  # must be bullish body
    bar1_range = bar1["high"] - bar1["low"]
    if bar1_range < bar1["close"] * 0.003:
        return []  # reject doji / tiny bars
    # Quality filter 1: ORB must have real range (≥ 0.5% of price — excludes gap-open chop)
    if orb_range < orb_high * 0.005:
        return []  # skip tight-range open days
    # Quality filter 2: don't chase — bar-1 close must be within 2% above ORB high
    if bar1["close"] > orb_high * 1.020:
        return []  # too extended above ORB, likely missed the move
    # Quality filter 3: bar-1 body must be ≥ 40% of bar-1 range (reject small-body breakouts)
    body = abs(bar1["close"] - bar1["open"])
    if bar1_range > 0 and body / bar1_range < 0.40:
        return []  # weak-body breakout, likely to reverse

    entry = bar1["close"]
    # Live scalp stop % (preferred); floor with ORB low when tighter risk is ok
    stop_dist_pct = entry * (max(stop_pct, 0.1) / 100.0)
    stop_from_orb = max(orb_low, entry * 0.96)
    stop_dist = max(stop_dist_pct, entry * 0.005)
    # Use the wider of % stop vs ORB-based stop distance so we don't ignore structure
    stop_dist = max(stop_dist, entry - stop_from_orb) if stop_from_orb < entry else stop_dist
    stop   = round(entry - stop_dist, 4)
    target = round(entry + stop_dist * max(rr_ratio, 0.5), 4)

    # Live dollar_per_trade sizing, capped at 25% of capital (matches ScalpStrategy)
    shares = max(1, int(float(dollar_per_trade) / entry)) if entry > 0 else 1
    shares = min(shares, max(1, int(capital * 0.25 / entry))) if entry > 0 else shares

    pos = {
        "symbol":     symbol,    "strategy":  "scalp",
        "date":       date,      "entry":     entry,
        "stop":       stop,      "target":    target,
        "shares":     shares,    "entry_bar": 1,
        "entry_time": b1_time.strftime("%H:%M"),
        "exit_time":  "",
    }

    for i in range(2, len(candles_15m)):
        bar     = candles_15m[i]
        bt      = datetime.datetime.fromtimestamp(bar["datetime"] / 1000, tz=_ET)
        set_sim_now(bt)
        hm      = bt.hour * 100 + bt.minute
        exit_p  = exit_r = None
        if bar["low"] <= pos["stop"]:
            exit_p, exit_r = pos["stop"], "stop"
        elif bar["high"] >= pos["target"]:
            exit_p, exit_r = pos["target"], "target"
        elif hm >= 1555:
            exit_p, exit_r = bar["close"], "eod"
        if exit_p is not None:
            pos["exit_time"] = bt.strftime("%H:%M")
            return [_make_trade(pos, exit_p, exit_r, i)]

    pos["exit_time"] = "16:00"
    return [_make_trade(pos, candles_15m[-1]["close"], "eod", len(candles_15m))]


def _run_div_day(
    symbol: str,
    date_str: str,
    eval_date: datetime.date,
    all_days_1min: Dict[str, List[Dict]],
    strategy,
    sim_client,
) -> List[BacktestTrade]:
    """Divergence strategy operates on 30-min bars across multiple days.
    We inject pre-generated candle history into the strategy cache and
    evaluate at 10:00 AM ET on eval_date.
    """
    trades: List[BacktestTrade] = []

    all_30min: List[Dict] = []
    for d_str in sorted(all_days_1min.keys()):
        if d_str <= date_str:
            all_30min.extend(_resample_30min(all_days_1min[d_str]))
    if len(all_30min) < strategy.bb_period + 10:
        return []

    eval_dt = datetime.datetime(
        eval_date.year, eval_date.month, eval_date.day, 10, 0, 0, tzinfo=_ET)
    set_sim_now(eval_dt)

    # Inject directly into strategy cache — bypasses network fetch entirely
    strategy._candle_cache[symbol] = (float("inf"), all_30min)

    price = all_30min[-1]["close"] if all_30min else 0.0
    strategy.reset_day()

    try:
        signal = strategy.evaluate(symbol, {}, {}, price, sim_client)
    except Exception as e:
        bt_log(f"[BT-DIV] {symbol} evaluate error: {e}")
        signal = None

    if signal:
        entry  = signal.entry_price
        stop   = signal.stop_price
        target = getattr(signal, "target_r2", entry + (entry - stop) * 2.0)
        # Use signal.shares (already risk-capped by strategy)
        shares = max(1, getattr(signal, "shares", 1))

        # Simulate exit on same-day 30-min bars after entry
        day_30min = _resample_30min(all_days_1min.get(date_str, []))
        remaining = [
            c for c in day_30min
            if datetime.datetime.fromtimestamp(
                c["datetime"] / 1000, tz=_ET).hour >= 10
        ]
        exit_price = price
        exit_reason = "eod"
        exit_bar_idx = len(remaining)
        for j, bar in enumerate(remaining):
            if bar["low"] <= stop:
                exit_price, exit_reason, exit_bar_idx = stop, "stop", j
                break
            elif bar["high"] >= target:
                exit_price, exit_reason, exit_bar_idx = target, "target", j
                break
        else:
            exit_price = remaining[-1]["close"] if remaining else price

        pnl = (exit_price - entry) * shares
        risk = abs((entry - stop) * shares)
        r_mul = pnl / risk if risk > 0 else 0.0
        exit_time = "16:00"
        if exit_bar_idx < len(remaining):
            exit_time = datetime.datetime.fromtimestamp(
                remaining[exit_bar_idx]["datetime"] / 1000, tz=_ET
            ).strftime("%H:%M")

        trades.append(BacktestTrade(
            symbol=symbol, strategy="div", date=date_str,
            entry_price=round(entry, 4), exit_price=round(exit_price, 4),
            stop=round(stop, 4), target=round(target, 4),
            shares=shares, pnl=round(pnl, 2),
            pnl_pct=round((exit_price - entry) / entry * 100, 2) if entry > 0 else 0.0,
            r_multiple=round(r_mul, 2), exit_reason=exit_reason,
            bars_held=exit_bar_idx, entry_time="10:00", exit_time=exit_time,
        ))

    strategy._candle_cache.pop(symbol, None)
    return trades


# ─────────────────────────────────────────────────────────────────
# Summary statistics
# ─────────────────────────────────────────────────────────────────

def _compute_stats(trades: List[BacktestTrade]) -> Dict:
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "gross_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "profit_factor": 0.0, "avg_r": 0.0, "max_drawdown": 0.0,
        }
    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_wins = sum(t.pnl for t in wins)
    total_loss = abs(sum(t.pnl for t in losses))
    pf = total_wins / total_loss if total_loss > 0 else 999.0

    equity = peak = max_dd = 0.0
    for t in trades:
        equity += t.pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    return {
        "trades":        len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / len(trades) * 100, 1),
        "gross_pnl":     round(sum(t.pnl for t in trades), 2),
        "avg_win":       round(total_wins / len(wins), 2) if wins else 0.0,
        "avg_loss":      round(-total_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(pf, 2),
        "avg_r":         round(sum(t.r_multiple for t in trades) / len(trades), 2),
        "max_drawdown":  round(max_dd, 2),
    }



# ─────────────────────────────────────────────────────────────────
# Live strategy settings (from engine config / strategy tabs)
# ─────────────────────────────────────────────────────────────────

def _load_live_config(params: dict) -> dict:
    """Prefer live_config injected by the dashboard; fall back to config.json."""
    cfg = params.get("live_config")
    if isinstance(cfg, dict) and cfg:
        return cfg
    try:
        import json
        path = Path(__file__).resolve().parent / "saved_data" / "config.json"
        if path.is_file():
            return json.load(open(path, encoding="utf-8"))
    except Exception as e:
        bt_log(f"[BT] Could not load live config: {e}")
    return {}


def _g(cfg: dict, key: str, default):
    if key not in cfg or cfg[key] is None:
        return default
    return cfg[key]


def _orb_bars_from_live(cfg: dict, override=None) -> int:
    """Live orb_minutes is wall-clock; BT candles are 15m bars → convert."""
    if override is not None:
        try:
            return max(1, int(override))
        except Exception:
            pass
    mins = int(_g(cfg, "orb_minutes", 15) or 15)
    # 15 live minutes on a 15m chart = 1 bar
    return max(1, int(round(mins / 15.0)))


def _orb_set_config_kwargs(cfg: dict, capital: float, risk_pct: float, orb_bars: int) -> dict:
    """Map live engine/ORB tab settings onto ORBStrategy.set_config()."""
    return dict(
        account_size=float(_g(cfg, "account_size", capital) or capital),
        risk_pct=float(_g(cfg, "risk_pct", risk_pct) or risk_pct),
        orb_minutes=int(orb_bars),  # bar count for BT 15m series
        max_trades=int(_g(cfg, "max_trades_per_day", 5) or 5),
        max_concurrent_positions=int(_g(cfg, "max_concurrent_positions", 2) or 2),
        entry_cutoff_hour=int(_g(cfg, "entry_cutoff_hour", 14) or 14),
        rr_target=float(_g(cfg, "rr_target", 2.0) or 2.0),
        require_vwap_above=bool(_g(cfg, "require_vwap_above", True)),
        confirm_close=bool(_g(cfg, "confirm_close", True)),
        require_volume_confirm=bool(_g(cfg, "require_volume_confirm", True)),
        min_breakout_rel_vol=float(_g(cfg, "min_breakout_rel_vol", 3.0) or 3.0),
        use_rsi_filter=bool(_g(cfg, "use_rsi_filter", True)),
        rsi_overbought=float(_g(cfg, "rsi_overbought", 72.0) or 72.0),
        rsi_oversold=float(_g(cfg, "rsi_oversold", 25.0) or 25.0),
        use_atr_stops=bool(_g(cfg, "use_atr_stops", True)),
        atr_stop_mult=float(_g(cfg, "atr_stop_mult", 2.5) or 2.5),
        use_macd_filter=bool(_g(cfg, "use_macd_filter", True)),
        use_adx_filter=bool(_g(cfg, "use_adx_filter", True)),
        adx_min=float(_g(cfg, "adx_min", 20.0) or 20.0),
        use_htf_filter=bool(_g(cfg, "use_htf_filter", True)),
        use_macd_exit=bool(_g(cfg, "use_macd_exit", True)),
        min_stop_dist=float(_g(cfg, "min_stop_dist", 0.10) or 0.10),
        require_orb_above_vwap=bool(_g(cfg, "require_orb_above_vwap", True)),
        orb_min_range_pct=float(_g(cfg, "orb_min_range_pct", 0.5) or 0.5),
        orb_max_range_pct=float(_g(cfg, "orb_max_range_pct", 8.0) or 8.0),
        orb_max_chase_pct=float(_g(cfg, "orb_max_chase_pct", 1.5) or 1.5),
        orb_max_float=int(_g(cfg, "orb_max_float", 50_000_000) or 0),
        require_pullback=bool(_g(cfg, "require_pullback", True)),
        orb_pullback_min_pct=float(_g(cfg, "orb_pullback_min_pct", 0.2) or 0.2),
        orb_pullback_max_pct=float(_g(cfg, "orb_pullback_max_pct", 1.5) or 1.5),
        orb_pullback_reclaim_pct=float(_g(cfg, "orb_pullback_reclaim_pct", 0.1) or 0.1),
        orb_pullback_max_chase_pct=float(_g(cfg, "orb_pullback_max_chase_pct", 1.0) or 1.0),
        orb_momentum_vol_min=float(_g(cfg, "orb_momentum_vol_min", 8.0) or 8.0),
        orb_momentum_rel_vol_min=float(_g(cfg, "orb_momentum_rel_vol_min", 5.0) or 5.0),
        orb_fp_min_pole_pct=float(_g(cfg, "orb_fp_min_pole_pct", 0.8) or 0.8),
        orb_fp_max_retrace_pct=float(_g(cfg, "orb_fp_max_retrace_pct", 50.0) or 50.0),
    )


def _div_set_config_kwargs(cfg: dict, capital: float, risk_pct: float) -> dict:
    cutoff_h = int(_g(cfg, "entry_cutoff_hour", 14) or 14)
    return dict(
        enabled=True,
        account_size=float(_g(cfg, "account_size", capital) or capital),
        risk_pct=float(_g(cfg, "risk_pct", risk_pct) or risk_pct),
        resample_minutes=int(_g(cfg, "div_resample_minutes", 30) or 30),
        lookback_days=int(_g(cfg, "div_lookback_days", 10) or 10),
        bb_period=int(_g(cfg, "div_bb_period", 21) or 21),
        bb_std_mult=float(_g(cfg, "div_bb_std_mult", 1.0) or 1.0),
        div_lookback=int(_g(cfg, "div_lookback", 60) or 60),
        pivot_bars=int(_g(cfg, "div_pivot_bars", 3) or 3),
        min_rsi_div=float(_g(cfg, "div_min_rsi_div", 2.0) or 2.0),
        max_rsi_entry=float(_g(cfg, "div_max_rsi_entry", 45.0) or 45.0),
        band_proximity=float(_g(cfg, "div_band_proximity", 0.15) or 0.15),
        use_macd_confirm=bool(_g(cfg, "div_use_macd_confirm", True)),
        stop_buffer_pct=float(_g(cfg, "div_stop_buffer_pct", 0.5) or 0.5),
        min_stop_pct=float(_g(cfg, "div_min_stop_pct", 0.5) or 0.5),
        max_stop_pct=float(_g(cfg, "div_max_stop_pct", 8.0) or 8.0),
        entry_cutoff_hhmm=cutoff_h * 100,
        premarket_start_hhmm=400,
        max_hold_days=int(_g(cfg, "div_max_hold_days", 5) or 5),
        allow_short=False,
    )


def _scalp_set_config_kwargs(cfg: dict, capital: float) -> dict:
    return dict(
        enabled=True,
        symbol=str(_g(cfg, "scalp_symbol", "") or ""),
        dollar_per_trade=float(_g(cfg, "scalp_dollar_per_trade", 200.0) or 200.0),
        session_budget=float(_g(cfg, "scalp_session_budget", 500.0) or 500.0),
        target_pct=float(_g(cfg, "scalp_target_pct", 0.5) or 0.5),
        stop_pct=float(_g(cfg, "scalp_stop_pct", 1.5) or 1.5),
        use_rr_ratio=True,
        rr_ratio=float(_g(cfg, "scalp_rr_ratio", 2.0) or 2.0),
        # BT uses bar-loop evaluate path; immediate needs a symbol — keep signal mode
        immediate_mode=False,
        breakout_bars=int(_g(cfg, "scalp_breakout_bars", 5) or 5),
        candle_minutes=int(_g(cfg, "scalp_candle_minutes", 1) or 1),
        min_vol_mult=float(_g(cfg, "scalp_min_vol_mult", 1.5) or 1.5),
        rsi_min=float(_g(cfg, "scalp_rsi_min", 40.0) or 40.0),
        rsi_max=float(_g(cfg, "scalp_rsi_max", 70.0) or 70.0),
        require_vwap=True,
        require_ema=True,
        cooldown_sec=int(_g(cfg, "scalp_cooldown_sec", 60) or 60),
        max_trades=int(_g(cfg, "scalp_max_trades", 20) or 20),
        entry_cutoff_hhmm=int(_g(cfg, "entry_cutoff_hour", 14) or 14) * 100 + 0,
    )


def _scalp_orb_params(cfg: dict, capital: float) -> dict:
    """Params for _run_scalp_orb_day sized like live scalp tab."""
    return dict(
        dollar_per_trade=float(_g(cfg, "scalp_dollar_per_trade", 200.0) or 200.0),
        stop_pct=float(_g(cfg, "scalp_stop_pct", 1.5) or 1.5),
        rr_ratio=float(_g(cfg, "scalp_rr_ratio", 2.0) or 2.0),
        capital=capital,
        session_budget=float(_g(cfg, "scalp_session_budget", 500.0) or 500.0),
    )




def _rsi_set_config_kwargs(cfg: dict, capital: float, risk_pct: float) -> dict:
    return dict(
        enabled=True,
        account_size=float(_g(cfg, "account_size", capital) or capital),
        risk_pct=float(_g(cfg, "risk_pct", risk_pct) or risk_pct),
        rsi_entry_threshold=float(_g(cfg, "rsi_entry_threshold", 20.0) or 20.0),
        rsi_partial_exit=float(_g(cfg, "rsi_partial_exit", 30.0) or 30.0),
        rsi_runner_exit=float(_g(cfg, "rsi_runner_exit", 40.0) or 40.0),
        initial_stop_pct=float(_g(cfg, "rsi_initial_stop_pct", 3.0) or 3.0),
        trailing_stop_pct=float(_g(cfg, "rsi_trail_stop_pct", 5.0) or 5.0),
        trail_trigger_pct=float(_g(cfg, "rsi_trail_trigger_pct", 2.0) or 2.0),
        max_hold_days=int(_g(cfg, "rsi_max_hold_days", 10) or 10),
        scan_interval_sec=float(_g(cfg, "rsi_scan_interval_sec", 300) or 300),
        min_avg_vol=int(_g(cfg, "rsi_min_avg_vol", 300_000) or 300_000),
        min_price=float(_g(cfg, "rsi_min_price", 2.0) or 2.0),
        max_price=float(_g(cfg, "rsi_max_price", 500.0) or 500.0),
        max_trades_per_day=int(_g(cfg, "rsi_max_trades", 3) or 3),
        max_concurrent=int(_g(cfg, "rsi_max_concurrent", 3) or 3),
        rsi_timeframe="15m",  # BT always scores RSI on 15m historic candles
    )


def _swing_set_config_kwargs(cfg: dict) -> dict:
    return dict(
        max_retrace_pct=float(_g(cfg, "swing_max_retrace_pct", 40.0) or 40.0),
        min_swings=int(_g(cfg, "swing_min_swings", 3) or 3),
        min_swing_pct=float(_g(cfg, "swing_min_swing_pct", 0.5) or 0.5),
        min_avg_vol=int(_g(cfg, "swing_min_avg_vol", 100_000) or 100_000),
        candle_minutes=int(_g(cfg, "swing_candle_minutes", 1) or 1),
        min_price=float(_g(cfg, "min_price", 1.0) or 1.0),
        max_price=float(_g(cfg, "max_price", 0.0) or 0.0) or 500.0,
    )


def _daily_list_to_candles(symbol: str, up_to_date: str) -> List[Dict]:
    """Ordered daily OHLCV candles for symbol up to and including up_to_date."""
    daily = _daily_ohlcv_cache.get(symbol.upper(), {}) or {}
    out: List[Dict] = []
    for ds in sorted(daily.keys()):
        if ds > up_to_date:
            break
        b = daily[ds]
        try:
            y, m, d = (int(x) for x in ds.split("-"))
            ms = int(datetime.datetime(y, m, d, 16, 0, tzinfo=_ET).timestamp() * 1000)
        except Exception:
            ms = 0
        out.append({
            "datetime": ms,
            "open":  float(b["open"]),
            "high":  float(b["high"]),
            "low":   float(b["low"]),
            "close": float(b["close"]),
            "volume": int(b.get("vol") or b.get("volume") or 0),
        })
    return out


def _rsi_score_from_candles(candles: List[Dict], cfg: dict) -> Optional[Dict]:
    """Mirror RSIScanner scoring on offline OHLCV bars (15m for BT)."""
    if not candles or len(candles) < 40:
        return None
    closes  = [c["close"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]
    last    = closes[-1]
    min_p = float(_g(cfg, "rsi_min_price", 2.0) or 2.0)
    max_p = float(_g(cfg, "rsi_max_price", 500.0) or 500.0)
    if last < min_p or last > max_p:
        return None
    rsi_entry = float(_g(cfg, "rsi_entry_threshold", 20.0) or 20.0)
    rsi_watch = max(rsi_entry + 20.0, 40.0)
    min_avg_vol = int(_g(cfg, "rsi_min_avg_vol", 300_000) or 300_000)

    rsi = _compute_rsi(closes, 14)
    if rsi is None or rsi > rsi_watch:
        return None

    def _sma(vals, p):
        if len(vals) < p:
            return None
        return sum(vals[-p:]) / p

    def _ema(vals, p):
        if len(vals) < p:
            return None
        k = 2 / (p + 1)
        e = sum(vals[:p]) / p
        for v in vals[p:]:
            e = v * k + e * (1 - k)
        return e

    sma20 = _sma(closes, 20)
    sma20_old = _sma(closes[:-10], 20) if len(closes) > 30 else None
    ema9 = _ema(closes, 9)
    ema20 = _ema(closes, 20)
    macd_d = _compute_macd(closes)
    avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0

    uptrend = bool(sma20 and sma20_old and last > sma20 and sma20 > sma20_old)
    vol_ok  = avg_vol >= min_avg_vol
    macd_ok = bool(macd_d and (macd_d.get("hist", -1) >= -0.05 or
                               (macd_d.get("macd") or 0) > (macd_d.get("signal") or 0)))
    ema_ok  = bool(ema9 and ema20 and ema9 >= ema20 * 0.995)
    rsi_ok  = rsi <= rsi_entry
    detail = {"uptrend": uptrend, "volume": vol_ok, "macd": macd_ok, "ema": ema_ok, "rsi": rsi_ok}
    score = sum(detail.values())
    ready = all(detail.values())
    return {
        "last": last,
        "rsi": round(rsi, 2),
        "score": score,
        "ready": ready,
        "score_detail": detail,
        "avg_vol": int(avg_vol),
    }



def _run_rsi_15m(
    symbol: str,
    bars_15m: List[Dict],
    eval_dates: set,
    capital: float,
    risk_pct: float,
    live_cfg: dict,
) -> List[BacktestTrade]:
    """RSI mean-reversion on **15-minute** historic candles.

    Uses continuous multi-day 15m history for indicators (no look-ahead on the
    signal bar). Entries only on dates inside eval_dates. Holds can span days.
    """
    trades: List[BacktestTrade] = []
    if not bars_15m or len(bars_15m) < 45:
        return trades

    stop_pct = float(_g(live_cfg, "rsi_initial_stop_pct", 3.0) or 3.0)
    trail_pct = float(_g(live_cfg, "rsi_trail_stop_pct", 5.0) or 5.0)
    trail_trig = float(_g(live_cfg, "rsi_trail_trigger_pct", 2.0) or 2.0)
    max_hold = int(_g(live_cfg, "rsi_max_hold_days", 10) or 10)
    rsi_partial = float(_g(live_cfg, "rsi_partial_exit", 30.0) or 30.0)
    rsi_runner = float(_g(live_cfg, "rsi_runner_exit", 40.0) or 40.0)
    max_trades = int(_g(live_cfg, "rsi_max_trades", 3) or 3)
    max_conc = int(_g(live_cfg, "rsi_max_concurrent", 3) or 3)
    acct = float(_g(live_cfg, "account_size", capital) or capital)
    rp = float(_g(live_cfg, "risk_pct", risk_pct) or risk_pct)
    # Force 15m scoring thresholds from live entry (watch = entry+20)
    cfg15 = dict(live_cfg)
    cfg15["rsi_timeframe"] = "15m"

    pos = None
    entries_today: Dict[str, int] = {}
    min_hist = 40

    for i, bar in enumerate(bars_15m):
        bt = datetime.datetime.fromtimestamp(bar["datetime"] / 1000, tz=_ET)
        set_sim_now(bt)
        ds = bt.date().isoformat()
        price = float(bar["close"])
        hi = float(bar["high"])
        lo = float(bar["low"])

        # ── manage open position ──
        if pos is not None:
            peak = max(pos.get("peak", pos["entry"]), hi)
            pos["peak"] = peak
            entry = pos["entry"]
            stop = pos["stop"]
            target = pos["target"]
            gain_pct = (peak - entry) / entry * 100 if entry > 0 else 0
            if gain_pct >= trail_trig:
                new_trail = round(peak * (1.0 - trail_pct / 100.0), 4)
                if new_trail > stop:
                    stop = new_trail
                    pos["stop"] = stop

            exit_price = exit_reason = None
            if lo <= stop:
                exit_price, exit_reason = stop, "stop"
            elif hi >= target:
                exit_price, exit_reason = target, "target"
            else:
                # RSI recovery on 15m history including this bar
                hist_x = bars_15m[: i + 1]
                closes = [c["close"] for c in hist_x]
                rsi_now = _compute_rsi(closes, 14) if len(closes) >= 15 else None
                entry_d = pos.get("entry_date", ds)
                try:
                    y, m, d = (int(x) for x in entry_d.split("-"))
                    held_days = (bt.date() - datetime.date(y, m, d)).days
                except Exception:
                    held_days = 0
                if rsi_now is not None and rsi_now >= rsi_runner:
                    exit_price, exit_reason = price, "rsi_runner"
                elif rsi_now is not None and rsi_now >= rsi_partial:
                    exit_price, exit_reason = price, "rsi_partial"
                elif held_days >= max_hold:
                    exit_price, exit_reason = price, "max_hold"
                elif bt.hour * 100 + bt.minute >= 1555 and held_days >= 0:
                    # optional: no forced EOD for multi-day RSI — only max_hold
                    pass

            if exit_price is not None:
                pos["exit_time"] = bt.strftime("%H:%M")
                pos["date"] = ds
                trades.append(_make_trade(pos, exit_price, exit_reason, i - pos.get("entry_bar", i)))
                pos = None
            continue

        # ── entries only on eval window days ──
        if ds not in eval_dates:
            continue
        if i < min_hist:
            continue
        if entries_today.get(ds, 0) >= max_trades:
            continue

        # History excluding current bar close for cleaner no-lookahead signal
        hist = bars_15m[:i]
        if len(hist) < min_hist:
            continue
        cand = _rsi_score_from_candles(hist, cfg15)
        if not cand or not cand.get("ready"):
            continue

        entry = price  # enter on signal bar close (next-bar open ≈ this close on 15m)
        if entry <= 0:
            continue
        stop = round(entry * (1.0 - stop_pct / 100.0), 4)
        risk_dist = entry - stop
        if risk_dist <= 0:
            continue
        risk_dollars = acct * (rp / 100.0)
        shares = max(1, int(risk_dollars / risk_dist))
        shares = min(shares, max(1, int(acct * 0.25 / entry)))
        target = round(entry + risk_dist * 2.0, 4)

        pos = {
            "symbol": symbol,
            "strategy": "rsi",
            "date": ds,
            "entry_date": ds,
            "entry": entry,
            "stop": stop,
            "target": target,
            "shares": shares,
            "entry_bar": i,
            "entry_time": bt.strftime("%H:%M"),
            "exit_time": "",
            "peak": entry,
        }
        entries_today[ds] = entries_today.get(ds, 0) + 1

    if pos is not None:
        last = bars_15m[-1]
        bt = datetime.datetime.fromtimestamp(last["datetime"] / 1000, tz=_ET)
        pos["exit_time"] = bt.strftime("%H:%M")
        pos["date"] = bt.date().isoformat()
        trades.append(_make_trade(pos, float(last["close"]), "eod", len(bars_15m) - pos.get("entry_bar", 0)))
    return trades


def _swing_analyze_candles(
    candles: List[Dict],
    min_swings: int,
    min_swing_pct: float,
    min_avg_vol: int,
    min_price: float,
    max_price: float,
    pivot_lookback: int = 5,
) -> Optional[Dict]:
    """Offline swing structure from a day of bars (15m or 1m)."""
    if not candles or len(candles) < pivot_lookback * 2 + 3:
        return None
    closes = [c["close"] for c in candles]
    highs  = [c["high"] for c in candles]
    lows   = [c["low"] for c in candles]
    vols   = [c.get("volume", 0) for c in candles]
    last = closes[-1]
    if last < min_price or (max_price > 0 and last > max_price):
        return None
    avg_vol = sum(vols) / len(vols) if vols else 0
    if avg_vol < min_avg_vol:
        return None

    def piv_hi():
        out = []
        lb = pivot_lookback
        for i in range(lb, len(highs) - lb):
            if highs[i] == max(highs[i - lb: i + lb + 1]):
                out.append({"index": i, "price": highs[i], "type": "high"})
        return out

    def piv_lo():
        out = []
        lb = pivot_lookback
        for i in range(lb, len(lows) - lb):
            if lows[i] == min(lows[i - lb: i + lb + 1]):
                out.append({"index": i, "price": lows[i], "type": "low"})
        return out

    ph, pl = piv_hi(), piv_lo()
    # alternate sequence
    all_p = sorted(ph + pl, key=lambda x: x["index"])
    pivots = []
    for p in all_p:
        if not pivots or pivots[-1]["type"] != p["type"]:
            pivots.append(p)
        elif p["type"] == "high" and p["price"] >= pivots[-1]["price"]:
            pivots[-1] = p
        elif p["type"] == "low" and p["price"] <= pivots[-1]["price"]:
            pivots[-1] = p
    swing_count = max(0, len(pivots) - 1)
    if swing_count < min_swings:
        return None
    amps = []
    for i in range(1, len(pivots)):
        prev, cur = pivots[i - 1]["price"], pivots[i]["price"]
        if prev > 0:
            amps.append(abs(cur - prev) / prev * 100)
    avg_amp = sum(amps) / len(amps) if amps else 0
    if avg_amp < min_swing_pct:
        return None
    day_high = max(highs)
    day_low = min(lows)
    open_price = candles[0]["open"]
    recent_highs = sorted([p["price"] for p in pivots if p["type"] == "high"], reverse=True)
    recent_lows = sorted([p["price"] for p in pivots if p["type"] == "low"])
    resistance = recent_highs[0] if recent_highs else day_high
    support = recent_lows[0] if recent_lows else day_low
    return {
        "last": last,
        "support": support,
        "resistance": resistance,
        "swing_count": swing_count,
        "day_high": day_high,
        "day_low": day_low,
        "open_price": open_price,
        "avg_amp": avg_amp,
    }


def _run_swing_day(
    symbol: str,
    date_str: str,
    candles_15m: List[Dict],
    capital: float,
    risk_pct: float,
    live_cfg: dict,
) -> List[BacktestTrade]:
    """Intraday swing range entries near support (mirrors live swing path)."""
    trades: List[BacktestTrade] = []
    if not candles_15m or len(candles_15m) < 12:
        return trades

    min_swings = int(_g(live_cfg, "swing_min_swings", 3) or 3)
    min_swing_pct = float(_g(live_cfg, "swing_min_swing_pct", 0.5) or 0.5)
    min_avg_vol = int(_g(live_cfg, "swing_min_avg_vol", 100_000) or 100_000)
    max_retrace = float(_g(live_cfg, "swing_max_retrace_pct", 40.0) or 40.0)
    min_price = float(_g(live_cfg, "min_price", 1.0) or 1.0)
    max_price = float(_g(live_cfg, "max_price", 0.0) or 0.0) or 500.0
    acct = float(_g(live_cfg, "account_size", capital) or capital)
    rp = float(_g(live_cfg, "risk_pct", risk_pct) or risk_pct)
    cutoff_h = int(_g(live_cfg, "entry_cutoff_hour", 14) or 14)
    require_vwap = bool(_g(live_cfg, "require_vwap_above", True))

    positions: Dict[str, Dict] = {}

    for i, bar in enumerate(candles_15m):
        bar_time = datetime.datetime.fromtimestamp(bar["datetime"] / 1000, tz=_ET)
        set_sim_now(bar_time)
        price = bar["close"]
        hm = bar_time.hour * 100 + bar_time.minute

        for sym in list(positions.keys()):
            pos = positions[sym]
            exit_price = exit_reason = None
            if bar["low"] <= pos["stop"]:
                exit_price, exit_reason = pos["stop"], "stop"
            elif bar["high"] >= pos["target"]:
                exit_price, exit_reason = pos["target"], "target"
            elif hm >= 1555:
                exit_price, exit_reason = price, "eod"
            if exit_price is not None:
                pos["exit_time"] = bar_time.strftime("%H:%M")
                trades.append(_make_trade(pos, exit_price, exit_reason, i))
                del positions[sym]

        if symbol in positions or i < 10:
            continue
        if bar_time.hour >= cutoff_h:
            continue

        window = candles_15m[: i + 1]
        structure = _swing_analyze_candles(
            window, min_swings, min_swing_pct, min_avg_vol, min_price, max_price,
        )
        if not structure:
            continue

        support = structure["support"]
        resistance = structure["resistance"]
        range_size = resistance - support
        if range_size <= 0:
            continue
        near_support = price <= support + range_size * 0.15
        if not near_support:
            continue

        open_price = structure["open_price"]
        day_high = structure["day_high"]
        gains = day_high - open_price
        if gains > 0.01:
            retrace_pct = (day_high - price) / gains * 100
            if retrace_pct > max_retrace:
                continue

        if require_vwap:
            vwap = _compute_vwap(window)
            if vwap and price < vwap:
                continue

        stop = round(support - range_size * 0.10, 4)
        target = round(support + range_size * 0.75, 4)
        if stop >= price or target <= price:
            continue
        risk_dist = price - stop
        if risk_dist <= 0:
            continue
        risk_dollars = acct * (rp / 100.0)
        shares = max(1, int(risk_dollars / risk_dist))
        shares = min(shares, max(1, int(acct * 0.25 / price)))

        positions[symbol] = {
            "symbol": symbol,
            "strategy": "swing",
            "date": date_str,
            "entry": price,
            "stop": stop,
            "target": target,
            "shares": shares,
            "entry_bar": i,
            "entry_time": bar_time.strftime("%H:%M"),
            "exit_time": "",
        }

    for sym, pos in positions.items():
        ep = candles_15m[-1]["close"]
        pos["exit_time"] = "16:00"
        trades.append(_make_trade(pos, ep, "eod", len(candles_15m)))
    return trades



# ─────────────────────────────────────────────────────────────────
# BacktestEngine
# ─────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Runs offline strategy backtests against deterministic synthetic OHLC data.
    Call start(params) for async execution; poll .report for progress.
    """

    def __init__(self):
        self._report = BacktestReport()
        self._lock   = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def report(self) -> BacktestReport:
        with self._lock:
            return self._report

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, params: dict) -> None:
        """Launch backtest in background thread. Non-blocking."""
        if self.is_running():
            return
        with self._lock:
            self._report = BacktestReport(params=params, status="running", message="Starting…")
        self._thread = threading.Thread(
            target=self._run_safe, args=(params,), daemon=True, name="BacktestThread")
        self._thread.start()

    def _run_safe(self, params: dict) -> None:
        try:
            report = self._run(params)
            with self._lock:
                self._report = report
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            bt_log(f"Fatal error: {e}\n{tb}")
            with self._lock:
                self._report.status  = "error"
                self._report.message = str(e)
        finally:
            set_sim_now(None)

    def _update(self, progress: float, msg: str) -> None:
        with self._lock:
            self._report.progress = progress
            self._report.message  = msg

    def _run(self, params: dict) -> BacktestReport:
        t_start = time.time()
        live_cfg    = _load_live_config(params)
        bt_log_section("BACKTEST RUN START")
        bt_log(f"params_raw: symbols={params.get('symbols')} days={params.get('days')} strategies={params.get('strategies')} capital={params.get('capital')} risk={params.get('risk_pct')}", also_bot=False)
        symbols     = [s.strip().upper() for s in params.get("symbols", ["TSLA", "NVDA", "AAPL"])]
        days        = max(1, min(int(params.get("days", 5)), 365))
        strategies  = [
            s.lower() for s in params.get("strategies", ["orb", "scalp", "div", "rsi", "swing"])
            if str(s).lower() not in ("fp", "first_pullback")
        ]
        # Prefer live account / risk when UI leaves defaults blank-ish
        capital     = float(params.get("capital") or _g(live_cfg, "account_size", 10000.0) or 10000.0)
        risk_pct    = float(params.get("risk_pct") if params.get("risk_pct") is not None
                            else _g(live_cfg, "risk_pct", 1.0) or 1.0)
        # BT data is 15m bars: convert live orb_minutes (wall-clock) → bar count
        orb_minutes = _orb_bars_from_live(live_cfg, params.get("orb_minutes"))
        bt_log(f"[BT] Using live strategy settings | capital=${capital:.0f} risk={risk_pct}% "
            f"orb_bars={orb_minutes} (from live orb_minutes={_g(live_cfg,'orb_minutes',15)}) "
            f"pullback={_g(live_cfg,'require_pullback',True)} "
            f"macd/adx/htf={_g(live_cfg,'use_macd_filter',True)}/"
            f"{_g(live_cfg,'use_adx_filter',True)}/{_g(live_cfg,'use_htf_filter',True)} "
            f"div_macd={_g(live_cfg,'div_use_macd_confirm',True)} "
            f"scalp_$={_g(live_cfg,'scalp_dollar_per_trade',200)} "
            f"stop%={_g(live_cfg,'scalp_stop_pct',1.5)} rr={_g(live_cfg,'scalp_rr_ratio',2.0)} "
            f"rsi_tf=15m entry≤{_g(live_cfg,'rsi_entry_threshold',20)}"
        )

        trade_dates = _trading_dates_back(days)
        total_work  = len(symbols) * len(trade_dates)
        done        = 0

        params = dict(params or {})
        params["live_config_applied"] = True
        params["orb_bars_bt"] = orb_minutes
        report = BacktestReport(params=params, status="running")

        # ── Instantiate strategies ────────────────────────────────
        from sim_client import SimClient
        sim_client = SimClient()

        orb_strat = scalp_strat = div_strat = None

        if "orb" in strategies:
            from orb_strategy import ORBStrategy
            orb_strat = ORBStrategy()
            orb_strat.set_config(**_orb_set_config_kwargs(live_cfg, capital, risk_pct, orb_minutes))


        if "scalp" in strategies:
            from scalp_strategy import ScalpStrategy
            scalp_strat = ScalpStrategy()
            scalp_strat.set_config(**_scalp_set_config_kwargs(live_cfg, capital))

        if "div" in strategies:
            from divergence_strategy import DivergenceStrategy
            div_strat = DivergenceStrategy()
            div_strat.set_config(**_div_set_config_kwargs(live_cfg, capital, risk_pct))

        # ── Generate candle data ──────────────────────────────────
        history_days = max(days + 15, 20)
        if "div" in strategies or "rsi" in strategies:
            history_days = max(history_days, days + 40)
        if "rsi" in strategies:
            history_days = max(history_days, days + 60)
        all_dates = _trading_dates_back(history_days)
        backtest_dates = _trading_dates_back(days)  # only the eval window

        # Fetch real market data: daily anchors + maximum historic 15m window
        self._update(0.01, f"Fetching historic candles for {len(symbols)} symbols…")
        _clear_real_15m_cache()
        _prefetch_daily_data(symbols, max(days + 80, 120))
        # Pull full Yahoo 15m window (~60d) for all symbols; covers eval + RSI warm-up
        _prefetch_real_data(symbols, all_dates)

        real_count = sum(
            1 for sym in symbols for d in all_dates
            if isinstance(_real_candle_cache.get((sym.upper(), d.isoformat())), list)
        )
        total_combos = len(symbols) * len(all_dates)
        data_note = (
            f"{real_count}/{total_combos} sessions use REAL 15m historic data"
            if real_count > 0
            else "no real 15m data — daily-anchored / synthetic only"
        )
        bt_log(f"[BT] Data source: {data_note}")

        self._update(0.02, f"Building 15m candle database ({data_note})…")
        bt_log(f"[BT] Building candles: {len(symbols)} symbols × "
            f"{len(all_dates)} days × {len(strategies)} strategies | RSI on 15m"
        )

        candles_db: Dict[str, Dict[str, List[Dict]]] = {}
        for sym in symbols:
            candles_db[sym] = {}
            prev_close = None
            for d in all_dates:
                day_c = _gen_day_candles(sym, d, prev_close)
                if day_c:
                    prev_close = day_c[-1]["close"]
                    candles_db[sym][d.isoformat()] = day_c
                else:
                    candles_db[sym][d.isoformat()] = []

        self._update(0.10, (
            f"Running backtest: {len(strategies)} strategies × "
            f"{len(symbols)} symbols × {days} days"
        ))

        all_trades: List[BacktestTrade] = []
        _trades_lock = threading.Lock()
        _done_count  = [0]

        def _run_sym(sym: str) -> None:
            """Run all enabled strategies for one symbol in a worker thread.
            Creates its own strategy instances for thread safety.
            """
            _orb_s = _scalp_s = _div_s = None
            _do_rsi = "rsi" in strategies
            _do_swing = "swing" in strategies
            if "orb" in strategies:
                from orb_strategy import ORBStrategy
                _orb_s = ORBStrategy()
                _orb_s.set_config(**_orb_set_config_kwargs(live_cfg, capital, risk_pct, orb_minutes))
            if "scalp" in strategies:
                from scalp_strategy import ScalpStrategy
                _scalp_s = ScalpStrategy()
                _scalp_s.set_config(**_scalp_set_config_kwargs(live_cfg, capital))
            if "div" in strategies:
                from divergence_strategy import DivergenceStrategy
                from sim_client import SimClient as _SC
                _div_s = DivergenceStrategy()
                _div_s.set_config(**_div_set_config_kwargs(live_cfg, capital, risk_pct))
                _sim_c = _SC()
            else:
                _sim_c = None

            sym_trades: List[BacktestTrade] = []
            for date in trade_dates:
                date_str = date.isoformat()
                candles  = candles_db[sym].get(date_str, [])

                if candles and _orb_s:
                    try:
                        sym_trades.extend(
                            _run_orb_day(sym, date_str, candles, _orb_s, orb_minutes, capital))
                    except Exception as _e:
                        bt_log(f"[BT-ORB] {sym}/{date_str}: {_e}")

                if candles and _scalp_s:
                    # ORB-momentum scalp using live scalp tab size/stop/RR
                    try:
                        sp = _scalp_orb_params(live_cfg, capital)
                        sym_trades.extend(
                            _run_scalp_orb_day(sym, date_str, candles, **sp))
                    except Exception as _e:
                        bt_log(f"[BT-SCALP] {sym}/{date_str}: {_e}")

                if candles and _div_s and _sim_c:
                    try:
                        sym_trades.extend(
                            _run_div_day(sym, date_str, date,
                                         candles_db[sym], _div_s, _sim_c))
                    except Exception as _e:
                        bt_log(f"[BT-DIV] {sym}/{date_str}: {_e}")

                if candles and _do_swing:
                    try:
                        sym_trades.extend(
                            _run_swing_day(
                                sym, date_str, candles, capital, risk_pct, live_cfg,
                            ))
                    except Exception as _e:
                        bt_log(f"[BT-SWING] {sym}/{date_str}: {_e}")

                with _trades_lock:
                    _done_count[0] += 1
                    _d = _done_count[0]
                prog = 0.10 + 0.85 * (_d / max(total_work, 1))
                self._update(prog, f"Backtesting {sym} {date_str} ({_d}/{total_work})")

            # RSI on continuous historic 15m (warm-up + eval window)
            if _do_rsi:
                try:
                    # Warm-up: extra trading days before eval for indicator history
                    warm_n = max(days + 15, 25)
                    warm_dates = _trading_dates_back(warm_n)
                    # Prefer dates that exist in candles_db for this symbol
                    series_dates = sorted(
                        set(warm_dates) | set(trade_dates),
                        key=lambda x: x.isoformat(),
                    )
                    bars_15m = _continuous_15m(sym, series_dates, candles_db)
                    eval_set = {d.isoformat() for d in trade_dates}
                    n_real = sum(
                        1 for d in series_dates
                        if isinstance(
                            _real_candle_cache.get((sym.upper(), d.isoformat())), list
                        )
                    )
                    bt_log(f"[BT-RSI] {sym}: {len(bars_15m)}×15m bars "
                        f"({n_real} real sessions in series) | eval days={len(eval_set)}"
                    )
                    sym_trades.extend(
                        _run_rsi_15m(
                            sym, bars_15m, eval_set, capital, risk_pct, live_cfg,
                        )
                    )
                except Exception as _e:
                    bt_log(f"[BT-RSI] {sym}: {_e}")

            with _trades_lock:
                all_trades.extend(sym_trades)

        # Run symbols in parallel — each thread has its own strategy instances
        n_workers = min(len(symbols), 4)
        with ThreadPoolExecutor(max_workers=n_workers) as _pool:
            _futs = [_pool.submit(_run_sym, s) for s in symbols]
            for _fut in as_completed(_futs):
                try:
                    _fut.result()  # propagate exceptions
                except Exception as _exc:
                    bt_log(f"[BT] Symbol worker error: {_exc}")

        set_sim_now(None)

        # ── Build statistics ──────────────────────────────────────
        self._update(0.97, "Computing statistics…")
        report.all_trades = all_trades

        for strat in strategies:
            strat_trades = [t for t in all_trades if t.strategy == strat]
            report.by_strategy[strat] = _compute_stats(strat_trades)

        for sym in symbols:
            sym_trades = [t for t in all_trades if t.symbol == sym]
            report.by_symbol[sym] = _compute_stats(sym_trades)

        report.summary     = _compute_stats(all_trades)
        report.status      = "complete"
        report.progress    = 1.0
        report.elapsed_sec = time.time() - t_start
        report.message     = (
            f"Complete — {len(all_trades)} trades | "
            f"{len(symbols)} symbols × {days} days × "
            f"{len(strategies)} strategies | {report.elapsed_sec:.1f}s"
        )
        try:
            # recompute data note for summary if not in scope — use locals
            _rc = sum(
                1 for sym in symbols for d in all_dates
                if isinstance(_real_candle_cache.get((sym.upper(), d.isoformat())), list)
            )
            _tc = max(1, len(symbols) * len(all_dates))
            _dn = f"{_rc}/{_tc} real 15m sessions"
        except Exception:
            _rc, _tc, _dn = 0, 0, "n/a"
        try:
            bt_log_run_summary(
                report, strategies, symbols, days, capital, risk_pct,
                live_cfg, _dn, _rc, _tc,
            )
        except Exception as _se:
            bt_log(f"[BT] summary write error: {_se}")
            bt_log(report.message)
        return report
