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
    def __init__(self, engine_config=None, strategy_configs=None, data_fetcher=None):
        self.engine_config = {**ENGINE_CONFIG, **(engine_config or {})}
        self.strategy_configs = {**DEFAULT_STRATEGY_CONFIGS, **(strategy_configs or {})}
        self.fetcher = data_fetcher or DataFetcher(self.engine_config)

    def run(self, strategy_name, strategy_config=None, limit=0):
        t_start = time.time()
        strategy_cls = get_strategy(strategy_name)
        strategy = strategy_cls()
        log(f"=== Running strategy: {strategy.name} ===")

        config = {**self.strategy_configs.get(strategy_name, {}), **(strategy_config or {})}
        if limit > 0:
            config["kline_check_limit"] = limit

        log("Fetching all A-share stocks...")
        all_stocks = self.fetcher.get_all_a_stocks()
        log(f"  Total: {len(all_stocks)}")

        log("Fetching market data (MV, circ shares)...")
        market_data = self.fetcher.get_market_data(all_stocks, batch_size=self.engine_config.get("tencent_batch_size", 80))
        log(f"  Market data: {len(market_data)} stocks")

        context = StrategyContext(
            all_stocks=all_stocks, market_data=market_data,
            config=config, engine_config=self.engine_config,
        )

        log(f"Running strategy '{strategy.name}'...")
        selected = strategy.run(context)
        elapsed = time.time() - t_start

        result = ScreeningResult(
            strategy_name=strategy.name, total_stocks=len(all_stocks),
            after_filters=len(market_data), final=selected, elapsed=elapsed,
        )
        return result

    def save_result(self, result, path=None):
        if path is None:
            data_dir = self.engine_config.get("data_dir", "/home/super-user/screening")
            os.makedirs(data_dir, exist_ok=True)
            path = os.path.join(data_dir, "strategy_result.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        log(f"Result saved to {path}")
        return path

    def list_strategies(self):
        return list_strategies()
