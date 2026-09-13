"""
paper_client.py
Paper trading client — real Schwab market data, simulated order fills.

Inherits SimClient's complete order simulation engine (OCO brackets,
trailing stops, position tracking, fill checking) but delegates ALL
price and candle data to a live SchwabClient.

This means:
  - Scanner prices, candles, ORB levels, indicators  → real Schwab API
  - Order placement, fills, balance, positions       → SimClient in-memory
  - Stop / target fill checks run against real prices → realistic paper P&L

Usage (in trading_engine.py enable_simulation):
    paper = PaperClient(self.client)   # self.client is the live SchwabClient
    self.client = paper
    self.scanner.client = paper
    self.swing_scanner.client = paper
"""

from typing import Dict, List, Optional, Any

from sim_client import SimClient
from utils import log_message


class PaperClient(SimClient):
    """
    Drop-in replacement for SimClient that uses real Schwab price data.
    Order execution, fills, and position tracking remain fully simulated.
    """

    def __init__(self, schwab_client) -> None:
        super().__init__()
        self._real       = schwab_client          # live SchwabClient instance
        self.app_key     = schwab_client.app_key
        self.app_secret  = schwab_client.app_secret

    # ── Authentication (delegates to real client) ────────────────────────────

    def is_authenticated(self) -> bool:
        return self._real.is_authenticated()

    def _ensure_token(self) -> bool:
        return self._real._ensure_token()

    def get_account_hash(self) -> Optional[str]:
        return self._real.get_account_hash()

    # ── Market data — ALL delegated to real Schwab ───────────────────────────

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Real-time quote from Schwab — used by scanner, signal evaluation,
        and SimClient._check_fills() so stop/target levels trigger at real prices."""
        return self._real.get_quote(symbol)

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """Batch real-time quotes for the trade loop and scanner."""
        return self._real.get_quotes(symbols)

    def get_price_history(self, symbol: str, **kwargs) -> List[Dict]:
        """Real 1-min candle history — ORB levels and indicators use real data."""
        return self._real.get_price_history(symbol, **kwargs)

    def get_movers(self, indices=None, direction: str = "up", change: str = "PERCENT") -> List[str]:
        """Real market movers from Schwab — scanner candidates are accurate."""
        return self._real.get_movers(indices=indices, direction=direction, change=change)

    def search_instruments(self, query: str, projection: str = "symbol-search") -> List:
        return self._real.search_instruments(query, projection)

    # ── Order execution, positions, balance ─────────────────────────────────
    # All inherited from SimClient — fully simulated, no real orders placed.
    #
    # Key inherited methods:
    #   get_balance()              → simulated cash balance
    #   get_positions()            → simulated holdings
    #   get_orders()               → simulated open orders
    #   place_market_order()       → SIM-XXXX order ID, immediate sim fill
    #   place_oco_order()          → SIM-XXXX, tracked stop+target bracket
    #   place_trailing_stop_order()→ SIM-XXXX, trailing stop tracked in-memory
    #   cancel_order()             → removes from sim order book
    #   _check_fills()             → now uses real prices (via get_quote above)
    #                                so stops fire at real market levels
