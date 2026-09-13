"""
data_handler.py
Config, watchlist, and position persistence (JSON-backed).
Mirrors data_handler.py from the crypto bot.
"""

import json
import os
from typing import Any, Dict, List

DATA_DIR = "saved_data"

DEFAULT_CONFIG = {
    "app_key":            "",
    "app_secret":         "",
    "account_size":       5000.00,
    "risk_pct":           1.5,
    "orb_minutes":        15,
    "max_trades_per_day":        10,   # Increased for multi-stock concurrent trading
    "max_concurrent_positions":   5,    # matches engine default
    "auto_trade":                 True,   # Paper fills when a strategy is armed
    "dry_run":                    True,   # Locked: never POST to Schwab
    "paper_mode":                 True,
    "auto_start_trading":         False, # Do not auto-arm on boot
    "extended_hours":             False, # ETH stays off until clock/stop split accepted
    "swing_scan_enabled":         False, # One RTH book first (ORB)
    "swing_scan_interval_sec":    60.0,  # Swing scan frequency in seconds
    "poll_interval":      30,
    "entry_cutoff_hour":  12,   # No new entries after 12:00 PM ET (matches engine)
    "rr_target":                  2.0,
    "require_vwap_above":         True,
    "min_gap_pct":                3.0,   # Minimum gap % to pass scanner filter
    "min_rel_vol":                2.5,   # Minimum relative volume to pass scanner filter
    "min_price":                  0.50,  # Minimum price filter
    "max_price":                  0,     # 0 = no upper limit
    "max_float":                  0,     # 0 = no float limit (Ross Cameron ★ badge shown separately)
    # Order execution
    "limit_entry_buffer":         0.5,   # % above ask for limit entry orders
    # Trailing stop upgrade
    "trail_trigger_r":            1.5,   # R-multiple gain before eligible
    "trail_vol_mult":             2.5,   # Volume spike multiplier vs average
    "trail_pct":                  3.0,   # Trailing stop distance %
    # Breakout / entry confirmation
    "confirm_close":              True,  # Require candle CLOSE beyond ORB high (not just a wick)
    "require_volume_confirm":     True,  # Require a volume surge on the breakout candle
    "min_breakout_rel_vol":       1.5,   # Breakout-candle vol must be >= this x recent avg
    # Momentum filter
    "use_rsi_filter":             True,  # Block entries when RSI is overextended
    "rsi_overbought":             80.0,  # 80 is standard for ORB/momentum breakouts (75 was too strict)
    "rsi_oversold":               25.0,
    # ATR-based stops
    "use_atr_stops":              True,  # Use ATR(14) * mult for stop distance (floored at ORB low)
    "atr_stop_mult":              1.5,
    # Partial exit + breakeven
    "partial_exit_enabled":       True,
    "partial_exit_r":             1.0,   # Scale out at +1R
    "partial_exit_pct":           50.0,  # % of position to scale out
    "runner_target_r":            3.0,   # Remaining runner targets this R after the partial
    # Risk controls
    "max_daily_loss_pct":         5.0,   # Halt + flatten when day PnL <= -this% of account
    "eod_flat_enabled":           True,  # Flatten all positions before the close
    "eod_flat_hhmm":              1558,  # 15:58 ET flatten all paper trades
    "eod_flat_timezone":          "America/New_York",
    # Advanced indicator filters (optional, default OFF)
    "use_macd_filter":            False, # Require bullish MACD histogram on entry
    "use_adx_filter":             False, # Require trend strength (ADX) on entry
    "adx_min":                    20.0,  # Minimum ADX for a tradeable trend
    "use_htf_filter":             False, # Require 5-min EMA alignment
    "use_macd_exit":              False, # Exit held positions on MACD bearish cross
    # Scanner cadence
    "scan_interval_sec":          60.0,  # Seconds between gap scans during sessions
    # First Pullback strategy (Ross Cameron momentum setup)
    "use_orb_strategy":           True,  # One RTH book
    "use_div_strategy":           False,
    "scalp_enabled":              False,
    "rsi_enabled":                False,
    "rsi_timeframe":              "daily",
    "use_first_pullback":         False, # Enable intraday First Pullback signal detection
    "fp_time_limit_hhmm":         1400,  # Stop new First Pullback entries after 2:00 PM ET
    "fp_max_stop_cents":          0.30,  # Max allowed stop distance in dollars
    "fp_min_pole_pct":            1.0,   # Minimum pole move % to qualify
    # News sentiment gate
    "news_sentiment_enabled":     True,
    "news_block_score":           -40,
    "news_cautious_score":        -10,
    "news_boost_score":           25,
    "news_max_age_hours":         24,
    "news_size_boost_pct":        0.15,
    "news_cache_ttl_sec":         900,
    "options_enabled":            False,
    "use_options_confirm":        False,
    "options_dry_run":            True,
    "options_expiry_mode":        "wed_then_fri",
    "options_max_premium":        150.0,
    "options_max_contracts":      2,
    "options_max_concurrent":     1,
    "options_strike":             "atm",
    "options_eod_flat":           True,
    "options_last_entry_hhmm":    1558,
    "options_watchlist":          [],
    "options_scan_interval_sec":  120.0,
}

DEFAULT_WATCHLIST: List[str] = []   # empty — scanner discovers gappers via the movers endpoint


class DataHandler:
    def __init__(self, data_dir: str = DATA_DIR) -> None:
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def _path(self, filename: str) -> str:
        return os.path.join(self.data_dir, filename)

    def _read(self, filename: str, default: Any = None) -> Any:
        path = self._path(filename)
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return default

    def _write(self, filename: str, data: Any) -> None:
        path = self._path(filename)
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            from utils import log_message
            log_message(f"[DATA] Write error {filename}: {e}")

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def load_config(self) -> Dict:
        saved = self._read("config.json", {})
        config = dict(DEFAULT_CONFIG)
        config.update(saved)
        return config

    def save_config(self, config: Dict) -> None:
        existing = self._read("config.json", {})
        existing.update(config)
        self._write("config.json", existing)

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    def load_watchlist(self) -> List[str]:
        return self._read("watchlist.json", list(DEFAULT_WATCHLIST))

    def save_watchlist(self, symbols: List[str]) -> None:
        self._write("watchlist.json", symbols)

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def save_positions(self, open_pos: List[Dict], closed_pos: List[Dict]) -> None:
        self._write("positions.json", {"open": open_pos, "closed": closed_pos})

    def load_positions(self) -> Dict:
        return self._read("positions.json", {"open": [], "closed": []})

    # ------------------------------------------------------------------
    # Trade log
    # ------------------------------------------------------------------

    def append_trade_log(self, trade: Dict) -> None:
        import datetime
        log = self._read("trade_log.json", [])
        if "time" not in trade:
            trade = {**trade, "time": datetime.datetime.utcnow().isoformat() + "Z"}
        log.append(trade)
        self._write("trade_log.json", log)

    def load_trade_log(self) -> List[Dict]:
        return self._read("trade_log.json", [])
