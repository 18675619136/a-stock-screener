"""
Core engine — fetches data and runs the selected strategy.
"""

import time
import json
import os
from typing import Any

from .base import StrategyBase, StrategyContext, ScreeningResult
from .registry import get_strategy, list_strategies
from .data.fetcher import DataFetcher, log
from .config import ENGINE_CONFIG, STRATEGY_CONFIGS as DEFAULT_STRATEGY_CONFIGS


class Engine:
    """Orchestrator that loads data and runs a strategy.

    Usage:
        engine = Engine()
        result = engine.run("momentum_ma")
        print(result.summary())
    """

    def __init__(
        self,
        engine_config: dict | None = None,
        strategy_configs: dict[str, dict] | None = None,
        data_fetcher: DataFetcher | None = None,
    ):
        self.engine_config = {**ENGINE_CONFIG, **(engine_config or {})}
        self.strategy_configs = {
            **DEFAULT_STRATEGY_CONFIGS,
            **(strategy_configs or {}),
        }
        self.fetcher = data_fetcher or DataFetcher(self.engine_config)

    def run(
        self,
        strategy_name: str,
        strategy_config: dict | None = None,
        limit: int = 0,
    ) -> ScreeningResult:
        """Execute a named strategy.

        Args:
            strategy_name: Name of registered strategy.
            strategy_config: Override strategy-specific config.
            limit: If > 0, limit kline checks to this many candidates (test mode).

        Returns:
            ScreeningResult with final picks and statistics.
        """
        t_start = time.time()

        # 1. Look up strategy
        strategy_cls = get_strategy(strategy_name)
        strategy: StrategyBase = strategy_cls()
        log(f"=== Running strategy: {strategy.name} ===")

        # 2. Merge configs
        config = {
            **self.strategy_configs.get(strategy_name, {}),
            **(strategy_config or {}),
        }
        if limit > 0:
            config["kline_check_limit"] = limit

        # 3. Fetch data
        log("▶ Fetching all A-share stocks...")
        all_stocks = self.fetcher.get_all_a_stocks()
        log(f"  Total: {len(all_stocks)}")

        log("▶ Fetching market data (MV, circ shares)...")
        market_data = self.fetcher.get_market_data(
            all_stocks, batch_size=self.engine_config.get("tencent_batch_size", 80)
        )
        log(f"  Market data: {len(market_data)} stocks")

        # 4. Build context
        context = StrategyContext(
            all_stocks=all_stocks,
            market_data=market_data,
            config=config,
            engine_config=self.engine_config,
        )

        # 5. Run the strategy
        log(f"▶ Running strategy '{strategy.name}'...")
        selected = strategy.run(context)

        elapsed = time.time() - t_start

        # 6. Build result
        result = ScreeningResult(
            strategy_name=strategy.name,
            total_stocks=len(all_stocks),
            after_filters=len(market_data),
            final=selected,
            elapsed=elapsed,
        )

        log(result.summary())
        return result

    def save_result(self, result: ScreeningResult, path: str | None = None) -> str:
        """Save screening result to a JSON file."""
        if path is None:
            data_dir = self.engine_config.get("data_dir", "/home/super-user/screening")
            os.makedirs(data_dir, exist_ok=True)
            path = os.path.join(data_dir, "strategy_result.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        log(f"Result saved to {path}")
        return path

    def list_strategies(self) -> dict:
        """Return info about all registered strategies."""
        return list_strategies()
