#!/usr/bin/env python3
"""
Run both strategies WITHOUT market filter (no bear market skipping).
Uses the same data period and universe for fair comparison.
"""
import sys, os, json, time

PROJECT_DIR = "/home/super-user/screening"
sys.path.insert(0, PROJECT_DIR)

from strategies.backtest import MomentumMABacktester, DEFAULT_CONFIG as MOM_CFG
from strategies.backtest_dual_ma_gc import DualMAGoldenCrossBacktester, DEFAULT_CONFIG as DUAL_CFG

# Patch: disable market filter
import strategies.backtest as bt_mod
bt_mod.MomentumMABacktester.is_bear_market = lambda self, date: False

import strategies.backtest_dual_ma_gc as dual_mod
dual_mod.DualMAGoldenCrossBacktester.is_bear_market = lambda self, date: False

CFG = {
    "universe_size": 500, "backtest_days": 480, "rebalance_freq_days": 5,
    "max_mv": 1000, "min_total_shares": 0.5, "max_total_shares": 10,
    "top_n": 30, "kline_delay": 0, "kline_request_delay": 0,
    "stoploss_pct": 0.95, "take_profit_mult": 1.3,
    "volume_surge_ratio": 1.2, "gc_lookback_days": 3,
    "ma_short": 5, "ma_long": 18, "ma_stop": 10,
    "sina_page_size": 5000, "tencent_batch_size": 80,
    "data_dir": PROJECT_DIR,
}

results = {}
for name, bt_cls, cfg_key in [
    ("momentum_ma", MomentumMABacktester, MOM_CFG),
    ("dual_ma_gc", DualMAGoldenCrossBacktester, DUAL_CFG),
]:
    cfg = {**cfg_key, **CFG}
    bt = bt_cls(cfg)
    print(f"\n{'='*60}")
    print(f"Running {name}...")
    print(f"{'='*60}")
    t0 = time.time()
    result = bt.run()
    elapsed = time.time() - t0
    results[name] = result
    
    # Print summary
    n = result.total_trades
    wr = result.win_rate
    tr = result.total_return_pct
    avg = result.avg_return_pct
    dd = result.max_drawdown
    sr = result.sharpe_ratio
    print(f"\n{'╔':═^60}{'╗'}")
    print(f"║   {name:<53s}║")
    print(f"{'╠':═^60}{'╣'}")
    print(f"║  Trades:      {n:<6d}                      ║")
    print(f"║  Win Rate:    {wr:<6.1f} %                    ║")
    print(f"║  Total Ret:   {tr:<7.2f} %               ║")
    print(f"║  Avg/Trade:   {avg:<6.2f} %               ║")
    print(f"║  Max DD:      {dd:<6.2f} %                 ║")
    print(f"║  Sharpe:      {sr:<6.2f}                      ║")
    print(f"{'╚':═^60}{'╝'}")
    print(f"  Time: {elapsed:.0f}s")
    
    # Save
    os.makedirs(f"{PROJECT_DIR}/backtest_results", exist_ok=True)
    fpath = f"{PROJECT_DIR}/backtest_results/{name}_nofilter.json"
    with open(fpath, 'w') as f:
        json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"  Saved: {fpath}")

# Comparison
print(f"\n{'='*60}")
print("  COMPARISON: No Market Filter")
print(f"{'='*60}")
print(f"{'Strategy':<20} {'Trades':>8} {'Win%':>8} {'Return%':>10} {'Avg%':>8} {'MaxDD%':>8} {'Sharpe':>8}")
print(f"{'-'*20} {'-'*8} {'-'*8} {'-'*10} {'-'*8} {'-'*8} {'-'*8}")
for name, r in results.items():
    print(f"{name:<20} {r.total_trades:>8} {r.win_rate:>7.1f}% {r.total_return_pct:>9.2f}% {r.avg_return_pct:>7.2f}% {r.max_drawdown:>7.2f}% {r.sharpe_ratio:>7.2f}")
