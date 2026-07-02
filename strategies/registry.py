"""
Strategy registry — strategies register themselves by name.

Usage:
    @register_strategy
    class MyStrategy(StrategyBase):
        name = "my_strategy"
        ...

    # Then:
    get_strategy("my_strategy") -> MyStrategy class
    list_strategies() -> {"my_strategy": MyStrategy, ...}
"""

from typing import Type
from .base import StrategyBase

_registry: dict[str, Type[StrategyBase]] = {}


def register_strategy(cls: Type[StrategyBase]) -> Type[StrategyBase]:
    """Decorator that registers a strategy class by its .name attribute.

    Can also be used as a plain function:
        register_strategy(MyStrategy)
    """
    if not issubclass(cls, StrategyBase):
        raise TypeError(f"{cls.__name__} must subclass StrategyBase")

    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'name' class attribute")

    if name in _registry:
        raise KeyError(f"Strategy '{name}' is already registered (existing: {_registry[name]})")

    _registry[name] = cls
    return cls


def get_strategy(name: str) -> Type[StrategyBase]:
    """Look up a strategy class by name.

    Raises KeyError if not found.
    """
    if name not in _registry:
        registered = ", ".join(sorted(_registry.keys()))
        raise KeyError(
            f"Unknown strategy '{name}'. "
            f"Registered strategies: {registered or '(none)'}"
        )
    return _registry[name]


def list_strategies() -> dict[str, dict]:
    """Return a dict of {name: {name, description}} for all registered strategies."""
    return {
        name: {"name": name, "description": cls.description}
        for name, cls in _registry.items()
    }
