#!/usr/bin/env python3
"""
A股选股策略框架 — CLI入口

Usage:
    python3 -m strategies.run momentum_ma              # 默认策略
    python3 -m strategies.run momentum_ma --limit 10   # 测试模式
    python3 -m strategies.run golden_cross             # 金叉策略
    python3 -m strategies.run --list                   # 列出所有策略
    python3 -m strategies.run --save                   # 运行并保存
"""

import argparse, sys, os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def main():
    parser = argparse.ArgumentParser(description="A股选股策略框架 — 模块化可插拔策略引擎")
    parser.add_argument("strategy", nargs="?", help="Strategy name (use --list to see available)")
    parser.add_argument("--limit", type=int, default=0, help="Test mode: only check first N candidates")
    parser.add_argument("--list", "-l", action="store_true", help="List all registered strategies")
    parser.add_argument("--save", "-s", action="store_true", help="Save result to JSON file")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output file path")
    parser.add_argument("--config", "-c", type=str, default=None, help="Strategy config as JSON string")
    args = parser.parse_args()

    if args.list:
        from strategies.engine import Engine
        engine = Engine()
        for name, info in sorted(engine.list_strategies().items()):
            print(f"  {name:<20s}  {info.get('description', '')[:60]}")
        return

    if not args.strategy:
        parser.print_help()
        return

    config_override = None
    if args.config:
        import json as j
        config_override = j.loads(args.config)

    from strategies.engine import Engine
    engine = Engine()
    result = engine.run(strategy_name=args.strategy, strategy_config=config_override, limit=args.limit)
    print("\n" + result.summary())

    if args.save:
        engine.save_result(result, args.output)


if __name__ == "__main__":
    main()
