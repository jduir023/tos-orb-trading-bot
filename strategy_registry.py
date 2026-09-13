"""
strategy_registry.py
Singleton registry of all strategy modules.
New strategies register a StrategyBase subclass; legacy strategies
register a _StrategyDescriptor that delegates enable/disable calls
to the engine's set_config() without requiring a full migration.
"""
from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional


class _StrategyDescriptor:
    """
    Lightweight proxy for legacy strategies that have not yet been migrated
    to StrategyBase.  enabled_getter / enabled_setter delegate to the engine.
    """

    def __init__(
        self,
        strategy_id: str,
        name: str,
        tab_id: str,
        description: str,
        enabled_getter: Callable[[], bool],
        enabled_setter: Callable[[bool], None],
        candidates_getter: Optional[Callable[[], list]] = None,
        tab_data_getter: Optional[Callable[[], dict]] = None,
    ) -> None:
        self.strategy_id        = strategy_id
        self.name               = name
        self.tab_id             = tab_id
        self.description        = description
        self._enabled_getter    = enabled_getter
        self._enabled_setter    = enabled_setter
        self._candidates_getter = candidates_getter or (lambda: [])
        self._tab_data_getter   = tab_data_getter

    @property
    def enabled(self) -> bool:
        return bool(self._enabled_getter())

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled_setter(bool(value))

    def get_tab_data(self) -> Dict[str, Any]:
        if self._tab_data_getter:
            return self._tab_data_getter()
        return {
            "strategy_id": self.strategy_id,
            "name":        self.name,
            "tab_id":      self.tab_id,
            "description": self.description,
            "enabled":     self.enabled,
            "candidates":  self.get_candidates(),
        }

    def get_candidates(self) -> list:
        return self._candidates_getter()


class StrategyRegistry:
    """
    Singleton.  All strategies register here so the engine and dashboard
    can enumerate them without hard-coding strategy names everywhere.
    """

    _instance: Optional["StrategyRegistry"] = None

    def __new__(cls) -> "StrategyRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._entries: Dict[str, Any] = {}
        return cls._instance

    def register(self, entry: Any) -> None:
        self._entries[entry.strategy_id] = entry

    def get(self, strategy_id: str) -> Optional[Any]:
        return self._entries.get(strategy_id)

    def all(self) -> List[Any]:
        return list(self._entries.values())

    def enabled(self) -> List[Any]:
        return [e for e in self._entries.values() if e.enabled]

    def get_status(self) -> List[Dict[str, Any]]:
        return [e.get_tab_data() for e in self._entries.values()]

    def set_enabled(self, strategy_id: str, enabled: bool) -> bool:
        entry = self._entries.get(strategy_id)
        if entry is None:
            return False
        entry.enabled = enabled
        return True
