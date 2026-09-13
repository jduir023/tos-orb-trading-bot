"""
strategy_base.py
Abstract interface that every strategy module must implement.
New strategies inherit from this class; legacy strategies are wrapped
in a _StrategyDescriptor (see strategy_registry.py).
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class StrategyBase(ABC):
    """Interface contract for all strategy modules."""

    name: str = ""
    strategy_id: str = ""
    tab_id: str = ""
    description: str = ""

    def __init__(self) -> None:
        self._enabled: bool = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    @abstractmethod
    def set_config(self, **kwargs) -> None:
        """Apply configuration pushed down from the engine or dashboard."""

    @abstractmethod
    def reset_day(self) -> None:
        """Reset per-day state at the start of each trading session."""

    @abstractmethod
    def evaluate(self, symbol: str, **kwargs) -> Optional[Any]:
        """Return a TradeSignal if entry conditions are met, else None."""

    def get_tab_data(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name":        self.name,
            "tab_id":      self.tab_id,
            "description": self.description,
            "enabled":     self._enabled,
        }

    def get_candidates(self) -> List[Dict]:
        return []
