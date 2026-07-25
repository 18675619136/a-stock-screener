"""
Built-in strategy implementations.
"""

from .momentum_ma import MomentumMAStrategy
from .dual_ma_gc import DualMAGoldenCross

__all__ = ["MomentumMAStrategy", "DualMAGoldenCross"]
