"""
Strategy registry — decorator-based registration.
"""
from typing import Any

_registry: dict[str, dict[str, Any]] = {}


def register_strategy(cls):
    """Decorator to register a strategy class."""
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"Strategy {cls.__name__} must define a 'name' attribute")
    _registry[name] = {
        "class": cls,
        "name": name,
        "description": getattr(cls, "description", ""),
    }
    return cls


def get_strategy(name: str):
    """Get a strategy class by name."""
    entry = _registry.get(name)
    if not entry:
        available = list(_registry.keys())
        raise KeyError(f"Strategy '{name}' not found. Available: {available}")
    return entry["class"]


def list_strategies() -> dict[str, dict[str, Any]]:
    """List all registered strategies."""
    return {name: {"name": info["name"], "description": info["description"]}
            for name, info in _registry.items()}
