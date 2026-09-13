"""无资金约束的"全部信号"模拟：每条信号都独立开一笔 100% 本金仓位，
用与组合回测完全相同的出场规则（T+1 / armed后跌破MA5卖半 / 跌破MA20清仓）。

作用：排除「3 个槽位 + 先到先得」带来的路径依赖，得到策略**真实的入场出场期望值**。
组合回测只能实现其中约 300/11581 的信号，选哪 300 个由任意顺序决定 → 组合收益是抽签。
"""
import statistics
import sys
import os
import importlib
from collections import Counter

sys.path.insert(0, "/home/super-user/screening")
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
m = importlib.import_module("strategies.backtest_ma20_surge_pullback")

CACHE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mkt_u800_d800.json"
MODE = sys.argv[2] if len(sys.argv) > 2 else "leg"
W0, W1 = (sys.argv[3], sys.argv[4]) if len(sys.argv) > 4 else ("2024-09-20", "2026-09-11")
FEE = float(sys.argv[5]) if len(sys.argv) > 5 else 0.005
BUF = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0
MF = (sys.argv[7].lower() in ("1", "on", "true", "yes")) if len(sys.argv) > 7 else False
HS = float(sys.argv[8]) if len(sys.argv) > 8 else 0.0

cfg = dict(universe_size=800, days=9999, kline_days=800, turn_lookback=15, band=0.03,
           rally_thresh=0.08, rally_mode=MODE, capital=150000, slots=3, fee=FEE,
           max_mv=0, min_total_shares=0, max_total_shares=0, fallback_limit=60,
           cache=CACHE, require_arm=True, ma20_buffer=BUF, data_source="tdx",
           sina_datalen=1000, window_start=W0, window_end=W1)
mkt = m.load_or_build_cache(cfg)
nm = {s["code"]: s.get("name", s["code"]) for s in mkt["universe"]}

rets, holds, reasons = [], [], Counter()
bear_days = m.load_index_bear_days(cfg) if MF else set()
skipped_mf = 0
for code, bars in mkt["klines"].items():
    s = m.compute_signals(code, bars, cfg)
    dates, d2i = s["dates"], None
    d2i = {d: i for i, d in enumerate(dates)}
    closes, ma5, ma20 = [b["close"] for b in bars], s["ma5"], s["ma20"]
    for sd in s["signals"]:
        if not (W0 <= sd <= W1):
            continue
        if MF and sd in bear_days:
            skipped_mf += 1
            continue
        i0 = d2i[sd]
        buy = closes[i0]
        armed = ma5[i0] > 0 and buy >= ma5[i0]
        leg1 = None            # 卖半的 (idx, price)
        exit_i = None
        for i in range(i0 + 1, len(closes)):        # T+1
            c = closes[i]
            if HS > 0 and c <= buy * (1 - HS):       # 硬止损优先
                exit_i = i
                break
            if ma20[i] > 0 and c < ma20[i] * (1 - BUF):
                exit_i = i
                break
            if leg1 is None and armed and ma5[i] > 0 and c <= ma5[i]:
                leg1 = (i, c)
            if ma5[i] > 0 and c >= ma5[i]:
                armed = True
        if exit_i is None:
            exit_i = len(closes) - 1
        if exit_i <= i0:
            continue
        r1 = ((leg1[1] / buy - 1) * 100 - FEE * 200) if leg1 else None
        r2 = (closes[exit_i] / buy - 1) * 100 - FEE * 200
        if r1 is None:
            r = r2
        else:
            r = 0.5 * r1 + 0.5 * r2
        rets.append(r)
        holds.append(exit_i - i0)
        reasons["MA5_half+MA20" if leg1 else "MA20_only"] += 1

rets_s = sorted(rets)
n = len(rets)
wins = [r for r in rets if r > 0]
losses = [r for r in rets if r <= 0]
print("=" * 74)
print(f"  全部信号等权模拟  rally={MODE}  手续费={FEE*100:.2f}%(单边)  窗口 {W0}→{W1}")
print(f"  入场容差={BUF*100:.0f}%   硬止损={HS*100:.0f}%   大盘择时={'on' if MF else 'off'}"
      f"   信号总数={n}" + (f" (择时跳过 {skipped_mf})" if MF else ""))
print("=" * 74)
print(f"  平均收益/笔       {sum(rets)/n:+.2f}%")
print(f"  中位收益/笔       {rets_s[n//2]:+.2f}%")
print(f"  胜率              {len(wins)/n*100:.1f}%")
print(f"  平均盈利          {sum(wins)/len(wins):+.2f}%" if wins else "  -")
print(f"  平均亏损          {sum(losses)/len(losses):+.2f}%" if losses else "  -")
print(f"  盈亏比            {(sum(wins)/len(wins))/abs(sum(losses)/len(losses)):.2f}"
      if wins and losses else "  -")
print(f"  平均持仓(交易日)  {sum(holds)/len(holds):.1f}")
print(f"  等于满仓单利的年化 {((sum(rets)/n)/100)*(252/(sum(holds)/len(holds)))*100:+.1f}%"
      f"  (n笔均值×年周转次数)")
best = rets_s[-8:][::-1]
print(f"  最好8笔: {[round(x,1) for x in best]}")
print(f"  最差8笔: {[round(x,1) for x in rets_s[:8]]}")
print(f"  出场构成: {dict(reasons)}")
