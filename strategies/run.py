#!/usr/bin/env python3
"""
A股选股策略框架 — CLI入口

Usage:
    python3 -m strategies.run momentum_ma          # Run default strategy
    python3 -m strategies.run momentum_ma --limit 10  # Test mode (first 10)
    python3 -m strategies.run dual_ma_gc           # Run dual_ma_gc strategy
    python3 -m strategies.run --list               # List all strategies
    python3 -m strategies.run --help               # Show help
"""

import argparse
import sys
import os

# Ensure the parent directory is on the path so we can import strategies
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def main():
    parser = argparse.ArgumentParser(
        description="A股选股策略框架 — 模块化可插拔策略引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m strategies.run momentum_ma              # 默认策略
  python3 -m strategies.run momentum_ma --limit 10   # 测试模式
  python3 -m strategies.run dual_ma_gc               # 双均线金叉策略
  python3 -m strategies.run momentum_ma --save       # 运行并保存结果
  python3 -m strategies.run --list                   # 列出所有策略
        """,
    )
    parser.add_argument(
        "strategy", nargs="?",
        help="Strategy name to run (use --list to see available)"
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Test mode: only check first N candidates (skip most klines)"
    )
    parser.add_argument(
        "--list", "-l", action="store_true",
        help="List all registered strategies"
    )
    parser.add_argument(
        "--save", "-s", action="store_true",
        help="Save result to JSON file"
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output file path (default: screening/strategy_result.json)"
    )
    parser.add_argument(
        "--config", "-c", type=str, default=None,
        help="Strategy config override as JSON string, e.g. '{\"max_mv\": 500}'"
    )

    args = parser.parse_args()

    if args.list:
        from strategies.engine import Engine
        engine = Engine()
        strategies = engine.list_strategies()
        print("Registered strategies:")
        print("=" * 60)
        for name, info in sorted(strategies.items()):
            desc = info.get("description", "")
            print(f"  {name:<20s}  {desc[:60]}")
        return

    if not args.strategy:
        parser.print_help()
        return

    # Parse optional config override
    config_override = None
    if args.config:
        import json
        try:
            config_override = json.loads(args.config)
        except json.JSONDecodeError as e:
            print(f"Error parsing --config: {e}", file=sys.stderr)
            sys.exit(1)

    # Run
    from strategies.engine import Engine
    engine = Engine()
    result = engine.run(
        strategy_name=args.strategy,
        strategy_config=config_override,
        limit=args.limit,
    )

    print("\n" + result.summary())

    if args.save:
        engine.save_result(result, args.output)


if __name__ == "__main__":
    main()
