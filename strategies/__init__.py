"""
Modular A-Share Stock Selection Strategy Framework
===================================================

A pluggable strategy framework for A-share stock selection.

Quick start:
    from strategies.engine import Engine
    engine = Engine()
    results = engine.run(strategy_name="momentum_ma")
    print(results.summary())
"""

# Import built-in strategies FIRST to trigger @register_strategy decorators
from . import builtins as _builtins  # noqa: F401

from .base import StrategyBase
from .registry import register_strategy, get_strategy, list_strategies
from .engine import Engine, ScreeningResult

__all__ = [
    "StrategyBase",
    "register_strategy",
    "get_strategy",
    "list_strategies",
    "Engine",
    "ScreeningResult",
]
