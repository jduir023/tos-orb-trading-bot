"""
trading_engine.py
Headless trading loop — orchestrates scanner, strategy, and order execution.
Mirrors trading_bot_engine.py from the crypto bot.
"""

import datetime
import threading
import os
import time
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

from schwab_client import SchwabClient
from scanner import StockScanner
from swing_scanner import SwingScanner
from orb_strategy import ORBStrategy, TradeSignal, OpenPosition
from first_pullback_strategy import FirstPullbackStrategy
from divergence_strategy import DivergenceStrategy
from scalp_strategy import ScalpStrategy
from data_handler import DataHandler
from news_sentiment import NewsSentimentService
from candidate_status import refresh_candidate_report, get_candidates_report
from analytics import compute_analytics
from utils import log_message
from strategy_registry import StrategyRegistry, _StrategyDescriptor
from rsi_strategy import RSIStrategy
from scalp_screener import ScalpScreener
from sr_levels import SREngine
from options_confirm_strategy import OptionsConfirmStrategy
from paper_account import PaperAccount, PAPER_STARTING_CASH
from schedule import (
    FLAT_HHMM, in_entry_window, in_scan_window, is_weekday, session_label, should_flatten,
)

STRATEGY_ENABLE_KEYS = {
    "use_orb_strategy": "orb",
    "use_div_strategy": "divergence",
    "swing_scan_enabled": "swing",
    "scalp_enabled": "scalp",
    "rsi_enabled": "rsi",
    "use_options_confirm": "opt_confirm",
    "options_enabled": "opt_confirm",
}


# Trading session windows (ET, 24-hour)
SESSION_PREMARKET_START  = (7,  0)   # Schwab ETH pre 7:00 ET (not Arca 4:00)
SESSION_PREMARKET_END    = (9, 25)   # Schwab International pre ends 9:25 ET
SESSION_REGULAR_START    = (9, 30)   # 09:30 ET RTH
SESSION_REGULAR_END      = (16, 0)   # 16:00 ET
SESSION_AFTERHOURS_START = (16, 5)   # Schwab ETH after 4:05 ET
SESSION_AFTERHOURS_END   = (20, 0)   # Schwab ETH after 8:00 ET


