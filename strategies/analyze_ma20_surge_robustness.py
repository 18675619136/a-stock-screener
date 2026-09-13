"""路径依赖稳健性检验：同一策略/窗口/数据，仅随机抽取 90% 候选池，看组合收益的离散度。

若组合收益随随机子样本剧烈波动 → 组合层面的收益不可复现（是运气/路径依赖），
只有"仓位级平均收益"才是稳健的策略 edge 估计。
"""
import random
import statistics
import sys
import os
import importlib

sys.path.insert(0, "/home/super-user/screening")
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
m = importlib.import_module("strategies.backtest_ma20_surge_pullback")

CACHE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mkt_u800_d800.json"
MODE = sys.argv[2] if len(sys.argv) > 2 else "leg"
W0, W1 = (sys.argv[3], sys.argv[4]) if len(sys.argv) > 4 else ("2024-09-20", "2026-09-11")
N = int(sys.argv[5]) if len(sys.argv) > 5 else 8

cfg = dict(universe_size=800, days=9999, kline_days=800, turn_lookback=15, band=0.03,
           rally_thresh=0.08, rally_mode=MODE, capital=150000, slots=3, fee=0.005,
           max_mv=0, min_total_shares=0, max_total_shares=0, fallback_limit=60,
           cache=CACHE, require_arm=True, ma20_buffer=0.0, data_source="tdx",
           sina_datalen=1000, window_start=W0, window_end=W1)
mkt = m.load_or_build_cache(cfg)
nm = {s["code"]: s.get("name", s["code"]) for s in mkt["universe"]}
sig_all = {}
for code, bars in mkt["klines"].items():
    s = m.compute_signals(code, bars, cfg); s["name"] = nm.get(code, code); sig_all[code] = s

codes = sorted(sig_all)
print(f"候选池 {len(codes)} 只 | rally={MODE} | 窗口 {W0}→{W1} | {N} 次随机 90% 子样本")
print(f"{'#':>3} {'仓位':>5} {'仓位均值':>9} {'胜率':>7} {'组合收益':>10} {'回撤':>8} {'Sharpe':>7}")

totals, posavg, poswin, dds = [], [], [], []
for i in range(N):
    random.seed(1000 + i)
    sub = set(random.sample(codes, int(len(codes) * 0.9)))
    sig = {c: sig_all[c] for c in codes if c in sub}
    res = m.run_portfolio(sig, cfg)
    out = m.summarize(res, cfg, sig)
    totals.append(out["total_return_pct"]); posavg.append(out["pos_avg_return_pct"])
    poswin.append(out["pos_win_rate_pct"]); dds.append(out["max_drawdown_pct"])
    print(f"{i:>3} {out['positions_closed']:>5} {out['pos_avg_return_pct']:>8.2f}% "
          f"{out['pos_win_rate_pct']:>6.1f}% {out['total_return_pct']:>9.2f}% "
          f"{out['max_drawdown_pct']:>7.2f}% {out['sharpe']:>7.2f}")

print("-" * 62)
print(f"组合收益  均值 {statistics.mean(totals):>8.2f}%  中位 {statistics.median(totals):>7.2f}% "
      f" 范围 [{min(totals):.2f}% ~ {max(totals):.2f}%]  标准差 {statistics.stdev(totals):.2f}pp")
print(f"仓位均值  均值 {statistics.mean(posavg):>8.2f}%  中位 {statistics.median(posavg):>7.2f}% "
      f" 范围 [{min(posavg):.2f}% ~ {max(posavg):.2f}%]  标准差 {statistics.stdev(posavg):.2f}pp")
print(f"仓位胜率  均值 {statistics.mean(poswin):>8.2f}%  范围 [{min(poswin):.2f}% ~ {max(poswin):.2f}%]")
print(f"最大回撤  均值 {statistics.mean(dds):>8.2f}%  范围 [{min(dds):.2f}% ~ {max(dds):.2f}%]")
