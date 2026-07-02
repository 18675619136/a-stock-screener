#!/usr/bin/env python3
"""
Test suite for the A-share stock selection strategy framework.
Tests the framework structure — real API calls are skipped (mock-based).
"""

import sys
import os
import json

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# ─── Test 1: Import everything ────────────────────────────────────
def test_imports():
    print("=== Test 1: Imports ===")
    from strategies import StrategyBase, register_strategy, get_strategy, list_strategies, Engine, ScreeningResult
    from strategies.base import StrategyContext
    from strategies.registry import _registry
    from strategies.data.fetcher import DataFetcher, code_to_prefix, match_track, safe_float
    from strategies.config import ENGINE_CONFIG, STRATEGY_CONFIGS
    print("  ✓ All imports successful")
    print(f"  ✓ Engine config keys: {list(ENGINE_CONFIG.keys())}")
    print(f"  ✓ Strategy configs: {list(STRATEGY_CONFIGS.keys())}")

# ─── Test 2: Registry works ───────────────────────────────────────
def test_registry():
    print("\n=== Test 2: Registry ===")
    from strategies import list_strategies, get_strategy
    strategies = list_strategies()
    print(f"  ✓ Registered strategies: {list(strategies.keys())}")
    assert "momentum_ma" in strategies, "momentum_ma should be registered"
    
    # Instantiate
    from strategies import get_strategy
    cls = get_strategy("momentum_ma")
    instance = cls()
    print(f"  ✓ Instantiated: {instance.name} — {instance.description[:50]}...")

# ─── Test 3: StrategyContext / ScreeningResult ────────────────────
def test_data_classes():
    print("\n=== Test 3: Data classes ===")
    from strategies.base import StrategyContext, ScreeningResult
    
    ctx = StrategyContext()
    print(f"  ✓ StrategyContext created: klines={len(ctx.klines)}, stocks={len(ctx.all_stocks)}")
    
    result = ScreeningResult(
        strategy_name="test",
        total_stocks=5000,
        after_filters=800,
        final=[
            {"code": "000001", "name": "平安银行", "price": 12.34, "mv": 200, "score": 0.95},
            {"code": "600519", "name": "贵州茅台", "price": 1800, "mv": 22000, "score": 0.50},
        ],
        elapsed=12.5,
    )
    summary = result.summary()
    assert "平安银行" in summary
    assert "Strategy: test" in summary
    print(f"  ✓ ScreeningResult.summary() OK ({len(summary)} chars)")
    
    d = result.to_dict()
    assert d["strategy"] == "test"
    assert d["final_count"] == 2
    print(f"  ✓ ScreeningResult.to_dict() OK: keys={list(d.keys())}")

# ─── Test 4: DataFetcher utilities ─────────────────────────────────
def test_fetcher_utils():
    print("\n=== Test 4: DataFetcher utilities ===")
    from strategies.data.fetcher import code_to_prefix, match_track, safe_float
    
    assert code_to_prefix("600000") == "sh"
    assert code_to_prefix("000001") == "sz"
    assert code_to_prefix("300750") == "sz"
    assert code_to_prefix("688001") == "sh"
    assert code_to_prefix("830000") == "bj"
    assert code_to_prefix("") == ""
    print("  ✓ code_to_prefix: all cases correct")
    
    assert match_track("半导体ETF") == "半导体/芯片"
    assert match_track("恒瑞医药") == "创新药/医药"
    assert match_track("长江电力") == "电力/公用"
    assert match_track("万科A") == "地产/基建"
    assert match_track("某某某某") == "其他"
    print("  ✓ match_track: all cases correct")
    
    assert safe_float("12.34") == 12.34
    assert safe_float("-") == 0.0
    assert safe_float(None) == 0.0
    assert safe_float("abc", 5.0) == 5.0
    print("  ✓ safe_float: all cases correct")

# ─── Test 5: Engine with offline mock ──────────────────────────────
def test_engine_offline():
    print("\n=== Test 5: Engine (offline/mock structure) ===")
    from strategies.engine import Engine
    
    engine = Engine()
    strategies = engine.list_strategies()
    assert "momentum_ma" in strategies
    print(f"  ✓ Engine created, {len(strategies)} strategies available")
    print(f"  ✓ Engine config has {len(engine.engine_config)} keys")

# ─── Test 6: Custom strategy registration ─────────────────────────
def test_custom_strategy():
    print("\n=== Test 6: Custom strategy registration ===")
    from strategies import StrategyBase, register_strategy, get_strategy, list_strategies

    @register_strategy
    class MyCustomStrategy(StrategyBase):
        name = "my_custom"
        description = "A custom test strategy"

        def run(self, context):
            return [{"code": "000001", "name": "TEST", "score": 1.0}]

    assert "my_custom" in list_strategies()
    cls = get_strategy("my_custom")
    instance = cls()
    result = instance.run(None)
    assert result[0]["code"] == "000001"
    print("  ✓ Custom strategy registered and executed successfully")

# ─── Test 7: Error handling ───────────────────────────────────────
def test_error_handling():
    print("\n=== Test 7: Error handling ===")
    from strategies.registry import get_strategy
    
    try:
        get_strategy("nonexistent_strategy")
        assert False, "Should have raised KeyError"
    except KeyError as e:
        print(f"  ✓ KeyError raised for unknown strategy: {e}")

    try:
        from strategies.registry import register_strategy
        class BadStrategy:
            pass
        register_strategy(BadStrategy)  # type: ignore
        assert False, "Should have raised TypeError"
    except TypeError as e:
        print(f"  ✓ TypeError raised for non-StrategyBase: {e}")


if __name__ == "__main__":
    test_imports()
    test_registry()
    test_data_classes()
    test_fetcher_utils()
    test_engine_offline()
    test_custom_strategy()
    test_error_handling()
    
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✓")
    print("=" * 60)