class TradingEngine:
    """
    Core engine:
      1. Runs the pre-market scanner.
      2. After 09:45 ET fetches ORB levels for top scan candidates.
      3. Polls quotes every `poll_interval` seconds, feeds strategy.
      4. On signal: places market buy + OCO (stop + target) orders.
      5. Monitors open positions for manual override / safety stop.
    """

    def __init__(self) -> None:
        self.data_handler = DataHandler()
        cfg = self.data_handler.load_config()

        # Prefer environment variables for API keys — keeps secrets out of config.json
        _app_key    = os.getenv("SCHWAB_APP_KEY")    or cfg.get("app_key", "")
        _app_secret = os.getenv("SCHWAB_APP_SECRET") or cfg.get("app_secret", "")
        self.client = SchwabClient(
            app_key=_app_key,
            app_secret=_app_secret,
            data_dir="saved_data",
        )
        # Permanent reference to the real SchwabClient — used for auth operations
        # even when sim mode replaces self.client with SimClient.
        self._schwab_client = self.client

        self.scanner      = StockScanner(client=self.client)
        self.swing_scanner = SwingScanner(client=self.client)
        self.strategy      = ORBStrategy()
        self.fp_strategy   = FirstPullbackStrategy()
        self.div_strategy   = DivergenceStrategy()
        self.scalp_strategy = ScalpStrategy()
        self.rsi_strategy   = RSIStrategy(client=self.client)
        self.scalp_screener = ScalpScreener(client=self.client)
        self.sr_engine = SREngine(ttl_sec=15.0)
        self.options_strategy = OptionsConfirmStrategy(client=self._schwab_client)

        # Engine state
        self.trading_active    = False
        self.poll_interval     = 30.0
        self.paper = PaperAccount(os.path.join("saved_data", "paper_account.json"), PAPER_STARTING_CASH)
        self.paper_mode = True
        self.account_size      = float(self.paper.starting_cash)
        self.risk_pct          = float(cfg.get("risk_pct", 1.5))
        self.orb_minutes       = int(cfg.get("orb_minutes", 15))
        self.max_trades        = int(cfg.get("max_trades_per_day", 10))
        self.max_concurrent     = int(cfg.get("max_concurrent_positions", 5))
        self.rr_target              = float(cfg.get("rr_target", 2.0))
        self.entry_cutoff_hour      = int(cfg.get("entry_cutoff_hour", 12))
        self.require_vwap_above     = bool(cfg.get("require_vwap_above", True))
        self.auto_trade        = True   # paper fills when a strategy is armed
        self.dry_run           = True   # alias: never POST to Schwab
        self.paper_mode        = True
        self.extended_hours    = False  # RTH weekdays only
        self.swing_scan_enabled = bool(cfg.get("swing_scan_enabled", False))
        # Trailing stop upgrade config
        self.trail_trigger_r    = float(cfg.get("trail_trigger_r",    1.5))
        self.trail_vol_mult     = float(cfg.get("trail_vol_mult",     2.5))
        self.trail_pct          = float(cfg.get("trail_pct",          3.0))
        self.limit_entry_buffer = float(cfg.get("limit_entry_buffer", 0.5)) / 100.0  # store as decimal
        # Strategy enhancement config
        self.confirm_close          = bool(cfg.get("confirm_close", True))
        self.require_volume_confirm = bool(cfg.get("require_volume_confirm", True))
        self.min_breakout_rel_vol   = float(cfg.get("min_breakout_rel_vol", 1.5))
        self.use_rsi_filter         = bool(cfg.get("use_rsi_filter", True))
        self.rsi_overbought         = float(cfg.get("rsi_overbought", 75.0))
        self.rsi_oversold           = float(cfg.get("rsi_oversold", 25.0))
        self.use_atr_stops          = bool(cfg.get("use_atr_stops", True))
        self.atr_stop_mult          = float(cfg.get("atr_stop_mult", 1.5))
        self.min_stop_dist          = float(cfg.get("min_stop_dist", 0.10))
        self.swing_max_retrace_pct  = float(cfg.get("swing_max_retrace_pct", 40.0))
        # Global intraday retrace gate — applies to ORB, FP, swing, and manual entries
        self.intraday_max_retrace_pct = float(
            cfg.get("intraday_max_retrace_pct", cfg.get("swing_max_retrace_pct", 40.0))
        )
        # Time-of-day position size decay: reduce size after morning momentum fades
        self.time_decay_start_hhmm  = int(cfg.get("time_decay_start_hhmm", 1130))
        self.time_decay_factor      = float(cfg.get("time_decay_factor", 0.5))
        # ORB quality filter config
        self.require_orb_above_vwap = bool(cfg.get("require_orb_above_vwap", True))
        self.orb_min_range_pct      = float(cfg.get("orb_min_range_pct", 0.5))
        self.orb_max_range_pct      = float(cfg.get("orb_max_range_pct", 8.0))
        self.orb_max_chase_pct      = float(cfg.get("orb_max_chase_pct", 1.5))
        self.require_pullback           = bool(cfg.get("require_pullback", True))
        self.orb_pullback_min_pct       = float(cfg.get("orb_pullback_min_pct", 0.20))
        self.orb_pullback_max_pct       = float(cfg.get("orb_pullback_max_pct", 1.50))
        self.orb_pullback_reclaim_pct   = float(cfg.get("orb_pullback_reclaim_pct", 0.10))
        self.orb_pullback_max_chase_pct = float(cfg.get("orb_pullback_max_chase_pct", 0.50))
        self.orb_momentum_vol_min        = float(cfg.get("orb_momentum_vol_min", 8.0))
        self.orb_momentum_rel_vol_min    = float(cfg.get("orb_momentum_rel_vol_min", 5.0))
        self.orb_fp_min_pole_pct         = float(cfg.get("orb_fp_min_pole_pct", 0.8))
        self.orb_fp_max_retrace_pct      = float(cfg.get("orb_fp_max_retrace_pct", 50.0))
        self.orb_max_float_shares   = int(cfg.get("orb_max_float", 50_000_000))
        # Market regime filter
        self.use_market_filter      = bool(cfg.get("use_market_filter", True))
        self.market_filter_symbol   = str(cfg.get("market_filter_symbol", "SPY"))
        self._market_filter_ok: bool  = True
        self._market_filter_ts: float = 0.0
        # FP retrace config
        self.fp_max_retrace_pct     = float(cfg.get("fp_max_retrace_pct", 40.0))
        # Divergence strategy config
        self.use_div_strategy       = bool(cfg.get("use_div_strategy", False))
        self._div_criteria_cache: Dict[str, Any] = {}
        self.div_bb_period          = int(cfg.get("div_bb_period", 21))
        self.div_bb_std_mult        = float(cfg.get("div_bb_std_mult", 1.0))
        self.div_lookback           = int(cfg.get("div_lookback", 60))
        self.div_pivot_bars         = int(cfg.get("div_pivot_bars", 3))
        self.div_min_rsi_div        = float(cfg.get("div_min_rsi_div", 2.0))
        self.div_max_rsi_entry      = float(cfg.get("div_max_rsi_entry", 45.0))
        self.div_band_proximity     = float(cfg.get("div_band_proximity", 0.15))
        self.div_use_macd_confirm   = bool(cfg.get("div_use_macd_confirm", True))
        self.div_stop_buffer_pct    = float(cfg.get("div_stop_buffer_pct",  0.5))
        # Scalp strategy config
        self.scalp_enabled          = bool(cfg.get("scalp_enabled",          False))
        self.scalp_symbol           = str(cfg.get("scalp_symbol",             ""))
        self.scalp_dollar_per_trade = float(cfg.get("scalp_dollar_per_trade", 50.0))
        self.scalp_session_budget   = float(cfg.get("scalp_session_budget",  200.0))
        self.scalp_target_pct       = float(cfg.get("scalp_target_pct",       0.50))
        self.scalp_stop_pct         = float(cfg.get("scalp_stop_pct",         0.25))
        self.scalp_rr_ratio         = float(cfg.get("scalp_rr_ratio",          2.0))
        self.scalp_cooldown_sec     = int(cfg.get("scalp_cooldown_sec",          60))
        self.scalp_max_trades       = int(cfg.get("scalp_max_trades",            20))
        self.scalp_breakout_bars    = int(cfg.get("scalp_breakout_bars",           5))
        self.scalp_min_vol_mult     = float(cfg.get("scalp_min_vol_mult",        1.5))
        self.scalp_rsi_min          = float(cfg.get("scalp_rsi_min",            40.0))
        self.scalp_rsi_max          = float(cfg.get("scalp_rsi_max",            70.0))
        self.div_min_stop_pct       = float(cfg.get("div_min_stop_pct",     0.5))
        self.div_max_stop_pct       = float(cfg.get("div_max_stop_pct",     8.0))
        self.div_max_hold_days      = int(cfg.get("div_max_hold_days",       5))
        self.div_resample_minutes   = int(cfg.get("div_resample_minutes",   30))
        self.div_lookback_days      = int(cfg.get("div_lookback_days",      10))
        self.scalp_candle_minutes  = int(cfg.get("scalp_candle_minutes",  5))
        self.scalp_scan_interval_sec = float(cfg.get("scalp_scan_interval_sec", 60.0))
        self._last_scalp_scan: float = 0.0
        # RSI Mean Reversion strategy config
        self.rsi_enabled           = bool(cfg.get("rsi_enabled",           False))
        self.rsi_entry_threshold   = float(cfg.get("rsi_entry_threshold",  20.0))
        self.rsi_partial_exit      = float(cfg.get("rsi_partial_exit",     30.0))
        self.rsi_runner_exit       = float(cfg.get("rsi_runner_exit",      40.0))
        self.rsi_initial_stop_pct  = float(cfg.get("rsi_initial_stop_pct",  3.0))
        self.rsi_trail_stop_pct    = float(cfg.get("rsi_trail_stop_pct",    5.0))
        self.rsi_trail_trigger_pct = float(cfg.get("rsi_trail_trigger_pct", 2.0))
        self.rsi_max_hold_days     = int(cfg.get("rsi_max_hold_days",       10))
        self.rsi_scan_interval_sec = float(cfg.get("rsi_scan_interval_sec",300.0))
        self.rsi_min_avg_vol       = int(cfg.get("rsi_min_avg_vol",     300_000))
        self.rsi_min_price         = float(cfg.get("rsi_min_price",        2.0))
        self.rsi_max_price         = float(cfg.get("rsi_max_price",      500.0))
        self.rsi_max_trades        = int(cfg.get("rsi_max_trades",          3))
        self.rsi_max_concurrent    = int(cfg.get("rsi_max_concurrent",      3))
        self.rsi_timeframe         = str(cfg.get("rsi_timeframe", "daily") or "daily")
        self._last_rsi_scan: float = 0.0
        self.use_options_confirm = bool(cfg.get("use_options_confirm", False))
        self.options_enabled = self.use_options_confirm
        self.options_dry_run = True
        self.options_expiry_mode = str(cfg.get("options_expiry_mode", "wed_then_fri") or "wed_then_fri")
        self.options_max_premium = float(cfg.get("options_max_premium", 150.0))
        self.options_max_contracts = int(cfg.get("options_max_contracts", 2))
        self.options_max_concurrent = int(cfg.get("options_max_concurrent", 1))
        self.options_strike = str(cfg.get("options_strike", "atm") or "atm")
        self.options_eod_flat = True   # flatten options with the 15:58 ET close
        self.options_last_entry_hhmm = FLAT_HHMM
        _owl = cfg.get("options_watchlist") or []
        if isinstance(_owl, str):
            self.options_watchlist = [x.strip().upper() for x in _owl.split(",") if x.strip()]
        else:
            self.options_watchlist = [str(x).upper() for x in _owl]
        self.options_scan_interval_sec = float(cfg.get("options_scan_interval_sec", 60.0))
        self._last_options_scan: float = 0.0
        self._last_uoa_scan: float = 0.0
        self._uoa_results: List[Dict] = []
        # Advanced indicator filters (optional, default OFF)
        self.use_macd_filter        = bool(cfg.get("use_macd_filter", False))
        self.use_adx_filter         = bool(cfg.get("use_adx_filter", False))
        self.adx_min                = float(cfg.get("adx_min", 20.0))
        self.use_htf_filter         = bool(cfg.get("use_htf_filter", False))
        self.use_5min_entry_rules     = bool(cfg.get("use_5min_entry_rules", True))
        self.htf_sma_period           = int(cfg.get("htf_sma_period", 10))
        self.require_5min_vol_above_avg = bool(cfg.get("require_5min_vol_above_avg", True))
        self.require_dip_entry          = bool(cfg.get("require_dip_entry", True))
        self.dip_sma_touch_pct          = float(cfg.get("dip_sma_touch_pct", 0.30))
        self.use_macd_exit          = bool(cfg.get("use_macd_exit", False))
        # First Pullback strategy removed — always off
        self.use_first_pullback     = False
        self.use_orb_strategy       = bool(cfg.get("use_orb_strategy", True))
        self.fp_time_limit_hhmm     = int(cfg.get("fp_time_limit_hhmm", 1130))
        self.fp_max_stop_cents      = float(cfg.get("fp_max_stop_cents", 0.30))
        self.fp_min_pole_pct        = float(cfg.get("fp_min_pole_pct", 1.0))
        # Scanner cadence
        self.scan_interval_sec      = float(cfg.get("scan_interval_sec", 60.0))
        self.min_gap_pct            = float(cfg.get("min_gap_pct", 2.0))
        self.min_realtime_rvol      = float(cfg.get("min_realtime_rvol", 2.0))
        self.min_rel_vol            = float(cfg.get("min_rel_vol", 2.5))
        self.min_price              = float(cfg.get("min_price", 1.00))
        self.max_price              = float(cfg.get("max_price", 10.00))
        self.max_float              = float(cfg.get("max_float", 100_000_000))
        # Pending-entry fill detection
        self._pending_timeout_sec   = 120.0   # cancel an unfilled limit entry after this
        # Risk management config
        self.max_daily_loss_pct     = float(cfg.get("max_daily_loss_pct", 5.0))  # % of account
        self.partial_exit_enabled   = bool(cfg.get("partial_exit_enabled", True))
        self.partial_exit_r         = float(cfg.get("partial_exit_r", 1.0))      # scale at +1R
        self.partial_exit_pct       = float(cfg.get("partial_exit_pct", 50.0))   # sell 50%
        self.runner_target_r        = float(cfg.get("runner_target_r", 3.0))     # runner aims here after the partial
        self.eod_flat_enabled       = True
        self.eod_flat_hhmm          = FLAT_HHMM   # 15:58 ET
        self.eod_flat_timezone      = "America/New_York"
        self._kill_switch_tripped   = False
        self._eod_flattened         = False
        self._reconcile_interval    = 60.0  # seconds between broker reconciliations
        self._last_reconcile        = 0.0
        self._swing_results:   List[Dict] = []
        self._swing_scan_interval = float(cfg.get("swing_scan_interval_sec", 60.0))
        self._last_swing_scan  = 0.0
        self.swing_min_swings    = int(cfg.get("swing_min_swings",   3))
        self.swing_min_swing_pct = float(cfg.get("swing_min_swing_pct", 0.5))
        self.swing_min_avg_vol   = int(cfg.get("swing_min_avg_vol", 100_000))
        self._vol_cache:       Dict[str, float] = {}  # avg vol/min cached from ORB candles
        self._last_prices:     Dict[str, float] = {}  # most recent quote price per symbol
        # News sentiment gate — blocks LONG entries on bearish catalysts (all strategies)
        self.news_sentiment_enabled = bool(cfg.get("news_sentiment_enabled", True))
        self.news_block_score       = float(cfg.get("news_block_score", -40))
        self.news_cautious_score    = float(cfg.get("news_cautious_score", -10))
        self.news_boost_score       = float(cfg.get("news_boost_score", 25))
        self.news_max_age_hours     = float(cfg.get("news_max_age_hours", 24))
        self.news_size_boost_pct    = float(cfg.get("news_size_boost_pct", 0.15))
        self.news_cache_ttl_sec     = float(cfg.get("news_cache_ttl_sec", 900))
        self._news_sentiment = NewsSentimentService(
            data_dir="saved_data",
            block_score=self.news_block_score,
            cautious_score=self.news_cautious_score,
            boost_score=self.news_boost_score,
            max_age_hours=self.news_max_age_hours,
            size_boost_pct=self.news_size_boost_pct,
            cache_ttl_sec=self.news_cache_ttl_sec,
        )
        self._last_news_refresh: float = 0.0

        self._watchlist:     List[str] = self.data_handler.load_watchlist()
        self._scan_results:  List[Dict] = []
        self._active_symbols: List[str] = []
        # Divergence-only symbol universe (decoupled from gap/momentum scanner)
        self._div_symbols:      List[str] = []
        self._div_scan_results: List[Dict] = []
        self._div_scan_interval = float(cfg.get("div_scan_interval_sec", 60.0))
        self._last_div_scan     = 0.0
        self.div_scan_max       = int(cfg.get("div_scan_max_symbols", 20))

        self.sim_mode        = False  # removed — paper book uses live Schwab data
        self._last_loop_time: float = 0.0
        self._stop_event     = threading.Event()
        self._trade_thread:  Optional[threading.Thread] = None
        self._scan_thread:   Optional[threading.Thread] = None

        self._callbacks: Dict[str, List[Callable[[Any], None]]] = {}
        self._cb_lock    = threading.Lock()
        self._state_lock = threading.Lock()

        # Wire scanner results back
        self.scanner.on_results = self._on_scan_results

        # Apply config to sub-components
        self._apply_config()
        found = []
        for key, sid in STRATEGY_ENABLE_KEYS.items():
            if getattr(self, key, False) and sid not in found:
                found.append(sid)
        if len(found) > 1:
            keep = found[0]
            log_message(f"[PAPER] multiple strategies were on — keeping {keep}, disabling the rest")
            for k, v in self._flags_for_strategy(keep).items():
                setattr(self, k, v)
            self._apply_config()

        # Build strategy registry so dashboard and API can enumerate strategies
        self._strategy_registry = self._build_strategy_registry()

        # Restore open + closed positions from last session
        self._restore_positions()
        self._restore_closed_positions()

        # Auto-start the trade loop if configured (avoids requiring manual 'Start Bot' click)
        # Only auto-start if the bot was running when it was last stopped.
        # Defaults to False so a fresh deploy or explicit stop stays stopped.
        self._schwab_client.paper_lock = True
        self.options_strategy.paper = self.paper
        self.scanner.should_scan = lambda: bool(self.use_orb_strategy)
        try:
            _pcfg = self.data_handler.load_config()
            _pcfg["dry_run"] = True
            _pcfg["paper_mode"] = True
            _pcfg["options_dry_run"] = True
            _pcfg["auto_trade"] = True
            _pcfg["account_size"] = float(self.paper.starting_cash)
            _pcfg["extended_hours"] = False
            _pcfg["eod_flat_enabled"] = True
            _pcfg["eod_flat_hhmm"] = FLAT_HHMM
            _pcfg["eod_flat_timezone"] = "America/New_York"
            _pcfg["options_eod_flat"] = True
            _pcfg["options_last_entry_hhmm"] = FLAT_HHMM
            _pcfg["sim_mode_enabled"] = False
            self.data_handler.save_config(_pcfg)
        except Exception:
            pass
        if bool(cfg.get("auto_start_trading", False)):  # process lock: default off unless config says on
            self.start()

    # ------------------------------------------------------------------
    # Event bus
    # ------------------------------------------------------------------

    def on(self, event: str, cb: Callable[[Any], None]) -> None:
        with self._cb_lock:
            self._callbacks.setdefault(event, []).append(cb)

    def emit(self, event: str, payload: Any = None) -> None:
        with self._cb_lock:
            cbs = list(self._callbacks.get(event, []))
        for cb in cbs:
            try:
                cb(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _apply_config(self) -> None:
        self.strategy.set_config(
            account_size=self.account_size,
            risk_pct=self.risk_pct,
            orb_minutes=self.orb_minutes,
            max_trades=self.max_trades,
            max_concurrent_positions=self.max_concurrent,
            rr_target=self.rr_target,
            entry_cutoff_hour=self.entry_cutoff_hour,
            require_vwap_above=self.require_vwap_above,
            confirm_close=self.confirm_close,
            require_volume_confirm=self.require_volume_confirm,
            min_breakout_rel_vol=self.min_breakout_rel_vol,
            use_rsi_filter=self.use_rsi_filter,
            rsi_overbought=self.rsi_overbought,
            rsi_oversold=self.rsi_oversold,
            use_atr_stops=self.use_atr_stops,
            atr_stop_mult=self.atr_stop_mult,
            use_macd_filter=self.use_macd_filter,
            use_adx_filter=self.use_adx_filter,
            adx_min=self.adx_min,
            use_htf_filter=self.use_htf_filter,
            use_macd_exit=self.use_macd_exit,
            min_stop_dist=self.min_stop_dist,
            require_orb_above_vwap=self.require_orb_above_vwap,
            orb_min_range_pct=self.orb_min_range_pct,
            orb_max_range_pct=self.orb_max_range_pct,
            orb_max_chase_pct=self.orb_max_chase_pct,
            orb_max_float=self.orb_max_float_shares,
            require_pullback=self.require_pullback,
            orb_pullback_min_pct=self.orb_pullback_min_pct,
            orb_pullback_max_pct=self.orb_pullback_max_pct,
            orb_pullback_reclaim_pct=self.orb_pullback_reclaim_pct,
            orb_pullback_max_chase_pct=self.orb_pullback_max_chase_pct,
            orb_momentum_vol_min=self.orb_momentum_vol_min,
            orb_momentum_rel_vol_min=self.orb_momentum_rel_vol_min,
            orb_fp_min_pole_pct=self.orb_fp_min_pole_pct,
            orb_fp_max_retrace_pct=self.orb_fp_max_retrace_pct,
        )
        self.scanner.set_watchlist(self._watchlist or self.scanner.watchlist)
        self.scanner.set_config(
            min_gap_pct=self.min_gap_pct,
            min_rel_vol=self.min_rel_vol,
            min_price=self.min_price,
            max_price=self.max_price,
            max_float=self.max_float,
            scan_interval_sec=self.scan_interval_sec,
            min_realtime_rvol=self.min_realtime_rvol,
        )
        self.swing_scanner.set_config(
            max_retrace_pct=self.swing_max_retrace_pct,
            min_swings=self.swing_min_swings,
            min_swing_pct=self.swing_min_swing_pct,
            min_avg_vol=self.swing_min_avg_vol,
            max_price=(self.max_price if getattr(self, "max_price", 0) else 500.00),
            min_price=getattr(self, "min_price", 1.00),
        )
        self.fp_strategy.set_config(
            enabled=self.use_first_pullback,
            account_size=self.account_size,
            risk_pct=self.risk_pct,
            rr_target=getattr(self.strategy, "rr_target", 2.0),
            time_limit_hhmm=self.fp_time_limit_hhmm,
            use_macd_filter=self.use_macd_filter,
            use_rsi_filter=self.use_rsi_filter,
            rsi_overbought=self.rsi_overbought,
            max_stop_cents=self.fp_max_stop_cents,
            min_stop_dist=self.min_stop_dist,
            min_pole_pct=self.fp_min_pole_pct,
            max_retrace_pct=self.fp_max_retrace_pct,
        )
        self.scalp_strategy.set_config(
            enabled=self.scalp_enabled,
            symbol=self.scalp_symbol,
            dollar_per_trade=self.scalp_dollar_per_trade,
            session_budget=self.scalp_session_budget,
            target_pct=self.scalp_target_pct,
            stop_pct=self.scalp_stop_pct,
            rr_ratio=self.scalp_rr_ratio,
            cooldown_sec=self.scalp_cooldown_sec,
            max_trades=self.scalp_max_trades,
            breakout_bars=self.scalp_breakout_bars,
            candle_minutes=self.scalp_candle_minutes,
            min_vol_mult=self.scalp_min_vol_mult,
            rsi_min=self.scalp_rsi_min,
            rsi_max=self.scalp_rsi_max,
        )
        self.scalp_screener.candle_minutes = self.scalp_candle_minutes
        self.scalp_screener.client         = self.client
        if hasattr(self, "_news_sentiment") and self._news_sentiment:
            self._news_sentiment.update_config(
                block_score=self.news_block_score,
                cautious_score=self.news_cautious_score,
                boost_score=self.news_boost_score,
                max_age_hours=self.news_max_age_hours,
                size_boost_pct=self.news_size_boost_pct,
                cache_ttl_sec=self.news_cache_ttl_sec,
            )
        self.div_strategy.set_config(
            enabled=self.use_div_strategy,
            account_size=self.account_size,
            risk_pct=self.risk_pct,
            bb_period=self.div_bb_period,
            bb_std_mult=self.div_bb_std_mult,
            div_lookback=self.div_lookback,
            pivot_bars=self.div_pivot_bars,
            min_rsi_div=self.div_min_rsi_div,
            max_rsi_entry=self.div_max_rsi_entry,
            band_proximity=self.div_band_proximity,
            use_macd_confirm=self.div_use_macd_confirm,
            entry_cutoff_hhmm=self.entry_cutoff_hour * 100,
            premarket_start_hhmm=400,
            allow_short=getattr(self.strategy, 'allow_short', False),
            stop_buffer_pct=self.div_stop_buffer_pct,
            min_stop_pct=self.div_min_stop_pct,
            max_stop_pct=self.div_max_stop_pct,
            max_hold_days=self.div_max_hold_days,
            resample_minutes=self.div_resample_minutes,
            lookback_days=self.div_lookback_days,
        )
        self.rsi_strategy.set_config(
            account_size=self.account_size,
            risk_pct=self.risk_pct,
            rsi_entry_threshold=self.rsi_entry_threshold,
            rsi_partial_exit=self.rsi_partial_exit,
            rsi_runner_exit=self.rsi_runner_exit,
            initial_stop_pct=self.rsi_initial_stop_pct,
            trailing_stop_pct=self.rsi_trail_stop_pct,
            trail_trigger_pct=self.rsi_trail_trigger_pct,
            max_hold_days=self.rsi_max_hold_days,
            scan_interval_sec=self.rsi_scan_interval_sec,
            min_avg_vol=self.rsi_min_avg_vol,
            min_price=self.rsi_min_price,
            max_price=self.rsi_max_price,
            max_trades_per_day=self.rsi_max_trades,
            max_concurrent=self.rsi_max_concurrent,
            rsi_timeframe=self.rsi_timeframe,
        )
        self.use_options_confirm = bool(
            getattr(self, "use_options_confirm", False) or getattr(self, "options_enabled", False)
        )
        self.options_enabled = self.use_options_confirm
        _owl = getattr(self, "options_watchlist", []) or []
        if isinstance(_owl, str):
            self.options_watchlist = [x.strip().upper() for x in _owl.split(",") if x.strip()]
        else:
            self.options_watchlist = [str(x).upper() for x in _owl]
        self.options_strategy.set_config(
            use_options_confirm=self.use_options_confirm,
            options_dry_run=self.options_dry_run,
            options_expiry_mode=self.options_expiry_mode,
            options_max_premium=self.options_max_premium,
            options_max_contracts=self.options_max_contracts,
            options_max_concurrent=self.options_max_concurrent,
            options_strike=self.options_strike,
            options_eod_flat=self.options_eod_flat,
            options_last_entry_hhmm=self.options_last_entry_hhmm,
            options_watchlist=self.options_watchlist,
        )
        self.options_strategy.client = self._schwab_client
        self.options_strategy.universe.client = self._schwab_client
        self.options_strategy.paper = getattr(self, "paper", None)
        self.account_size = float(getattr(self.paper, "starting_cash", PAPER_STARTING_CASH))
        self.dry_run = True
        self.paper_mode = True
        self.options_dry_run = True
        self.extended_hours = False
        self.eod_flat_enabled = True
        self.eod_flat_hhmm = FLAT_HHMM
        self.eod_flat_timezone = "America/New_York"
        self.options_eod_flat = True
        self.options_last_entry_hhmm = FLAT_HHMM
        self._schwab_client.paper_lock = True

    @staticmethod
    def _flags_for_strategy(strategy_id: str) -> Dict[str, bool]:
        flags = {
            "use_orb_strategy": False,
            "use_div_strategy": False,
            "swing_scan_enabled": False,
            "scalp_enabled": False,
            "rsi_enabled": False,
            "use_options_confirm": False,
            "options_enabled": False,
            "use_first_pullback": False,
        }
        mapping = {
            "orb": {"use_orb_strategy": True},
            "divergence": {"use_div_strategy": True},
            "swing": {"swing_scan_enabled": True},
            "scalp": {"scalp_enabled": True},
            "rsi": {"rsi_enabled": True},
            "opt_confirm": {"use_options_confirm": True, "options_enabled": True},
            "options": {"use_options_confirm": True, "options_enabled": True},
        }
        flags.update(mapping.get(strategy_id, {}))
        return flags

    def active_strategy(self) -> Optional[str]:
        if getattr(self, "use_orb_strategy", False):
            return "orb"
        if getattr(self, "use_div_strategy", False):
            return "divergence"
        if getattr(self, "swing_scan_enabled", False):
            return "swing"
        if getattr(self, "scalp_enabled", False):
            return "scalp"
        if getattr(self, "rsi_enabled", False):
            return "rsi"
        if getattr(self, "use_options_confirm", False) or getattr(self, "options_enabled", False):
            return "opt_confirm"
        return None

    def select_strategy(self, strategy_id: str) -> None:
        self.set_config(**self._flags_for_strategy(strategy_id))

    def set_config(self, **kwargs) -> None:
        kwargs = dict(kwargs)
        kwargs["dry_run"] = True
        kwargs["paper_mode"] = True
        kwargs["options_dry_run"] = True
        kwargs.pop("account_size", None)
        chosen = None
        for key, sid in STRATEGY_ENABLE_KEYS.items():
            if key in kwargs and bool(kwargs[key]):
                chosen = sid
        if chosen:
            kwargs.update(self._flags_for_strategy(chosen))
        with self._state_lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)
        self._apply_config()
        self.rsi_strategy.enabled = self.rsi_enabled
        self.data_handler.save_config(self._build_config_dict())
        active = self.active_strategy()
        self.emit("log", f"Configuration updated. Paper book ${self.account_size:,.0f}. Strategy: {active or 'none'}.")

    def _build_config_dict(self) -> Dict:
        return {
            "app_key":                  self._schwab_client.app_key,
            "app_secret":               self._schwab_client.app_secret,
            "account_size":             float(getattr(self, "paper", None).starting_cash if getattr(self, "paper", None) else PAPER_STARTING_CASH),
            "risk_pct":                 self.risk_pct,
            "orb_minutes":              self.orb_minutes,
            "max_trades_per_day":       self.max_trades,
            "max_concurrent_positions": self.max_concurrent,
            "rr_target":                self.rr_target,
            "entry_cutoff_hour":        self.entry_cutoff_hour,
            "require_vwap_above":       self.require_vwap_above,
            "auto_trade":               self.auto_trade,
            "dry_run":                  True,
            "paper_mode":               True,
            "extended_hours":           self.extended_hours,
            "swing_scan_enabled":       self.swing_scan_enabled,
            "trail_trigger_r":          self.trail_trigger_r,
            "trail_vol_mult":           self.trail_vol_mult,
            "trail_pct":                self.trail_pct,
            "limit_entry_buffer":       round(self.limit_entry_buffer * 100, 2),
            "confirm_close":            self.confirm_close,
            "require_volume_confirm":   self.require_volume_confirm,
            "min_breakout_rel_vol":     self.min_breakout_rel_vol,
            "use_rsi_filter":           self.use_rsi_filter,
            "rsi_overbought":           self.rsi_overbought,
            "rsi_oversold":             self.rsi_oversold,
            "use_atr_stops":            self.use_atr_stops,
            "atr_stop_mult":            self.atr_stop_mult,
            "max_daily_loss_pct":       self.max_daily_loss_pct,
            "partial_exit_enabled":     self.partial_exit_enabled,
            "partial_exit_r":           self.partial_exit_r,
            "partial_exit_pct":         self.partial_exit_pct,
            "runner_target_r":          self.runner_target_r,
            "eod_flat_enabled":         self.eod_flat_enabled,
            "eod_flat_hhmm":            self.eod_flat_hhmm,
            "eod_flat_timezone":        self.eod_flat_timezone,
            "use_macd_filter":          self.use_macd_filter,
            "use_adx_filter":           self.use_adx_filter,
            "adx_min":                  self.adx_min,
            "use_htf_filter":           self.use_htf_filter,
            "use_5min_entry_rules":     self.use_5min_entry_rules,
            "htf_sma_period":           self.htf_sma_period,
            "require_5min_vol_above_avg": self.require_5min_vol_above_avg,
            "require_dip_entry":        self.require_dip_entry,
            "dip_sma_touch_pct":        self.dip_sma_touch_pct,
            "use_macd_exit":            self.use_macd_exit,
            "use_first_pullback":       self.use_first_pullback,
            "use_orb_strategy":         self.use_orb_strategy,
            "fp_time_limit_hhmm":       self.fp_time_limit_hhmm,
            "fp_max_stop_cents":        self.fp_max_stop_cents,
            "fp_min_pole_pct":          self.fp_min_pole_pct,
            "scan_interval_sec":        self.scan_interval_sec,
            "min_gap_pct":              self.min_gap_pct,
            "min_realtime_rvol":        self.min_realtime_rvol,
            "min_rel_vol":              self.min_rel_vol,
            "min_price":                self.min_price,
            "max_price":                self.max_price,
            "max_float":                self.max_float,
            "intraday_max_retrace_pct": self.intraday_max_retrace_pct,
            "swing_max_retrace_pct":    self.swing_max_retrace_pct,
            "fp_max_retrace_pct":       self.fp_max_retrace_pct,
            # Quality filters & new strategy fields (added incrementally)
            "use_market_filter":         self.use_market_filter,
            "market_filter_symbol":      self.market_filter_symbol,
            "require_orb_above_vwap":    self.require_orb_above_vwap,
            "orb_min_range_pct":         self.orb_min_range_pct,
            "orb_max_range_pct":         self.orb_max_range_pct,
            "orb_max_chase_pct":         self.orb_max_chase_pct,
            "orb_max_float":             self.orb_max_float_shares,
            "min_stop_dist":             self.min_stop_dist,
            "time_decay_start_hhmm":     self.time_decay_start_hhmm,
            "time_decay_factor":         self.time_decay_factor,
            "swing_scan_enabled":        self.swing_scan_enabled,
            "swing_max_retrace_pct":     self.swing_max_retrace_pct,
            "swing_min_swings":          self.swing_min_swings,
            "swing_min_swing_pct":       self.swing_min_swing_pct,
            "swing_min_avg_vol":         self.swing_min_avg_vol,
            "use_div_strategy":          self.use_div_strategy,
            "rsi_enabled":             self.rsi_enabled,
            "rsi_entry_threshold":     self.rsi_entry_threshold,
            "rsi_partial_exit":        self.rsi_partial_exit,
            "rsi_runner_exit":         self.rsi_runner_exit,
            "rsi_initial_stop_pct":    self.rsi_initial_stop_pct,
            "rsi_trail_stop_pct":      self.rsi_trail_stop_pct,
            "rsi_trail_trigger_pct":   self.rsi_trail_trigger_pct,
            "rsi_max_hold_days":       self.rsi_max_hold_days,
            "rsi_scan_interval_sec":   self.rsi_scan_interval_sec,
            "rsi_min_avg_vol":         self.rsi_min_avg_vol,
            "rsi_min_price":           self.rsi_min_price,
            "rsi_max_price":           self.rsi_max_price,
            "rsi_max_trades":          self.rsi_max_trades,
            "rsi_max_concurrent":      self.rsi_max_concurrent,
            "rsi_timeframe":           self.rsi_timeframe,
            "scalp_enabled":             self.scalp_enabled,
            "scalp_symbol":              self.scalp_symbol,
            "scalp_dollar_per_trade":    self.scalp_dollar_per_trade,
            "scalp_session_budget":      self.scalp_session_budget,
            "scalp_target_pct":          self.scalp_target_pct,
            "scalp_stop_pct":            self.scalp_stop_pct,
            "scalp_rr_ratio":            self.scalp_rr_ratio,
            "scalp_cooldown_sec":        self.scalp_cooldown_sec,
            "scalp_max_trades":          self.scalp_max_trades,
            "scalp_breakout_bars":       self.scalp_breakout_bars,
            "scalp_min_vol_mult":        self.scalp_min_vol_mult,
            "scalp_rsi_min":             self.scalp_rsi_min,
            "scalp_rsi_max":             self.scalp_rsi_max,
            "div_bb_period":             self.div_bb_period,
            "div_bb_std_mult":           self.div_bb_std_mult,
            "div_lookback":              self.div_lookback,
            "div_pivot_bars":            self.div_pivot_bars,
            "div_min_rsi_div":           self.div_min_rsi_div,
            "div_max_rsi_entry":         self.div_max_rsi_entry,
            "div_band_proximity":        self.div_band_proximity,
            "div_use_macd_confirm":      self.div_use_macd_confirm,
            "div_stop_buffer_pct":       self.div_stop_buffer_pct,
            "div_min_stop_pct":          self.div_min_stop_pct,
            "div_max_stop_pct":          self.div_max_stop_pct,
            "div_max_hold_days":         self.div_max_hold_days,
            "div_resample_minutes":      self.div_resample_minutes,
            "div_lookback_days":         self.div_lookback_days,
            "news_sentiment_enabled":    self.news_sentiment_enabled,
            "news_block_score":          self.news_block_score,
            "news_cautious_score":       self.news_cautious_score,
            "news_boost_score":          self.news_boost_score,
            "news_max_age_hours":        self.news_max_age_hours,
            "news_size_boost_pct":       self.news_size_boost_pct,
            "news_cache_ttl_sec":        self.news_cache_ttl_sec,
            "auto_start_trading":        self.trading_active,
            "sim_mode_enabled":          False,
            "use_options_confirm":       self.use_options_confirm,
            "options_enabled":           self.use_options_confirm,
            "options_dry_run":           self.options_dry_run,
            "options_expiry_mode":       self.options_expiry_mode,
            "options_max_premium":       self.options_max_premium,
            "options_max_contracts":     self.options_max_contracts,
            "options_max_concurrent":    self.options_max_concurrent,
            "options_strike":            self.options_strike,
            "options_eod_flat":          self.options_eod_flat,
            "options_last_entry_hhmm":   self.options_last_entry_hhmm,
            "options_watchlist":         self.options_watchlist,
            "options_scan_interval_sec": self.options_scan_interval_sec,
        }

    def set_api_keys(self, app_key: str, app_secret: str) -> None:
        # Always update the real Schwab client — self.client may be SimClient in sim mode.
        self._schwab_client.app_key    = app_key
        self._schwab_client.app_secret = app_secret
        self.data_handler.save_config(self._build_config_dict())
        self.emit("log", "API keys updated.")

    def get_watchlist(self) -> List[str]:
        return list(self._watchlist)

    def set_watchlist(self, symbols: List[str]) -> None:
        self._watchlist = symbols
        self.scanner.set_watchlist(symbols)
        self.data_handler.save_watchlist(symbols)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authorize(self) -> bool:
        result = self._schwab_client.authorize()
        if result:
            self.emit("log", "Schwab authorization successful.")
            self.emit("auth_status", True)
        else:
            self.emit("log", "Schwab authorization failed.")
            self.emit("auth_status", False)
        return result

    def is_authenticated(self) -> bool:
        return self._schwab_client.is_authenticated()

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hhmm(dt: datetime.datetime) -> int:
        return dt.hour * 100 + dt.minute

    def _in_premarket(self, now: datetime.datetime) -> bool:
        hm = self._hhmm(now)
        start = SESSION_PREMARKET_START[0] * 100  + SESSION_PREMARKET_START[1]
        end   = SESSION_PREMARKET_END[0]   * 100  + SESSION_PREMARKET_END[1]
        return start <= hm < end

    def _in_regular(self, now: datetime.datetime) -> bool:
        return in_scan_window(now)

    def _in_afterhours(self, now: datetime.datetime) -> bool:
        hm = self._hhmm(now)
        start = SESSION_AFTERHOURS_START[0] * 100 + SESSION_AFTERHOURS_START[1]
        end   = SESSION_AFTERHOURS_END[0]   * 100 + SESSION_AFTERHOURS_END[1]
        return start <= hm < end

    def _in_any_session(self, now: datetime.datetime) -> bool:
        if self._in_regular(now):
            return True
        if self.extended_hours and (self._in_premarket(now) or self._in_afterhours(now)):
            return True
        return False

    def _is_extended_hours_now(self, now: datetime.datetime) -> bool:
        return self.extended_hours and (self._in_premarket(now) or self._in_afterhours(now))

    def _session_name(self, now: datetime.datetime) -> str:
        return session_label(now)

    def _in_div_trading_window(self, now: datetime.datetime) -> bool:
        """Divergence scan/eval: weekday 9:30–4:00 PM ET only."""
        if not self.use_div_strategy:
            return False
        return in_scan_window(now)

    def _get_now(self) -> datetime.datetime:
        return datetime.datetime.now(_ET)

    
    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Strategy registry
    # ------------------------------------------------------------------

    def _build_strategy_registry(self) -> StrategyRegistry:
        reg = StrategyRegistry()
        reg.register(_StrategyDescriptor(
            strategy_id="orb",
            name="ORB Breakout",
            tab_id="orb",
            description="Opening Range Breakout — buys above the first-candle high.",
            enabled_getter=lambda: self.use_orb_strategy,
            enabled_setter=lambda v: self.select_strategy("orb") if v else self.set_config(use_orb_strategy=False),
        ))
        reg.register(_StrategyDescriptor(
            strategy_id="divergence",
            name="Divergence",
            tab_id="divergence",
            description="30-min RSI divergence near lower Bollinger Band.",
            enabled_getter=lambda: self.use_div_strategy,
            enabled_setter=lambda v: self.select_strategy("divergence") if v else self.set_config(use_div_strategy=False),
            candidates_getter=lambda: list(getattr(self, "_div_scan_results", [])),
        ))
        reg.register(_StrategyDescriptor(
            strategy_id="swing",
            name="Swing",
            tab_id="swing",
            description="Intraday oscillation entries at support/resistance.",
            enabled_getter=lambda: self.swing_scan_enabled,
            enabled_setter=lambda v: self.select_strategy("swing") if v else self.set_config(swing_scan_enabled=False),
            candidates_getter=lambda: list(getattr(self, "_swing_results", [])),
        ))
        reg.register(_StrategyDescriptor(
            strategy_id="scalp",
            name="Scalp",
            tab_id="scalp",
            description="Single-symbol immediate-entry or breakout scalp.",
            enabled_getter=lambda: self.scalp_enabled,
            enabled_setter=lambda v: self.select_strategy("scalp") if v else self.set_config(scalp_enabled=False),
        ))
        reg.register(_StrategyDescriptor(
            strategy_id="rsi",
            name="RSI Mean Reversion",
            tab_id="rsi",
            description="2-week uptrend stocks with daily RSI ≤20.",
            enabled_getter=lambda: self.rsi_strategy.enabled,
            enabled_setter=lambda v: self.select_strategy("rsi") if v else self.set_config(rsi_enabled=False),
            candidates_getter=lambda: self.rsi_strategy.get_candidates(),
            tab_data_getter=lambda: self.rsi_strategy.get_tab_data(),
        ))
        reg.register(_StrategyDescriptor(
            strategy_id="opt_confirm",
            name="Options Confirm",
            tab_id="options",
            description="5-min close through a level. Next Wed/Fri weekly. Stop on the stock.",
            enabled_getter=lambda: self.use_options_confirm,
            enabled_setter=lambda v: self.select_strategy("opt_confirm") if v else self.set_config(use_options_confirm=False, options_enabled=False),
            candidates_getter=lambda: self.options_strategy.get_candidates(),
            tab_data_getter=lambda: self.options_strategy.get_tab_data(),
        ))
        return reg

    def get_strategy_status(self):
        return self._strategy_registry.get_status()

    # ------------------------------------------------------------------
    # Trading loop start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self.trading_active:
            return
        self.trading_active = True
        # Persist running state — survives service restarts
        try:
            cfg = self.data_handler.load_config()
            cfg["auto_start_trading"] = True
            self.data_handler.save_config(cfg)
        except Exception:
            pass
        self._stop_event.clear()
        self.strategy.reset_day()

        self._trade_thread = threading.Thread(
            target=self._trade_loop, daemon=True, name="trade_loop"
        )
        self._trade_thread.start()

        self.scanner.start()
        # Arm scalp for immediate first entry if scalp mode is on
        self.scalp_strategy.arm_immediate_entry()
        self.emit("log", "Trading engine started.")
        self.emit("status", "running")

    def stop(self) -> None:
        self.trading_active = False
        self._stop_event.set()
        # Persist stopped state — next launch starts stopped
        try:
            cfg = self.data_handler.load_config()
            cfg["auto_start_trading"] = False
            self.data_handler.save_config(cfg)
        except Exception:
            pass
        self.scanner.stop()
        self.emit("log", "Trading engine stopped.")
        self.emit("status", "stopped")

    # ------------------------------------------------------------------
    # Main trade loop
    # ------------------------------------------------------------------

    def _trade_loop(self) -> None:
        iteration    = 0
        loop_date    = None

        while not self._stop_event.is_set():
            try:
                self._last_loop_time = time.time()
                now    = self._get_now()
                hm     = self._hhmm(now)
                in_reg = self._in_regular(now)
                can_scan = in_scan_window(now)
                can_enter = in_entry_window(now) and not self._eod_flattened

                self.strategy.reset_day()
                self.fp_strategy.reset_day()
                self.div_strategy.reset_day()
                today = now.date().isoformat()
                if loop_date != today:
                    loop_date = today
                    self._kill_switch_tripped = False
                    self._eod_flattened       = False

                session = self._session_name(now)
                if iteration % 10 == 0:
                    self.emit("log", f"[{session}] {now.strftime('%H:%M:%S')} — engine tick")

                orb_cutoff = 930 + self.orb_minutes

                # --- CRITICAL: fill detection runs first, isolated from other crashes ---
                try:
                    if not self.dry_run and self.strategy.get_pending_positions():
                        self._check_pending_fills()
                except Exception as e:
                    log_message(f"[ENGINE] Fill detection error: {e}")

                # --- ORB level load (regular session) ---
                try:
                    orb_eval_active = self.use_orb_strategy
                    if orb_eval_active and can_scan and hm >= orb_cutoff and self._active_symbols:
                        self._load_orb_levels(only_missing=True)
                except Exception as e:
                    log_message(f"[ENGINE] ORB level load error: {e}")

                # --- Divergence candidate discovery (only if enabled, weekday RTH) ---
                try:
                    if self.use_div_strategy and can_scan:
                        if time.time() - self._last_div_scan > self._div_scan_interval:
                            self._run_div_scan()
                except Exception as e:
                    log_message(f"[ENGINE] Div scan error: {e}")

                # --- Divergence evaluation: regular session ---
                try:
                    if self.use_div_strategy and can_enter and self._div_symbols:
                        self._poll_div_evaluate(extended=False)
                except Exception as e:
                    log_message(f"[ENGINE] Div regular eval error: {e}")

                # --- News sentiment refresh for active symbols ---
                try:
                    if getattr(self, "news_sentiment_enabled", False):
                        ns = getattr(self, "_news_sentiment", None)
                        if ns and time.time() - getattr(self, "_last_news_refresh", 0.0) > self.news_cache_ttl_sec:
                            syms = list({
                                *(self._active_symbols or []),
                                *(self._div_symbols or []),
                                *(
                                    [self.scalp_strategy.symbol]
                                    if self.scalp_strategy.enabled and self.scalp_strategy.symbol
                                    else []
                                ),
                            })
                            if syms:
                                ns.refresh_symbols(syms[:15])
                            self._last_news_refresh = time.time()
                except Exception as e:
                    log_message(f"[NEWS] Refresh error: {e}")

                # --- Swing scan (only if enabled) ---
                try:
                    if can_scan and self.swing_scan_enabled:
                        if time.time() - self._last_swing_scan > self._swing_scan_interval:
                            self._run_swing_scan()
                except Exception as e:
                    log_message(f"[ENGINE] Swing scan error: {e}")

                # --- RSI Mean Reversion scan + evaluate (only if enabled) ---
                try:
                    if (self.rsi_strategy.enabled and can_scan and
                            time.time() - self._last_rsi_scan > self.rsi_scan_interval_sec):
                        self._run_rsi_scan()
                except Exception as _re:
                    log_message(f"[RSI] Scan error: {_re}")

                try:
                    if self.rsi_strategy.enabled and can_enter and self.rsi_strategy.get_candidates():
                        self._poll_rsi_evaluate()
                except Exception as _re:
                    log_message(f"[RSI] Evaluate error: {_re}")

                # --- Scalp screener (only if enabled) ---
                try:
                    if self.scalp_enabled and can_scan and time.time() - self._last_scalp_scan > self.scalp_scan_interval_sec:
                        self._run_scalp_scan()
                except Exception as _se:
                    log_message(f"[SCALP-SCREEN] scan error: {_se}")

                # --- Scalp polling (RTH weekday, enabled only) ---
                try:
                    if self.scalp_strategy.enabled and can_enter and self.scalp_strategy.symbol:
                        self._poll_scalp()
                except Exception as e:
                    log_message(f"[SCALP] Poll error: {e}")

                # --- Support / resistance refresh for scanner hits ---
                try:
                    syms = list(dict.fromkeys(
                        [r.get("symbol") for r in (self._scan_results or [])]
                        + list(self._active_symbols or [])
                        + [p.get("symbol") for p in self.options_strategy.open_positions()]
                    ))
                    self.sr_engine.refresh(
                        self.client, [s for s in syms if s],
                        candle_fn=lambda s: self.scanner._get_today_candles(s),
                        max_n=8,
                    )
                    for r in (self._scan_results or []):
                        self.sr_engine.attach(r)
                except Exception as e:
                    log_message(f"[SR] refresh error: {e}")

                # --- Options confirm sleeve (only if enabled, weekday RTH) ---
                try:
                    if self.use_options_confirm and can_scan:
                        if time.time() - self._last_options_scan > self.options_scan_interval_sec:
                            self._run_options_scan()
                        if can_enter or self.options_strategy.open_positions():
                            self._poll_options()
                except Exception as e:
                    log_message(f"[OPT] loop error: {e}")

                # --- Quote poll + strategy evaluation (ORB enabled, weekday RTH) ---
                try:
                    if self.use_orb_strategy and self._active_symbols and can_enter and hm >= orb_cutoff:
                        self._poll_and_evaluate(extended=False)
                except Exception as e:
                    log_message(f"[ENGINE] Poll/evaluate error: {e}")

                # --- Broker reconciliation ---
                try:
                    if (not self.dry_run and self.strategy._open_positions
                            and time.time() - self._last_reconcile > self._reconcile_interval):
                        self._reconcile_positions()
                        self._last_reconcile = time.time()
                except Exception as e:
                    log_message(f"[ENGINE] Reconcile error: {e}")

                # --- Daily loss kill switch ---
                try:
                    if not self._kill_switch_tripped and self.auto_trade:
                        day_pnl  = self.strategy.get_day_pnl()
                        max_loss = -abs(self.account_size * (self.max_daily_loss_pct / 100.0))
                        if day_pnl <= max_loss:
                            self._trip_kill_switch(day_pnl, max_loss)
                except Exception as e:
                    log_message(f"[ENGINE] Kill switch check error: {e}")

                # --- End-of-day flatten (always runs, own guard) ---
                try:
                    _eod_tz = ZoneInfo(self.eod_flat_timezone)
                    _eod_now = datetime.datetime.now(_eod_tz)
                    _eod_hm = _eod_now.hour * 100 + _eod_now.minute
                    _eod_lbl = {"America/Chicago": "CT", "America/New_York": "ET"}.get(
                        self.eod_flat_timezone, self.eod_flat_timezone
                    )
                    if (self.eod_flat_enabled and not self._eod_flattened
                            and should_flatten(_eod_now)):
                        self.emit(
                            "log",
                            f"[EOD] {_eod_now.strftime('%H:%M')} {_eod_lbl} — "
                            f"flattening ALL paper trades before the 4:00 PM ET close."
                        )
                        self._flatten_all("eod_flat")
                        self._eod_flattened = True
                except Exception as e:
                    log_message(f"[ENGINE] EOD flatten error: {e}")

                try:
                    refresh_candidate_report(self)
                except Exception as e:
                    log_message(f"[CANDIDATES] refresh error: {e}")

                if iteration % 4 == 0:
                    self._emit_status_update()

                iteration += 1

            except Exception as e:
                log_message(f"[ENGINE] Trade loop error: {e}")
                self.emit("log", f"Trade loop error: {e}")

            self._stop_event.wait(timeout=self.poll_interval)

    # ------------------------------------------------------------------
    # Scanner callback
    # ------------------------------------------------------------------

    def _market_bullish(self) -> bool:
        """Returns True when SPY is above its daily VWAP.
        Bypassed in dry_run mode so tests are not blocked by market conditions.
        """
        if not self.use_market_filter:
            return True
        if self.dry_run:
            return True
        if time.time() - self._market_filter_ts < 300:   # 5-min cache
            return self._market_filter_ok
        try:
            vwap = self.scanner.get_vwap(self.market_filter_symbol)
            q    = self.client.get_quote(self.market_filter_symbol)
            px   = float((q or {}).get("quote", {}).get("lastPrice", 0) or 0)
            self._market_filter_ok = (px > 0 and vwap is not None and px >= vwap)
        except Exception as e:
            log_message(f"[FILTER] Market filter error ({self.market_filter_symbol}): {e}")
            self._market_filter_ok = True   # fail open — don't block on API error
        self._market_filter_ts = time.time()
        if not self._market_filter_ok:
            log_message(
                f"[FILTER] {self.market_filter_symbol} below VWAP — LONG entries paused"
            )
        return self._market_filter_ok

    def _on_scan_results(self, results: List[Dict]) -> None:
        for r in results or []:
            try:
                self.sr_engine.from_quote(r.get("symbol") or "", {
                    "quote": {
                        "lastPrice": r.get("last") or r.get("price"),
                        "highPrice": r.get("resistance") or r.get("day_high"),
                        "lowPrice": r.get("support") or r.get("day_low"),
                    }
                })
                self.sr_engine.attach(r)
            except Exception:
                pass
        self._scan_results = results

        # All qualifying scanner candidates are eligible — entries are gated by
        # max_concurrent_positions / max_trades_per_day / entry_cutoff at signal time.
        new_symbols = [r["symbol"] for r in results]
        # Scalp mode: always keep the chosen symbol monitored (scanner may not pick it up)
        if self.scalp_strategy.enabled and self.scalp_strategy.symbol:
            if self.scalp_strategy.symbol not in new_symbols:
                new_symbols = [self.scalp_strategy.symbol] + new_symbols

        # Preserve symbols that have open positions — they must run to completion
        # regardless of whether the new scan still includes them
        open_syms  = list(self.strategy._open_positions.keys())
        preserved  = [s for s in open_syms if s not in new_symbols]

        prev = set(self._active_symbols) - set(open_syms)  # compare only the scan portion
        self._active_symbols = new_symbols + preserved

        # Invalidate swing results only when the fresh scan set changes
        if set(new_symbols) != prev:
            self._swing_results   = []
            self._last_swing_scan = 0.0

        self.emit("scan_results", results)
        if new_symbols:
            msg = f"Watching {len(new_symbols)} candidates: {new_symbols}"
            if preserved:
                msg += f" | Keeping open positions: {preserved}"
            self.emit("log", msg)
        else:
            self.emit("log", "Scanner found no qualifying stocks. No trades until scan updates.")

    # ------------------------------------------------------------------
    # ORB level loading
    # ------------------------------------------------------------------

    def _poll_scalp(self) -> None:
        """Poll the scalp symbol, evaluate entry, manage open position.
        Runs in ALL trading sessions — pre-market, regular, after-hours.
        """
        sym = self.scalp_strategy.symbol
        if not sym:
            return

        # Make sure scalp symbol is in active symbols so price history loads
        if sym not in self._active_symbols:
            self._active_symbols = [sym] + self._active_symbols

        quotes = self.client.get_quotes([sym])
        data   = quotes.get(sym, {})
        quote  = data.get("quote", {})
        price  = float(quote.get("lastPrice", 0) or 0)
        ask    = float(quote.get("askPrice",  0) or 0)
        if price <= 0:
            return
        self._last_prices[sym] = price

        # Fetch 1-min indicators (for signal-mode and for OCO sizing)
        ind = None
        try:
            ind = self.scanner.get_indicators(sym, self.orb_minutes)
            if ind:
                ind["avg_vol_per_min"] = self._vol_cache.get(sym, 0)
        except Exception as e:
            log_message(f"[SCALP] indicator error for {sym}: {e}")

        # Evaluate entry (only when no open position)
        if sym not in self.strategy._open_positions:
            sig = self.scalp_strategy.evaluate(
                sym, ind or {}, self.strategy._open_positions, price,
                account_size=self.account_size, ask_price=ask,
            )
            if sig:
                self.emit("signal", sig)
                if self.auto_trade:
                    is_ext = self._is_extended_hours_now(self._get_now())
                    self._execute_trade(sig, extended=is_ext)

        # Manage existing open position (partial exits, trailing, reconcile)
        if sym in self.strategy._open_positions:
            self._manage_open_positions({sym: data})

        # --- Screener-mode: act on swing-low-at-support candidates ---
        if self.scalp_strategy.screener_mode:
            self._poll_scalp_screener()


    def _poll_scalp_screener(self) -> None:
        """
        Iterate screener candidates that are at swing-low support on a 2-day uptrend
        and trigger a scalp entry via ScalpStrategy.evaluate_screener_result().
        Only fires when the configured scalp symbol is not already in a position.
        """
        results = self.scalp_screener.get_results()
        if not results:
            return

        # Only act when no active scalp position exists
        active_syms = set(self.strategy._open_positions.keys())

        for candidate in results:
            sym = candidate.get("symbol", "")
            if not sym or sym in active_syms:
                continue
            if not candidate.get("ready"):
                continue

            # Refresh the live price before evaluating
            try:
                quotes = self.client.get_quotes([sym])
                live_px = float(
                    (quotes.get(sym, {}).get("quote", {}) or {}).get("lastPrice", 0) or 0
                )
                if live_px > 0:
                    candidate = {**candidate, "price": live_px}
            except Exception:
                pass

            sig = self.scalp_strategy.evaluate_screener_result(
                candidate,
                open_positions=self.strategy._open_positions,
                account_size=self.account_size,
            )
            if sig:
                self.emit("signal", sig)
                if self.auto_trade:
                    is_ext = self._is_extended_hours_now(self._get_now())
                    self._execute_trade(sig, extended=is_ext)
                break   # one trade at a time from the screener

    def _load_orb_levels(self, only_missing: bool = False) -> None:
        """Load ORB levels + indicators for active symbols.

        When only_missing=True, only symbols that don't yet have ORB levels are
        loaded — this lets symbols that enter the universe intraday still get
        levels instead of being silently un-tradeable.
        """
        targets = [
            s for s in self._active_symbols
            if not (only_missing and self.strategy.has_orb_levels(s))
        ]
        if not targets:
            return

        # Rate-limit retries: don't hammer the candle API for the same symbols
        # every 30s. Only retry a failed symbol after 5 minutes.
        now_ts = time.time()
        if not hasattr(self, "_orb_retry_at"):
            self._orb_retry_at: Dict[str, float] = {}
        targets = [s for s in targets if now_ts >= self._orb_retry_at.get(s, 0)]
        if not targets:
            return

        # Build a quick lookup of scan metadata (rel_vol / gap_pct) by symbol
        scan_by_sym = {r["symbol"]: r for r in self._scan_results}
        log_message(f"[ORB] Loading levels for: {targets}")
        self.emit("log", f"Loading ORB levels for {targets}...")
        for symbol in targets:
            try:
                levels = self.scanner.get_orb_levels(symbol, self.orb_minutes)
                if levels:
                    self.strategy.set_orb_levels(symbol, levels)
                    vwap = self.scanner.get_vwap(symbol)
                    if vwap:
                        self.strategy.set_vwap(symbol, vwap)
                    msg = f"[ORB] {symbol}: H=${levels['high']} L=${levels['low']} VWAP=${vwap or 'N/A'}"
                    log_message(msg)
                    self.emit("log", msg)
                else:
                    log_message(f"[ORB] {symbol}: no candle data from API — retry in 5 min")
                    self._orb_retry_at[symbol] = now_ts + 300
                # Pass scanner relative-volume / gap so signals carry them
                meta = scan_by_sym.get(symbol)
                if meta:
                    self.strategy.set_scan_meta(
                        symbol,
                        meta.get("rel_vol", 0.0),
                        meta.get("gap_pct", 0.0),
                        float_shares=int(meta.get("float", 0) or 0),
                    )
                # Cache average volume per minute for trail upgrade detection
                avg_vol = self.scanner.get_avg_volume_per_min(symbol)
                if avg_vol > 0:
                    self._vol_cache[symbol] = avg_vol
            except Exception as e:
                log_message(f"[ENGINE] ORB load error for {symbol}: {e}")
                self._orb_retry_at[symbol] = now_ts + 300

    # ------------------------------------------------------------------
    # Quote polling and signal evaluation
    # ------------------------------------------------------------------

    def _poll_and_evaluate(self, extended: bool = False) -> None:
        if not self._active_symbols:
            return

        quotes = self.client.get_quotes(self._active_symbols)

        # Emit a per-symbol diagnostic every 10 iterations (~5 min at 30s poll)
        self._eval_iter = getattr(self, "_eval_iter", 0) + 1
        emit_diag = (self._eval_iter % 10 == 1)

        for symbol in self._active_symbols:
            data  = quotes.get(symbol, {})
            quote = data.get("quote", {})
            price = float(quote.get("lastPrice", 0) or 0)
            if price <= 0:
                continue
            self._last_prices[symbol] = price  # cache for UI current-price display (EMA9/EMA20/RSI/ATR/VWAP/breakout vol) from candles
            ind = None  # reset per-symbol so stale data never bleeds across iterations
            try:
                _orb_lv = self.strategy._orb_levels.get(symbol)
                _orb_hi = _orb_lv["high"] if _orb_lv else None
                ind = self.scanner.get_indicators(
                    symbol, self.orb_minutes, orb_high=_orb_hi
                )
                if ind:
                    self.strategy.set_indicators(symbol, ind)
            except Exception as e:
                log_message(f"[ENGINE] indicator calc error for {symbol}: {e}")

            if self.use_orb_strategy:
                signal = self.strategy.evaluate(symbol, price)
                if signal:
                    self.emit("signal", signal)
                    if self.auto_trade:
                        self._execute_trade(signal, extended=extended)
                elif emit_diag:
                    # Log why ORB has not fired for this symbol (only when ORB is enabled)
                    levels  = self.strategy._orb_levels.get(symbol)
                    ind_dbg = self.strategy._indicators.get(symbol, {})
                    rsi     = ind_dbg.get("rsi")
                    ema9    = ind_dbg.get("ema9")
                    ema20   = ind_dbg.get("ema20")
                    vr      = ind_dbg.get("breakout_vol_ratio", 0)
                    vwap    = self.strategy._vwap.get(symbol)
                    if self.strategy._triggered.get(symbol):
                        reason = "already triggered today"
                    elif symbol in self.strategy._open_positions:
                        reason = "position already open"
                    elif not levels:
                        reason = "ORB levels not loaded"
                    elif price <= levels["high"]:
                        reason = f"price ${price:.2f} below ORB high ${levels['high']:.2f}"
                    elif self.strategy.require_vwap_above and vwap and price < vwap:
                        reason = f"price ${price:.2f} below VWAP ${vwap:.2f}"
                    elif ema9 is not None and ema20 is not None and ema9 < ema20:
                        reason = f"EMA9 {ema9:.3f} < EMA20 {ema20:.3f}"
                    elif rsi is not None and rsi > self.strategy.rsi_overbought:
                        reason = f"RSI {rsi:.1f} > {self.strategy.rsi_overbought}"
                    elif vr and vr < self.strategy.min_breakout_rel_vol:
                        reason = f"breakout vol {vr:.1f}x < {self.strategy.min_breakout_rel_vol}x"
                    else:
                        reason = "watching"
                    msg = f"[EVAL] {symbol} @ ${price:.2f} -- {reason}"
                    log_message(msg)
                    self.emit("log", msg)

            # --- First Pullback strategy (intraday, 09:30–11:30 ET) ---
            if self.use_first_pullback and ind and not extended:
                # Respect shared daily trade / concurrent-position limits
                trades_ok   = self.strategy._trades_today < self.strategy.max_trades_per_day
                slots_ok    = len(self.strategy._open_positions) < self.strategy.max_concurrent_positions
                if trades_ok and slots_ok:
                    fp_signal = self.fp_strategy.evaluate(
                        symbol, ind, self.strategy._open_positions
                    )
                    if fp_signal:
                        self.emit("signal", fp_signal)
                        if self.auto_trade:
                            self._execute_trade(fp_signal, extended=False)

            # --- Intraday swing entries (regular hours, stocks oscillating at support) ---
            if self.swing_scan_enabled and not extended and self._swing_results:
                swing_by_sym = {r["symbol"]: r for r in self._swing_results}
                # No swing entries after entry_cutoff_hour or after EOD flatten
                _now_sw = datetime.datetime.now(_ET)
                swing_entry_ok = (
                    _now_sw.hour < self.entry_cutoff_hour
                    and not self._eod_flattened
                )
                if swing_entry_ok and symbol in swing_by_sym and symbol not in self.strategy._open_positions:
                    trades_ok = self.strategy._trades_today < self.strategy.max_trades_per_day
                    slots_ok  = len(self.strategy._open_positions) < self.strategy.max_concurrent_positions
                    if trades_ok and slots_ok:
                        # Cooldown: don’t re-fire a swing entry for the same symbol within 5 minutes
                        if not hasattr(self, "_swing_cooldown"):
                            self._swing_cooldown: Dict[str, float] = {}
                        if time.time() < self._swing_cooldown.get(symbol, 0):
                            continue
                        sw_sig = self.swing_scanner.get_swing_entry_signal(symbol, price)
                        if sw_sig and sw_sig["direction"] == "LONG":
                            # VWAP guard: only enter long if price is at or above VWAP.
                            # Buying below VWAP means the average intraday price is above us
                            # — the stock is in a net declining phase, not a healthy dip.
                            _sw_vwap = self.strategy._vwap.get(symbol)
                            if _sw_vwap and price < _sw_vwap:
                                log_message(
                                    f"[SWING] {symbol} LONG skipped — price ${price:.4f} "
                                    f"below VWAP ${_sw_vwap:.4f}"
                                )
                                continue
                        if sw_sig and sw_sig["direction"] == "LONG":
                            from orb_strategy import TradeSignal as _TS
                            sw_shares, sw_risk, _ = self.strategy.calc_position_size(
                                sw_sig["entry"], sw_sig["stop"]
                            )
                            ts = _TS(
                                symbol=symbol, direction="LONG",
                                entry_price=sw_sig["entry"],
                                stop_price=sw_sig["stop"],
                                target_r2=sw_sig["target"],
                                target_r3=round(sw_sig["entry"] + (sw_sig["entry"] - sw_sig["stop"]) * 3, 4),
                                shares=sw_shares, risk_dollars=sw_risk,
                                orb_high=sw_sig["resistance"], orb_low=sw_sig["support"],
                                vwap=None, rel_vol=0, gap_pct=0,
                                reason=sw_sig["reason"],
                            )
                            msg = (f"[SWING] {symbol} @ ${price:.4f} "
                                   f"support={sw_sig['support']} resist={sw_sig['resistance']}")
                            log_message(msg)
                            self.emit("log", msg)
                            self.emit("signal", ts)
                            # Set 5-minute cooldown so we don't re-enter this symbol every 30 seconds
                            self._swing_cooldown[symbol] = time.time() + 300
                            if self.auto_trade:
                                self._execute_trade(ts)

        # Manage open positions: partial exits, breakeven, trailing upgrades
        self._manage_open_positions(quotes)

    def _run_div_scan(self) -> None:
        """Build _div_symbols from lower-band pre-filter (not gap-up momentum list)."""
        if not self.use_div_strategy:
            return
        results = self.div_strategy.scan_candidates(
            self.client,
            watchlist=list(self._watchlist or []),
            min_price=self.min_price,
            max_price=self.max_price,
            top_n=self.div_scan_max,
        )
        self._div_scan_results = results
        self._last_div_scan    = time.time()
        new_symbols = [r["symbol"] for r in results]
        open_syms   = list(self.strategy._open_positions.keys())
        preserved   = [s for s in open_syms if s not in new_symbols]
        self._div_symbols = new_symbols + preserved
        self.emit("div_scan_results", results)

    def _poll_div_evaluate(self, extended: bool = False) -> None:
        """Evaluate divergence strategy on the div-specific symbol universe."""
        if not self._div_symbols and not self.strategy._open_positions:
            return

        quote_syms = list(dict.fromkeys(
            list(self._div_symbols) + list(self.strategy._open_positions.keys())
        ))
        quotes = self.client.get_quotes(quote_syms)
        for symbol in self._div_symbols:
            data  = quotes.get(symbol, {})
            quote = data.get("quote", {})
            price = float(quote.get("lastPrice", 0) or 0)
            if price <= 0:
                continue
            self._last_prices[symbol] = price

            try:
                self._div_criteria_cache[symbol] = self.div_strategy.check_entry_criteria(
                    symbol, price, self.client, self.strategy._open_positions,
                )
            except Exception as e:
                log_message(f"[DIV] criteria check error for {symbol}: {e}")

            trades_ok = self.strategy._trades_today < self.strategy.max_trades_per_day
            slots_ok  = len(self.strategy._open_positions) < self.strategy.max_concurrent_positions
            if not (trades_ok and slots_ok):
                continue

            div_signal = self.div_strategy.evaluate(
                symbol, {}, self.strategy._open_positions, price,
                data_client=self.client,
            )
            if div_signal:
                self.emit("signal", div_signal)
                if self.auto_trade:
                    self._execute_trade(div_signal, extended=extended)

        self._manage_open_positions(quotes)

    def _poll_extended_hours(self) -> None:
        """Extended hours swing entries (pre-market and after-hours).

        Pre-market entries carry through to regular hours with their original
        stop/target — the regular EOD flatten at 3:55 PM is the safety net.

        After-hours entries are force-closed at 7:55 PM ET (5 min before the
        8:00 PM session end) at current market price via _flatten_all().
        """
        if not self._swing_results or not self._active_symbols:
            return

        approved   = set(self._active_symbols)
        top_swings = [r for r in self._swing_results if r["symbol"] in approved][:5]
        symbols    = [r["symbol"] for r in top_swings]
        quotes     = self.client.get_quotes(symbols)

        for r in top_swings:
            symbol = r["symbol"]
            if symbol in self.strategy._open_positions:
                continue
            data  = quotes.get(symbol, {})
            price = float(data.get("quote", {}).get("lastPrice", 0) or 0)
            if price <= 0:
                continue
            signal = self.swing_scanner.get_swing_entry_signal(symbol, price)
            if signal and signal["direction"] == "LONG":
                self.emit("log",
                    f"[EXT] Swing signal: {symbol} @ {price:.4f} "
                    f"support={signal['support']} resist={signal['resistance']}"
                )
                if self.auto_trade:
                    trades_ok = self.strategy._trades_today < self.strategy.max_trades_per_day
                    slots_ok  = len(self.strategy._open_positions) < self.strategy.max_concurrent_positions
                    if trades_ok and slots_ok:
                        from orb_strategy import TradeSignal
                        shares, risk, _ = self.strategy.calc_position_size(
                            signal["entry"], signal["stop"]
                        )
                        ts = TradeSignal(
                            symbol=symbol, direction="LONG",
                            entry_price=signal["entry"],
                            stop_price=signal["stop"],
                            target_r2=signal["target"],
                            target_r3=round(signal["entry"] + (signal["entry"] - signal["stop"]) * 3, 4),
                            shares=shares, risk_dollars=risk,
                            orb_high=signal["resistance"], orb_low=signal["support"],
                            vwap=None, rel_vol=0, gap_pct=0,
                            reason=signal["reason"],
                        )
                        self._execute_trade(ts, extended=True)

    # ------------------------------------------------------------------
    # RSI strategy scan + evaluate
    # ------------------------------------------------------------------

    def _run_rsi_scan(self) -> None:
        watchlist = list(self._watchlist or [])
        results   = self.rsi_strategy.scan_candidates(watchlist=watchlist, max_workers=8)
        self._last_rsi_scan = time.time()
        self.emit("rsi_scan_results", results)
        ready = [r["symbol"] for r in results if r.get("ready")]
        watching = [r["symbol"] for r in results if r.get("watching") and not r.get("ready")]
        log_message(
            f"[RSI] Scan done: {len(results)} candidates, "
            f"{len(ready)} ready, {len(watching)} watching"
        )
        self.emit("log",
            f"[RSI] {len(ready)} ready: {ready[:5]} | "
            f"{len(watching)} watching: {watching[:5]}"
        )

    def _run_scalp_scan(self) -> None:
        universe = list(self._watchlist or [])
        if hasattr(self.client, "get_movers"):
            for direction in ("up", "down"):
                try: universe.extend(self.client.get_movers(direction=direction))
                except Exception: pass
        seen: set = set()
        symbols = [s for s in universe if not (s in seen or seen.add(s))]  # type: ignore
        results = self.scalp_screener.scan(symbols, max_workers=8)
        self._last_scalp_scan = time.time()
        self.emit("scalp_scan_results", results)

    def _poll_rsi_evaluate(self) -> None:
        candidates = self.rsi_strategy.get_candidates()
        if not candidates:
            return
        # Refresh daily RSI for already-open RSI positions
        rsi_open = [
            sym for sym, pos in self.strategy._open_positions.items()
            if "RSI MeanRev" in (getattr(pos, "entry_reason", "") or "")
        ]
        eval_syms = list({c["symbol"] for c in candidates if c.get("ready")} | set(rsi_open))
        if not eval_syms:
            return
        quotes = self.client.get_quotes(eval_syms)
        for cand in candidates:
            if not cand.get("ready"):
                continue
            symbol = cand["symbol"]
            if symbol in self.strategy._open_positions:
                continue
            if self.strategy._trades_today >= self.strategy.max_trades_per_day:
                break
            if len(self.strategy._open_positions) >= self.strategy.max_concurrent_positions:
                break
            price = float((quotes.get(symbol, {}).get("quote", {}).get("lastPrice") or 0))
            if price <= 0:
                continue
            signal = self.rsi_strategy.evaluate(
                symbol, candidate=cand, current_price=price
            )
            if signal:
                self.emit("signal", signal)
                if self.auto_trade:
                    self._execute_trade(signal)
        # RSI exit management for open RSI positions
        for symbol in rsi_open:
            pos = self.strategy._open_positions.get(symbol)
            if not pos or pos.status != "open":
                continue
            price = float((quotes.get(symbol, {}).get("quote", {}).get("lastPrice") or 0))
            if price <= 0:
                continue
            daily_rsi = self.rsi_strategy.get_daily_rsi(symbol)
            action = self.rsi_strategy.manage_position(symbol, price, daily_rsi)
            if not action:
                continue
            if action["action"] == "close":
                log_message(f"[RSI] Closing {symbol} — {action['reason']}")
                self.emit("log", f"[RSI] Closing {symbol} — {action['reason']}")
                try:
                    self.manual_sell(symbol)
                    self.rsi_strategy.record_close(symbol)
                except Exception as _e:
                    log_message(f"[RSI] close error {symbol}: {_e}")
            elif action["action"] == "partial":
                log_message(f"[RSI] Partial exit {symbol} — {action['reason']}")
                self.emit("log", f"[RSI] Partial exit {symbol} — {action['reason']}")
                qty = max(1, pos.shares // 2)
                try:
                    self._partial_exit(symbol, pos, price)
                    self.rsi_strategy.record_partial(symbol)
                except Exception as _e:
                    log_message(f"[RSI] partial error {symbol}: {_e}")

    def _run_swing_scan(self) -> None:
        """Full market swing scan — finds top 10 oscillating stocks across all major indices."""
        extra = list(self._active_symbols) + list(self._watchlist)
        log_message("[SWING] Starting market-wide swing scan...")
        self.emit("log", "[SWING] Starting market-wide swing scan...")
        results = self.swing_scanner.scan_market(extra_symbols=extra, top_n=10)
        self._swing_results = results
        self._last_swing_scan = time.time()
        self.emit("swing_results", results)
        if results:
            top = [f"{r['symbol']}({r['swing_count']}sw)" for r in results[:5]]
            log_message(f"[SWING] Top candidates: {', '.join(top)}")
            self.emit("log", f"[SWING] Top candidates: {', '.join(top)}")

    # ------------------------------------------------------------------
    # Trailing stop upgrade logic
    # ------------------------------------------------------------------

    def _manage_open_positions(self, quotes: Dict) -> None:
        """
        Per-poll management of every open position:
          1. Partial exit at +partial_exit_r R → sell partial_exit_pct %, move stop to breakeven
          2. Trailing-stop upgrade on R-multiple gain + volume spike
        """
        for symbol, pos in list(self.strategy._open_positions.items()):
            if pos.status != "open":
                continue  # skip PENDING entries — not filled yet
            data  = quotes.get(symbol, {})
            quote = data.get("quote", {})
            price = float(quote.get("lastPrice", 0) or 0)
            if price <= 0:
                continue

            # Optional momentum exit: close on MACD bearish cross
            # Skip for swing range trades — MACD crosses too frequently on oscillating stocks
            is_swing     = "Swing"     in (getattr(pos, "entry_reason", "") or "")
            is_div_swing = "div_swing" in (getattr(pos, "entry_reason", "") or "")
            is_rsi       = "RSI MeanRev" in (getattr(pos, "entry_reason", "") or "")

            # RSI stop-hit: close immediately; RSI-level exits handled in _poll_rsi_evaluate
            if is_rsi:
                if price <= pos.stop_price:
                    try:
                        self.manual_sell(symbol)
                        self.rsi_strategy.record_close(symbol)
                    except Exception as _e:
                        log_message(f"[RSI] stop-hit close error {symbol}: {_e}")
                continue

            # Divergence swing: enforce max hold days then force-close
            if is_div_swing:
                days_held = (time.time() - (pos.entry_time or time.time())) / 86400
                max_d     = getattr(self, "div_max_hold_days", 5)
                if days_held >= max_d:
                    msg = (f"[DIV] {symbol} max hold {max_d}d reached "
                           f"({days_held:.1f}d) — force-closing.")
                    log_message(msg)
                    self.emit("log", msg)
                    try:
                        self.manual_sell(symbol)
                    except Exception as _e:
                        log_message(f"[DIV] force-close error {symbol}: {_e}")
                    continue

            if self.use_macd_exit and not is_swing and not is_div_swing:
                mh = self.strategy.get_macd_hist(symbol)
                if mh is not None and mh < 0:
                    self.emit("log", f"[EXIT] {symbol} MACD bearish (hist {mh}) — closing position.")
                    try:
                        self.manual_sell(symbol)
                    except Exception as e:
                        log_message(f"[ENGINE] MACD exit error for {symbol}: {e}")
                    continue

            # Track peak price
            if pos.peak_price == 0.0:
                pos.peak_price = pos.entry_price
            if price > pos.peak_price:
                pos.peak_price = price

            risk_dist = abs(pos.entry_price - pos.stop_price)
            if risk_dist <= 0:
                continue
            r_multiple = (price - pos.entry_price) / risk_dist

            # --- 1. Partial exit + breakeven ---
            if (self.partial_exit_enabled and not pos.partial_exit_done
                    and r_multiple >= self.partial_exit_r and pos.shares > 1):
                self._partial_exit(symbol, pos, price)
                continue  # let next poll handle trailing on the runner

            # --- 2. Trailing-stop upgrade ---
            if pos.trailing_stop_active:
                continue
            # Divergence swing trades target the mid/upper band over days;
            # a premature trail upgrade would kill the trade on a normal pullback.
            if "div_swing" in (getattr(pos, "entry_reason", "") or ""):
                continue
            if r_multiple < self.trail_trigger_r:
                pos.last_volume_snapshot = int(quote.get("totalVolume", 0) or 0)
                continue

            total_vol = int(quote.get("totalVolume", 0) or 0)
            vol_delta = total_vol - pos.last_volume_snapshot if pos.last_volume_snapshot > 0 else 0
            avg_per_min    = self._vol_cache.get(symbol, 0)
            avg_per_interval = avg_per_min * (self.poll_interval / 60.0)
            volume_spiked  = (
                vol_delta > 0 and avg_per_interval > 0 and
                vol_delta >= avg_per_interval * self.trail_vol_mult
            )
            pos.last_volume_snapshot = total_vol

            if volume_spiked:
                spike_x = round(vol_delta / avg_per_interval, 1) if avg_per_interval > 0 else 0
                self.emit("log",
                    f"[TRAIL] {symbol} at {r_multiple:.1f}R \u2014 vol spike {spike_x}x avg "
                    f"\u2192 upgrading to {self.trail_pct}% trailing stop"
                )
                self._upgrade_to_trailing_stop(symbol, pos)

    def _partial_exit(self, symbol: str, pos, price: float) -> None:
        """Sell a slice at +R, rebook the OCO on the remaining shares at breakeven."""
        qty = max(1, int(pos.shares * (self.partial_exit_pct / 100.0)))
        if qty >= pos.shares:
            qty = pos.shares - 1  # always keep a runner
        if qty <= 0:
            return

        if self.dry_run or self.paper_mode:
            self.paper.sell_equity(symbol, qty, price, getattr(pos, "entry_reason", "") or "")
            slice_pnl = self.strategy.record_partial_exit(symbol, price, qty)
            self._save_positions()
            self.emit("log", f"[PAPER] Partial exit {qty} {symbol} @ {price:.4f} (+${slice_pnl}) — stop→breakeven cash ${self.paper.cash:.2f}")
            self.emit("trade_partial", {"symbol": symbol, "qty": qty, "price": price, "pnl": slice_pnl})
            return

        # Live: cancel existing bracket, sell the slice, re-bracket the runner at breakeven
        if pos.oco_order_id and pos.oco_order_id != "DRY_RUN":
            if not self.client.cancel_order(pos.oco_order_id):
                self.emit("log", f"WARNING: could not cancel OCO for {symbol} — skipping partial exit")
                return
        sell_id = self.client.place_market_order(symbol, qty, "SELL")
        if not sell_id:
            self.emit("log", f"ERROR: partial sell failed for {symbol} — re-bracketing original")
            self._rebracket(pos)
            return
        slice_pnl = self.strategy.record_partial_exit(symbol, price, qty)
        # Re-bracket the remaining runner with stop at breakeven
        self._rebracket(pos)
        self._save_positions()
        self.emit("log", f"PARTIAL EXIT {qty} {symbol} @ {price:.4f} (+${slice_pnl}) — stop→breakeven, {pos.shares}sh runner")
        self.emit("trade_partial", {"symbol": symbol, "qty": qty, "price": price, "pnl": slice_pnl})

    def _rebracket(self, pos) -> None:
        """Place a fresh OCO bracket for a position's current shares/stop/target.

        After a partial scale-out, the surviving runner aims for the extended
        runner_target_r (default 3R) instead of the original 2R/3R target — this
        lets winners run further, which is what pays for the small frequent losers.
        """
        now    = datetime.datetime.now(_ET)
        is_ext = self._is_extended_hours_now(now)
        if pos.partial_exit_done and self.runner_target_r and self.runner_target_r > 0:
            # Recover per-share risk from the 3R target (consistent for ORB + FP),
            # then project the configured runner target off the entry.
            r_per_share = (pos.target_r3 - pos.entry_price) / 3.0
            if r_per_share > 0:
                target = round(pos.entry_price + r_per_share * self.runner_target_r, 4)
            else:
                target = pos.target_r3
        else:
            target = pos.target_r2 if self.strategy.rr_target < 3.0 else pos.target_r3
        oco_id = self.client.place_oco_order(pos.symbol, pos.shares, pos.stop_price, target, extended_hours=is_ext)
        if oco_id:
            pos.oco_order_id = oco_id
        else:
            self.emit("log", f"WARNING: re-bracket OCO failed for {pos.symbol} — monitor manually!")

    def _upgrade_to_trailing_stop(self, symbol: str, pos) -> None:
        """Cancel the OCO bracket and replace with a trailing stop sell order."""
        now    = datetime.datetime.now(_ET)
        is_ext = self._is_extended_hours_now(now)

        if self.dry_run or self.paper_mode:
            pos.trailing_stop_active = True
            self.emit("log", f"[PAPER] Trailing stop armed on {symbol} at {self.trail_pct}% (paper — no broker order)")
            self.emit("trade_upgraded", {"symbol": symbol, "trail_pct": self.trail_pct})
            return

        # Cancel existing OCO bracket
        if pos.oco_order_id and pos.oco_order_id != "DRY_RUN":
            cancelled = self.client.cancel_order(pos.oco_order_id)
            if not cancelled:
                self.emit("log", f"WARNING: Could not cancel OCO for {symbol} \u2014 skipping trail upgrade")
                return
            self.emit("log", f"OCO cancelled for {symbol}")

        # Place trailing stop
        trail_id = self.client.place_trailing_stop_order(
            symbol, pos.shares, self.trail_pct, extended_hours=is_ext
        )
        if trail_id:
            pos.oco_order_id        = trail_id
            pos.trailing_stop_active = True
            self._save_positions()
            self.emit("log", f"UPGRADED {symbol} \u2192 {self.trail_pct}% trailing stop (ID: {trail_id})")
            self.emit("trade_upgraded", {"symbol": symbol, "trail_pct": self.trail_pct})
        else:
            # Trail failed AFTER cancelling the OCO — position is now naked.
            # Re-place the original OCO bracket so the position is never unprotected.
            self.emit("log", f"ERROR: Trailing stop placement failed for {symbol} \u2014 restoring OCO bracket.")
            self._rebracket(pos)
            self._save_positions()

    # ------------------------------------------------------------------
    # Broker reconciliation — detect fills the bot didn't initiate
    # ------------------------------------------------------------------

    def _reconcile_positions(self) -> None:
        """
        Compare in-memory open positions against the broker's actual positions.
        If the broker no longer holds a symbol the bot thinks is open, the exit
        (stop/target/trail) filled at the broker → close it in our books.
        Skipped in dry-run.
        """
        if self.dry_run:
            return
        try:
            broker_positions = self.client.get_positions()
        except Exception as e:
            log_message(f"[RECON] get_positions error: {e}")
            return
        broker_qty = {p["symbol"]: float(p.get("quantity", 0) or 0) for p in broker_positions}

        # --- Reverse check: broker has positions the bot doesn't know about ---
        # This catches fills that were missed during pending-fill detection
        # (API error, auth expiry, etc.) that left untracked broker positions.
        tracked = set(self.strategy._open_positions.keys())
        for broker_sym, broker_qty_val in broker_qty.items():
            if broker_qty_val < 1:
                continue
            if broker_sym in tracked:
                continue   # bot knows about this one
            # Untracked broker position — critical alert + emergency protection
            alert = (
                f"[CRITICAL] UNTRACKED POSITION: broker holds {broker_qty_val:.0f} sh {broker_sym} "
                f"with NO stop or target in bot. Attempting emergency trailing stop."
            )
            log_message(alert)
            self.emit("log", alert)
            # Attempt a 3% trailing stop as emergency protection
            try:
                is_ext = self._is_extended_hours_now(datetime.datetime.now(_ET))
                trail_id = self.client.place_trailing_stop_order(
                    broker_sym, int(broker_qty_val), 3.0, extended_hours=is_ext
                )
                if trail_id:
                    self.emit("log",
                        f"[CRITICAL] Emergency 3% trailing stop placed for "
                        f"{broker_sym} (ID: {trail_id}). Review manually."
                    )
                else:
                    self.emit("log",
                        f"[CRITICAL] Emergency trail FAILED for {broker_sym}. "
                        f"CLOSE {broker_qty_val:.0f} SHARES MANUALLY NOW."
                    )
            except Exception as e_trail:
                self.emit("log",
                    f"[CRITICAL] Could not protect {broker_sym}: {e_trail}. "
                    f"CLOSE MANUALLY."
                )

        for symbol, pos in list(self.strategy._open_positions.items()):
            if pos.status != "open":
                continue  # PENDING entries are handled by _check_pending_fills
            held = broker_qty.get(symbol, 0.0)
            if held >= 1:
                continue  # still held (full or partial) — leave for next cycle
            # Broker holds 0 shares → exit order filled externally. Determine price/reason.
            exit_price = pos.entry_price
            reason = "broker_exit"
            try:
                q = self.client.get_quote(symbol)
                if q:
                    exit_price = float(q.get("quote", {}).get("lastPrice", pos.entry_price) or pos.entry_price)
            except Exception:
                pass
            if exit_price <= pos.stop_price * 1.001:
                reason = "stop"
            elif exit_price >= pos.target_r2 * 0.999:
                reason = "target"
            closed = self.strategy.record_exit(symbol, exit_price, reason)
            self._save_positions()
            if closed:
                self.data_handler.append_trade_log({
                    "symbol": symbol, "exit_price": exit_price,
                    "pnl": closed.pnl, "reason": f"reconciled_{reason}",
                })
                self.emit("trade_closed", {"symbol": symbol, "pnl": closed.pnl})
                self.emit("log", f"[RECON] {symbol} closed at broker ({reason}) @ {exit_price:.4f} | PnL ${closed.pnl:.2f}")
                if (self.scalp_strategy.enabled and
                        symbol == self.scalp_strategy.symbol and
                        "SCALP" in (getattr(closed, "entry_reason", "") or "")):
                    self.scalp_strategy.mark_trade_closed(closed.pnl)

    # ------------------------------------------------------------------
    # Daily loss kill switch + end-of-day flatten
    # ------------------------------------------------------------------

    def _trip_kill_switch(self, day_pnl: float, max_loss: float) -> None:
        if self._kill_switch_tripped:
            return
        self._kill_switch_tripped = True
        self.emit("log",
            f"[RISK] DAILY LOSS LIMIT HIT — PnL ${day_pnl:.2f} <= ${max_loss:.2f}. "
            f"Flattening all positions and halting new entries."
        )
        self.emit("kill_switch", {"day_pnl": day_pnl, "limit": max_loss})
        self._flatten_all("kill_switch")

    def _flatten_all(self, reason: str) -> None:
        """Market-sell every held position and cancel any unfilled entries."""
        for symbol, pos in list(self.strategy._open_positions.items()):
            try:
                if pos.status == "pending":
                    # Unfilled entry — cancel the limit order and drop it.
                    if not self.dry_run and pos.entry_order_id and pos.entry_order_id != "DRY_RUN":
                        self.client.cancel_order(pos.entry_order_id)
                    self.strategy.drop_pending(symbol)
                else:
                    self.manual_sell(symbol)
            except Exception as e:
                log_message(f"[ENGINE] flatten error for {symbol}: {e}")
        self._save_positions()
        self._flatten_options(reason)
        self.emit("log", f"[ENGINE] All positions flattened ({reason}).")

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def _execute_trade(self, signal: TradeSignal, extended: bool = False) -> None:
        now = self._get_now()
        if self._eod_flattened or not in_entry_window(now):
            log_message(
                f"[SCHED] skip {signal.symbol} — entries only weekday 9:30 AM–3:58 PM ET "
                f"(now {now.strftime('%a %H:%M')} ET)"
            )
            return
        symbol  = signal.symbol
        shares  = signal.shares
        entry   = signal.entry_price
        stop    = signal.stop_price
        target  = signal.target_r2 if self.strategy.rr_target < 3.0 else signal.target_r3

        is_div = "div_swing" in (getattr(signal, "reason", "") or "")

        # --- News catalyst gate: block ALL new LONG entries on bearish headlines ---
        # No strategy exemption — div, ORB, FP, swing, scalp, and manual all checked.
        _news_sent = None
        if (
            signal.direction == "LONG"
            and getattr(self, "news_sentiment_enabled", False)
        ):
            ns = getattr(self, "_news_sentiment", None)
            if ns:
                _news_sent = ns.get_sentiment(symbol)
                if _news_sent.get("blocked"):
                    headline = (_news_sent.get("headline") or "")[:80]
                    extra = f" — {headline}" if headline else ""
                    msg = (
                        f"[NEWS] {symbol} LONG blocked — bearish catalyst "
                        f"(score={_news_sent.get('score', 0):+.0f}{extra})"
                    )
                    log_message(msg)
                    self.emit("log", msg)
                    return

        # --- Market regime gate: only take LONG entries when SPY is above VWAP ---
        # Div/mean-reversion entries are exempt — they target pullbacks, not momentum.
        if signal.direction == "LONG" and not is_div and not self._market_bullish():
            msg = (f"[FILTER] {symbol} LONG blocked — {self.market_filter_symbol} "
                   f"below VWAP (bearish market day).")
            log_message(msg)
            self.emit("log", msg)
            return

        # --- 5-min chart entry rules: SMA10 + volume + dip-only (no chasing) ---
        # Div swing entries buy mean-reversion at the lower band — exempt.
        if (
            signal.direction == "LONG"
            and getattr(self, "use_5min_entry_rules", False)
            and not is_div
        ):
            check_px = float(self._last_prices.get(symbol, entry) or entry)
            try:
                chk = self.scanner.get_5min_long_entry_check(
                    symbol,
                    check_px,
                    sma_period=self.htf_sma_period,
                    dip_sma_touch_pct=self.dip_sma_touch_pct,
                    require_vol=self.require_5min_vol_above_avg,
                    require_dip=self.require_dip_entry,
                )
            except Exception as e_5m:
                chk = {"ok": False, "reason": f"5m check error: {e_5m}"}
            if not chk.get("ok"):
                msg = f"[FILTER] {symbol} LONG blocked — {chk.get('reason', '5m rules')}"
                log_message(msg)
                self.emit("log", msg)
                return

        # --- Intraday retrace gate: skip LONG if stock gave back too much of its run ---
        # Div entries intentionally buy near the lower band after pullbacks — exempt.
        if signal.direction == "LONG" and self.intraday_max_retrace_pct > 0 and not is_div:
            check_px = self._last_prices.get(symbol, entry)
            try:
                retrace = self.scanner.get_intraday_retrace_pct(symbol, check_px)
            except Exception:
                retrace = None
            if (retrace is not None
                    and isinstance(retrace, (int, float))
                    and retrace > self.intraday_max_retrace_pct):
                msg = (
                    f"[FILTER] {symbol} LONG blocked — retraced {retrace:.1f}% of "
                    f"intraday gains from open (max {self.intraday_max_retrace_pct:.0f}%)"
                )
                log_message(msg)
                self.emit("log", msg)
                return

        # --- Time-of-day position size decay ---
        _decay_shares = signal.shares
        if self.time_decay_factor < 1.0:
            try:
                _tnow = _now_et()   # use utils (respects sim clock, avoids mock issues)
                _now_hm = _tnow.hour * 100 + _tnow.minute
            except Exception:
                _now_hm = 0         # safe default — no decay on error
            if _now_hm >= self.time_decay_start_hhmm:
                _decay_shares = max(1, int(signal.shares * self.time_decay_factor))
                if _decay_shares != signal.shares:
                    log_message(
                        f"[DECAY] {symbol} size {signal.shares}→{_decay_shares}sh "
                        f"({self.time_decay_factor:.0%} after {self.time_decay_start_hhmm//100}"
                        f":{self.time_decay_start_hhmm%100:02d})"
                    )
        shares = _decay_shares

        # --- Bullish news size boost (optional) ---
        if (
            signal.direction == "LONG"
            and _news_sent
            and not _news_sent.get("blocked")
        ):
            mult = float(_news_sent.get("size_mult", 1.0) or 1.0)
            if mult > 1.0:
                boosted = max(1, int(shares * mult))
                if boosted != shares:
                    log_message(
                        f"[NEWS] {symbol} size boost {shares}→{boosted}sh "
                        f"(score={_news_sent.get('score', 0):+.0f})"
                    )
                    shares = boosted

        # --- Risk gate 1: daily loss kill switch ---
        if self._kill_switch_tripped:
            msg = f"[RISK] Kill switch active — entry for {symbol} blocked."
            log_message(msg)
            self.emit("log", msg)
            return
        day_pnl = self.strategy.get_day_pnl()
        max_loss = -abs(self.account_size * (self.max_daily_loss_pct / 100.0))
        if day_pnl <= max_loss:
            self._trip_kill_switch(day_pnl, max_loss)
            return

        # --- Risk gate 2: buying-power / capital allocation check ---
        est_cost = _decay_shares * entry
        committed = sum(p.shares * p.entry_price for p in self.strategy.get_active_positions())
        cash_avail = float(self.paper.cash)
        if committed + est_cost > cash_avail + 1e-9:
            msg = (f"[PAPER] {symbol} entry skipped — needs ${est_cost:.0f}, only "
                   f"${max(0.0, cash_avail):.0f} paper cash.")
            log_message(msg)
            self.emit("log", msg)
            return

        # Paper fill at live ask. Never POST to Schwab.
        fill = entry
        try:
            q = self.client.get_quote(symbol)
            qq = (q or {}).get("quote") or {}
            ask = float(qq.get("askPrice") or 0)
            last = float(qq.get("lastPrice") or 0)
            ref = ask if ask > 0 else last
            if ref > 0:
                fill = round(ref * (1 + self.limit_entry_buffer), 4)
        except Exception:
            pass
        signal.entry_price = fill
        oid = self.paper.buy_equity(symbol, shares, fill, getattr(signal, "reason", "") or "orb")
        if not oid:
            msg = f"[PAPER] {symbol} skipped — not enough paper cash (${self.paper.cash:.2f})"
            log_message(msg)
            self.emit("log", msg)
            return
        msg = (f"[PAPER] FILL BUY {shares} {symbol} @ {fill:.4f} "
               f"| stop={stop:.4f} target={target:.4f} cash ${self.paper.cash:.2f}")
        log_message(msg)
        self.emit("log", msg)
        pos = self.strategy.record_entry(signal, oid, status="open")
        pos.oco_order_id = oid
        self._save_positions()
        self._emit_status_update()
        self.data_handler.append_trade_log({
            "symbol": symbol, "shares": shares, "entry": fill,
            "stop": stop, "target": target, "dry_run": True, "paper": True,
            "order_id": oid,
            "session": "extended" if extended else "regular",
            "source": "paper",
            "strategy_id": getattr(signal, "reason", "") or "orb",
        })
        return

    # ------------------------------------------------------------------
    # Pending-entry fill detection (PENDING → HELD)
    # ------------------------------------------------------------------

    def _place_entry_bracket(self, pos) -> None:
        """Place the protective OCO bracket for a freshly-filled position.

        Retries up to 3 times. If all attempts fail, issues an emergency
        market-sell to close the position rather than leaving it naked.
        """
        now    = datetime.datetime.now(_ET)
        is_ext = self._is_extended_hours_now(now)
        target = pos.target_r2 if self.strategy.rr_target < 3.0 else pos.target_r3

        # Retry loop — transient API errors are common right after a fill
        oco_id = None
        for attempt in range(1, 4):   # up to 3 attempts
            oco_id = self.client.place_oco_order(
                pos.symbol, pos.shares, pos.stop_price, target, extended_hours=is_ext
            )
            if oco_id:
                break
            log_message(f"[BRACKET] OCO attempt {attempt}/3 failed for {pos.symbol}")
            time.sleep(2)   # short pause before retry

        if oco_id:
            pos.oco_order_id = oco_id
            self.emit("log",
                f"Bracket placed for {pos.symbol}: "
                f"stop={pos.stop_price:.4f} target={target:.4f} (OCO {oco_id})"
            )
        else:
            # All OCO attempts failed — position is unprotected.
            # Emergency exit: market-sell immediately rather than leave it naked.
            alert = (
                f"[CRITICAL] OCO bracket FAILED after 3 attempts for {pos.symbol} "
                f"({pos.shares} sh @ {pos.entry_price}). "
                f"Issuing emergency MARKET SELL to prevent naked exposure."
            )
            log_message(alert)
            self.emit("log", alert)
            if not self.dry_run:
                sell_id = self.client.place_market_order(pos.symbol, pos.shares, "SELL", extended_hours=is_ext)
                if sell_id:
                    # Record exit at approximate current price
                    try:
                        q = self.client.get_quote(pos.symbol)
                        ep = float((q or {}).get("quote", {}).get("lastPrice", pos.entry_price))
                    except Exception:
                        ep = pos.entry_price
                    self.strategy.record_exit(pos.symbol, ep, "emergency_sell")
                    self._save_positions()
                    self.emit("log",
                        f"[CRITICAL] Emergency sell executed for {pos.symbol} @ ~{ep:.4f}"
                    )
                else:
                    self.emit("log",
                        f"[CRITICAL] Emergency sell ALSO failed for {pos.symbol}. "
                        f"MANUAL ACTION REQUIRED — close {pos.shares} shares immediately."
                    )

    def _check_pending_fills(self) -> None:
        """Promote PENDING entries to HELD when the broker confirms the fill,
        and drop entries whose limit order never filled within the timeout."""
        if self.dry_run:
            return
        pending = self.strategy.get_pending_positions()
        if not pending:
            return
        try:
            broker_positions = self.client.get_positions()
        except Exception as e:
            log_message(f"[FILL] get_positions error: {e}")
            return
        broker = {p["symbol"]: p for p in broker_positions}

        for pos in pending:
            symbol = pos.symbol
            bp = broker.get(symbol)
            held_qty = float(bp.get("quantity", 0) or 0) if bp else 0.0
            if held_qty >= 1:
                fill_price = float(bp.get("avg_price", pos.entry_price) or pos.entry_price)
                self.strategy.mark_filled(symbol, fill_price)
                self._place_entry_bracket(pos)
                self._save_positions()
                self._emit_status_update()
                self.emit("trade_placed", {
                    "symbol": symbol, "shares": pos.shares, "entry": pos.entry_price,
                    "stop": pos.stop_price, "target": pos.target_r2,
                    "order_id": pos.entry_order_id, "oco_id": pos.oco_order_id,
                })
                self.data_handler.append_trade_log({
                    "symbol": symbol, "shares": pos.shares, "entry": pos.entry_price,
                    "stop": pos.stop_price, "target": pos.target_r2,
                    "status": "filled", "order_id": pos.entry_order_id,
                })
            elif time.time() - pos.entry_time > self._pending_timeout_sec:
                # Safety check before dropping: confirm broker doesn't hold shares.
                # If the fill happened but was missed by the API (network blip, auth
                # expiry at the exact poll moment), dropping the pending would create
                # a naked broker position with no stop or target.
                try:
                    fresh_pos = self.client.get_positions()
                    fresh_broker = {p["symbol"]: p for p in fresh_pos}
                    fresh_bp  = fresh_broker.get(symbol)
                    fresh_qty = float(fresh_bp.get("quantity", 0) or 0) if fresh_bp else 0.0
                except Exception:
                    fresh_qty = 0.0

                if fresh_qty >= 1:
                    # Broker actually filled it — treat as a late fill detection.
                    fill_price = float(fresh_bp.get("avg_price", pos.entry_price) or pos.entry_price)
                    self.strategy.mark_filled(symbol, fill_price)
                    self._place_entry_bracket(pos)
                    self._save_positions()
                    self._emit_status_update()
                    log_message(
                        f"[FILL] {symbol} late-fill detected at timeout — "
                        f"filled @ {fill_price} — bracket placed."
                    )
                    self.emit("log",
                        f"[FILL] {symbol} late fill @ {fill_price:.4f} — bracket placed."
                    )
                else:
                    # Confirmed unfilled — cancel and free the slot.
                    if pos.entry_order_id and pos.entry_order_id not in ("DRY_RUN", None):
                        try:
                            self.client.cancel_order(pos.entry_order_id)
                        except Exception as e:
                            log_message(f"[FILL] cancel error for {symbol}: {e}")
                    self.strategy.drop_pending(symbol)
                    self._save_positions()
                    self._emit_status_update()
                    self.emit("log",
                        f"[FILL] {symbol} entry not filled within "
                        f"{int(self._pending_timeout_sec)}s — confirmed unfilled, cancelled."
                    )

    def manual_buy(self, symbol: str) -> None:
        """Force a market buy from the UI (overrides auto signal requirement)."""
        quote = self.client.get_quote(symbol)
        if not quote:
            self.emit("log", f"Cannot get quote for {symbol}")
            return
        price = float(quote.get("quote", {}).get("lastPrice", 0) or 0)
        if price <= 0:
            self.emit("log", f"Invalid price for {symbol}")
            return
        levels = self.strategy._orb_levels.get(symbol)
        if not levels:
            self.emit("log", f"No ORB levels for {symbol}. Set levels first.")
            return
        # Build a manual signal
        stop   = levels["low"]
        shares, risk, _ = self.strategy.calc_position_size(price, stop)
        target = round(price + (price - stop) * self.strategy.rr_target, 4)
        from orb_strategy import TradeSignal
        signal = TradeSignal(
            symbol=symbol, direction="LONG",
            entry_price=price, stop_price=stop,
            target_r2=target, target_r3=round(price + (price - stop) * 3, 4),
            shares=shares, risk_dollars=risk,
            orb_high=levels["high"], orb_low=levels["low"],
            vwap=self.strategy._vwap.get(symbol), rel_vol=0, gap_pct=0,
            reason="Manual buy",
        )
        _now = datetime.datetime.now(_ET)
        self._execute_trade(signal, extended=self._is_extended_hours_now(_now))

    def manual_sell(self, symbol: str) -> None:
        """Market sell all shares of symbol and close position."""
        pos = self.strategy._open_positions.get(symbol)
        if not pos:
            self.emit("log", f"No open position for {symbol}")
            return
        quote = self.client.get_quote(symbol)
        exit_price = pos.entry_price
        if quote:
            qq = quote.get("quote") or {}
            bid = float(qq.get("bidPrice") or 0)
            last = float(qq.get("lastPrice", pos.entry_price) or pos.entry_price)
            exit_price = bid if bid > 0 else last
        self.paper.sell_equity(symbol, pos.shares, exit_price, getattr(pos, "entry_reason", "") or "")
        closed = self.strategy.record_exit(symbol, exit_price, "manual")
        self._save_positions()
        if closed:
            self.data_handler.append_trade_log({
                "symbol": symbol, "exit_price": exit_price,
                "pnl": closed.pnl, "reason": "manual_sell",
                "source": "paper", "paper": True,
            })
            self.emit("trade_closed", {"symbol": symbol, "pnl": closed.pnl})
            self.emit("log", f"[PAPER] SELL {symbol} @ {exit_price:.4f} | PnL: ${closed.pnl:.2f} cash ${self.paper.cash:.2f}")
            # Update scalp session P&L when a scalp trade closes
            if (self.scalp_strategy.enabled and
                    symbol == self.scalp_strategy.symbol and
                    "SCALP" in (getattr(closed, "entry_reason", "") or "")):
                self.scalp_strategy.update_session_pnl(closed.pnl)

    # ------------------------------------------------------------------
    # Status + persistence
    # ------------------------------------------------------------------

    def _emit_status_update(self) -> None:
        snap = self.paper.snapshot(getattr(self, "_last_prices", {}) or {})
        self.emit("balance", snap)
        self.emit("paper", snap)
        # Enrich active positions with current market price so the UI can
        # show live price and compute unrealized P&L without needing a
        # separate API call from the frontend.
        def _with_price(pos_list):
            out = []
            for p in pos_list:
                d = dict(vars(p))
                d['current_price'] = self._last_prices.get(p.symbol, p.entry_price)
                out.append(d)
            return out
        self.emit("positions", {
            "pending": _with_price(self.strategy.get_pending_positions()),
            "held":    _with_price(self.strategy.get_open_positions()),
            "open":    _with_price(self.strategy.get_open_positions()),
            "closed":  [vars(p) for p in self.strategy.get_today_closed_positions()],
        })
        self.emit("day_pnl", float(snap.get("day_pnl") or self.strategy.get_day_pnl()))

    def _restore_positions(self) -> None:
        """Reload open positions persisted to disk — called once on startup after a restart."""
        saved = self.data_handler.load_positions()
        open_list = saved.get("open", [])
        if not open_list:
            return
        from orb_strategy import OpenPosition
        count = 0
        for d in open_list:
            try:
                pos = OpenPosition(
                    symbol=d["symbol"],
                    direction=d["direction"],
                    entry_price=float(d["entry_price"]),
                    shares=int(d["shares"]),
                    stop_price=float(d["stop_price"]),
                    target_r2=float(d["target_r2"]),
                    target_r3=float(d["target_r3"]),
                    risk_dollars=float(d["risk_dollars"]),
                    entry_order_id=d.get("entry_order_id"),
                    oco_order_id=d.get("oco_order_id"),
                    status=d.get("status", "open"),
                    trailing_stop_active=bool(d.get("trailing_stop_active", False)),
                    peak_price=float(d.get("peak_price", 0.0)),
                    last_volume_snapshot=int(d.get("last_volume_snapshot", 0)),
                    # Partial-exit / breakeven state — must survive restarts or a
                    # scaled position would lose its locked-in profit and could
                    # scale out a second time at the next +R.
                    original_shares=int(d.get("original_shares", 0) or int(d["shares"])),
                    partial_exit_done=bool(d.get("partial_exit_done", False)),
                    breakeven_active=bool(d.get("breakeven_active", False)),
                    realized_pnl=float(d.get("realized_pnl", 0.0)),
                    entry_reason=d.get("entry_reason", ""),
                    gap_pct=float(d.get("gap_pct", 0.0) or 0.0),
                )
                self.strategy._open_positions[pos.symbol] = pos
                self.strategy._triggered[pos.symbol] = True
                count += 1
            except Exception as e:
                log_message(f"[ENGINE] Could not restore position {d.get('symbol','?')}: {e}")
        if count:
            log_message(f"[ENGINE] Restored {count} open position(s) from disk.")


    def _restore_closed_positions(self, cap: int = 2000) -> None:
        """Reload the persisted closed-trade history into the strategy so it is
        not wiped by the next _save_positions(), and so analytics has the full
        record across restarts. Capped to the most recent `cap` trades."""
        saved = self.data_handler.load_positions()
        closed_list = saved.get("closed", [])
        if not closed_list:
            return
        from orb_strategy import OpenPosition
        restored = 0
        for d in closed_list[-cap:]:
            try:
                pos = OpenPosition(
                    symbol=d["symbol"],
                    direction=d.get("direction", "LONG"),
                    entry_price=float(d.get("entry_price", 0.0)),
                    shares=int(d.get("shares", 0)),
                    stop_price=float(d.get("stop_price", 0.0)),
                    target_r2=float(d.get("target_r2", 0.0)),
                    target_r3=float(d.get("target_r3", 0.0)),
                    risk_dollars=float(d.get("risk_dollars", 0.0)),
                    entry_time=float(d.get("entry_time", 0.0) or 0.0),
                    entry_order_id=d.get("entry_order_id"),
                    oco_order_id=d.get("oco_order_id"),
                    status=d.get("status", "closed"),
                    exit_price=d.get("exit_price"),
                    exit_time=d.get("exit_time"),
                    pnl=float(d.get("pnl", 0.0) or 0.0),
                    original_shares=int(d.get("original_shares", 0) or 0),
                    partial_exit_done=bool(d.get("partial_exit_done", False)),
                    breakeven_active=bool(d.get("breakeven_active", False)),
                    realized_pnl=float(d.get("realized_pnl", 0.0) or 0.0),
                    entry_reason=d.get("entry_reason", ""),
                    gap_pct=float(d.get("gap_pct", 0.0) or 0.0),
                )
                self.strategy._closed_positions.append(pos)
                restored += 1
            except Exception as e:
                log_message(f"[ENGINE] Could not restore closed trade {d.get('symbol','?')}: {e}")
        if restored:
            log_message(f"[ENGINE] Restored {restored} closed trade(s) for analytics.")


    def _save_positions(self) -> None:
        open_pos   = [vars(p) for p in self.strategy.get_active_positions()]
        closed_pos = [vars(p) for p in self.strategy.get_closed_positions()]
        self.data_handler.save_positions(open_pos, closed_pos)

    def get_swing_results(self) -> List[Dict]:
        return list(self._swing_results)

    def get_session_status(self) -> str:
        return self._session_name(self._get_now())

    def get_balance(self) -> Dict:
        return self.paper.snapshot(getattr(self, "_last_prices", {}) or {})

    def get_open_positions(self) -> List:
        return self.strategy.get_open_positions()

    def get_pending_positions(self) -> List:
        return self.strategy.get_pending_positions()

    def get_closed_positions(self) -> List:
        return self.strategy.get_closed_positions()

    def get_today_closed_positions(self) -> List:
        return self.strategy.get_today_closed_positions()


    def get_candidates_report(self) -> List[Dict]:
        return get_candidates_report(self)

    def get_scan_results(self) -> List[Dict]:
        rows = list(self._scan_results)
        for r in rows:
            try:
                self.sr_engine.attach(r)
            except Exception:
                pass
        return rows

    def _option_candles(self, symbol: str) -> List[Dict]:
        try:
            return self.scanner._get_today_candles(symbol) or []
        except Exception:
            return []

    def _run_options_scan(self) -> None:
        results = self.options_strategy.scan_universe(candle_fn=self._option_candles)
        self._uoa_results = results
        self._last_options_scan = time.time()
        self._last_uoa_scan = self._last_options_scan
        self.emit("options_cards", self.options_strategy.get_tab_data())
        tradable = [r["symbol"] for r in results if r.get("tradable")]
        ready = [r["symbol"] for r in results if r.get("ready")]
        log_message(f"[OPT] universe {len(tradable)} tradable / {len(results)} scanned, confirm {ready[:6]}")

    def _run_uoa_scan(self) -> None:
        """Back-compat alias used by /api/strategy/options/scan."""
        self._run_options_scan()

    def _poll_options(self) -> None:
        events = self.options_strategy.poll(
            self._option_candles,
            auto_trade=self.auto_trade,
            inherit_dry_run=self.dry_run,
        )
        for ev in events:
            pos = ev.get("pos") or {}
            if ev.get("type") == "close":
                self.emit("trade_closed", {
                    "symbol": pos.get("symbol"),
                    "pnl": pos.get("pnl"),
                    "reason": ev.get("reason") or pos.get("exit_reason"),
                    "strategy": "opt_confirm",
                })
            elif ev.get("type") == "open":
                self.emit("signal", type("S", (), {
                    "symbol": pos.get("symbol"),
                    "direction": pos.get("side") or pos.get("type") or "CALL",
                    "entry_price": pos.get("entry") or 0,
                    "stop_price": pos.get("kill") or 0,
                    "target_r2": pos.get("target") or 0,
                    "target_r3": pos.get("target") or 0,
                    "shares": pos.get("qty") or 0,
                    "risk_dollars": 0,
                    "reason": f"opt_confirm {pos.get('side')} {pos.get('occ')} kill={pos.get('kill')}",
                })())
        if events:
            self.emit("options_positions", self.options_strategy.get_tab_data())

    def _flatten_options(self, reason: str) -> None:
        dry = bool(getattr(self, "options_dry_run", True) or self.dry_run)
        opens = list(self.options_strategy.open_positions())
        if not opens:
            return
        for pos in opens:
            try:
                self.options_strategy._close(pos, f"flatten:{reason}", dry)
            except Exception as exc:
                log_message(f"[OPT] flatten error {pos.get('symbol')}: {exc}")
        self.options_strategy._positions = []
        self.options_strategy._save()
        log_message(f"[OPT] flattened {len(opens)} option position(s) ({reason})")

    def get_options_results(self) -> List[Dict]:
        return list(self.options_strategy.get_candidates())

    def get_uoa_results(self) -> List[Dict]:
        return self.get_options_results()

    def get_day_pnl(self) -> float:
        try:
            return float(self.paper.snapshot(getattr(self, "_last_prices", {}) or {}).get("day_pnl") or 0)
        except Exception:
            return self.strategy.get_day_pnl()

    def get_trades_today(self) -> int:
        return self.strategy.get_trades_today()

    def get_analytics(self) -> Dict:
        """Performance analytics over all closed trades (session + restored history).
        Returns overall metrics plus slices by setup, ET entry hour, and gap bucket."""
        closed = [vars(p) for p in self.strategy.get_closed_positions()]
        return compute_analytics(closed)
