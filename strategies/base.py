"""
Strategy base class — all strategies implement this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StrategyContext:
    """Data context passed to every strategy's run() method.

    Attributes:
        all_stocks: Raw list of all A-share stocks from Sina.
        market_data: Dict[code -> {name, price, mv, circ, ...}] from Tencent.
        klines: Dict[code -> list of {close, volume, ...}] — fetched lazily.
        config: Strategy-specific configuration dict.
        engine_config: Global engine configuration (data limits, timeouts, etc.).
    """
    all_stocks: list[dict[str, Any]] = field(default_factory=list)
    market_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    klines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    engine_config: dict[str, Any] = field(default_factory=dict)

    def get_kline(self, code: str) -> list[dict[str, Any]] | None:
        """Safely retrieve a kline for a given code."""
        return self.klines.get(code)


@dataclass
class ScreeningResult:
    """Result of a screening run."""

    strategy_name: str = ""
    total_stocks: int = 0
    after_filters: int = 0
    final: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"╔══════════════════════════════════════╗",
            f"║  Strategy: {self.strategy_name:<26s} ║",
            f"║  Total A-shares:  {self.total_stocks:<5d}                  ║",
            f"║  Passed filters: {self.after_filters:<5d}                  ║",
            f"║  Final picks:    {len(self.final):<5d}                  ║",
            f"║  Time:           {self.elapsed:<5.1f}s                  ║",
            f"╚══════════════════════════════════════╝",
        ]
        if self.final:
            lines.append("")
            lines.append(f"{'Code':>8s}  {'Name':>10s}  {'Price':>7s}  {'MV(b)':>7s}  {'Score':>6s}")
            lines.append("-" * 50)
            for s in self.final[:20]:
                lines.append(
                    f"{s.get('code', ''):>8s}  "
                    f"{s.get('name', ''):>10s}  "
                    f"{s.get('price', 0):>7.2f}  "
                    f"{s.get('mv', 0):>7.1f}  "
                    f"{s.get('score', 0):>6.2f}"
                )
            if len(self.final) > 20:
                lines.append(f"  ... and {len(self.final) - 20} more")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize to dict for JSON output."""
        return {
            "strategy": self.strategy_name,
            "total_stocks": self.total_stocks,
            "after_filters": self.after_filters,
            "final_count": len(self.final),
            "stocks": self.final,
            "details": self.details,
            "elapsed": round(self.elapsed, 2),
        }


class StrategyBase(ABC):
    """Abstract base class for all stock selection strategies.

    Subclasses must define:
        name: Short unique identifier (used in registry).
        description: Human-readable description.

    Subclasses must implement:
        run(context: StrategyContext) -> list[dict]
            Return a list of selected stocks, each as a dict with at minimum:
                'code', 'name', 'price', 'mv', 'score'
    """

    name: str = ""
    description: str = ""

    @abstractmethod
    def run(self, context: StrategyContext) -> list[dict[str, Any]]:
        """Execute the strategy.

        Args:
            context: Full data context (stocks, market data, klines, config).

        Returns:
            List of selected stock dicts, each containing at least:
                code, name, price, mv, score
        """
        ...
