#!/usr/bin/env python3
"""
熊市回测：双均线金叉 PICK3（评分Top3）在 2022-01 ~ 2024-07 的熊市环境表现。

使用 baostock 获取长周期历史K线数据。

策略逻辑与 dual_ma_gc v3 一致：
  - 买入: MA10 > MA30（均线金叉状态）+ 金叉发生在最近3日内 + 成交量>0
  - 排序: MA强度30% + 放量20% + 动量20% + 小市值15% + 金叉新鲜度15%
  - 买入Top3（今日候选→明日买入）
  - 卖出: SL_94pct > MA18_below > TP_MA5x1.3 > SL_below_MA5
  - A股 T+1 规则遵守
"""

import sys, os, json, time, math, statistics
import baostock as bs
from datetime import datetime, timedelta
from collections import defaultdict

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from strategies.backtest import (
    TradeRecord, BacktestResult, Position, _date_diff, calc_ma, COST
)
from strategies.data.fetcher import (
    DataFetcher, log, safe_float, code_to_prefix, SINA_ALL_URL
)

# ========== Configuration ==========
CONFIG = {
    "universe_size": 500,
    "rebalance_freq_days": 5,
    "backtest_start": "2022-01-01",
    "backtest_end": "2024-07-31",
    "max_mv": 1000,
    "min_total_shares": 0.5,
    "max_total_shares": 10,
    "ma_short": 10,       # dual_ma_gc uses MA10
    "ma_long": 30,        # dual_ma_gc uses MA30
    "gc_lookback_days": 3, # golden cross within last 3 days
    "top_n": 15,
    "pick_count": 3,
    "pick_mode": "score",
    "stoploss_pct": 0.94,
    "take_profit_mult": 1.3,
    "sell_ma_short": 5,   # Sell using MA5 (same as momentum_ma)
    "sell_ma_long": 18,   # Sell using MA18
}

ST_NAME_PREFIXES = ("ST", "*ST", "S")

def is_st(name):
    return name.startswith(ST_NAME_PREFIXES) or "退" in name

# ========== Step 1: Fetch universe ==========
log("=" * 60)
log("熊市回测 (2022-01 ~ 2024-07)：双均线金叉 PICK3")
log("=" * 60)

log("\nStep 1: Fetching stock universe (Sina + Tencent)...")
fetcher = DataFetcher(CONFIG)
all_stocks = fetcher.get_all_a_stocks()
log(f"  Total A-share stocks: {len(all_stocks)}")

filtered = [s for s in all_stocks if not is_st(s.get("name","")) and s.get("price",0) > 0]
log(f"  After ST/price filter: {len(filtered)}")

market_data = fetcher.get_market_data(filtered, batch_size=80)
log(f"  Market data: {len(market_data)} stocks")

enriched = []
for s in filtered:
    code = s["code"]
    md = market_data.get(code)
    if not md: continue
    mv = md.get("mv", 0)
    total_shares = md.get("total_shares", 0)
    name = md.get("name", "")
    if mv <= 0 or mv > CONFIG["max_mv"]: continue
    if total_shares <= 0 or total_shares < CONFIG["min_total_shares"] or total_shares > CONFIG["max_total_shares"]: continue
    if is_st(name): continue
    enriched.append({
        "code": code, "name": name, "mv": mv,
        "total_shares": total_shares,
        "amount": s.get("amount", 0),
        "price": md.get("price", 0),
        "changepercent": s.get("changepercent", 0),
    })

enriched.sort(key=lambda x: x["amount"], reverse=True)
universe = enriched[:CONFIG["universe_size"]]
md_map = {s["code"]: s for s in universe}
log(f"  Universe: {len(universe)} stocks (MV<={CONFIG['max_mv']}亿)")

# ========== Step 2: Fetch klines from baostock ==========
log("\nStep 2: Fetching klines from baostock (2022-01-01 ~ 2024-07-31)...")

def to_bs_code(code):
    if code.startswith(("6", "9", "68")):
        return f"sh.{code}"
    elif code.startswith(("0", "3")):
        return f"sz.{code}"
    elif code.startswith(("92", "8", "4")):
        return f"bj.{code}"
    return None

codes_to_fetch = [s["code"] for s in universe]
total = len(codes_to_fetch)

log(f"  Fetching {total} stocks via baostock (single session)...")
bs.login()
klines_data = {}
fetched = 0
errors = 0

for i, code in enumerate(codes_to_fetch):
    bs_code = to_bs_code(code)
    if not bs_code:
        errors += 1
        continue
    try:
        rs = bs.query_history_k_data_plus(
            bs_code,
            'date,open,high,low,close,volume,amount',
            start_date='2022-01-01', end_date='2024-07-31',
            frequency='d', adjustflag='2'
        )
        rows = []
        while (rs.error_code == '0') & rs.next():
            rows.append(rs.get_row_data())
        if len(rows) >= 150:  # Need more data for MA30
            records = []
            for r in rows:
                try:
                    records.append({
                        "date": r[0].replace("-", ""),
                        "open": float(r[1]),
                        "close": float(r[2]),
                        "high": float(r[3]),
                        "low": float(r[4]),
                        "volume": float(r[5]) if r[5] else 0,
                        "amount": float(r[6]) if r[6] else 0,
                    })
                except (ValueError, IndexError):
                    continue
            klines_data[code] = records
            fetched += 1
        else:
            errors += 1
    except Exception:
        errors += 1
    if (i + 1) % 50 == 0:
        log(f"  Progress: {i+1}/{total}, ok={fetched}, err={errors}")

bs.logout()
log(f"  Klines fetched: {fetched} stocks (errors: {errors})")

if fetched < 50:
    log("ERROR: Too few stocks with kline data, aborting.")
    sys.exit(1)

# ========== Step 3: Build date axis ==========
log("\nStep 3: Building date axis...")
all_dates = set()
for code, klines in klines_data.items():
    for k in klines:
        all_dates.add(k["date"])
all_dates = sorted(all_dates)
log(f"  Total trading days: {len(all_dates)} ({all_dates[0]} ~ {all_dates[-1]})")

backtest_dates = [d for d in all_dates if "20220101" <= d <= "20240731"]
log(f"  Backtest period: {backtest_dates[0]} ~ {backtest_dates[-1]}, {len(backtest_dates)} days")

rebalance_freq = CONFIG["rebalance_freq_days"]
rebalance_indices = list(range(0, len(backtest_dates), rebalance_freq))
rebalance_dates = [backtest_dates[i] for i in rebalance_indices]
log(f"  Rebalance dates: {len(rebalance_dates)}")

# ========== Step 4: Fetch Index ==========
log("\nStep 4: Fetching index klines (sh.000001 上证综指) from baostock...")
idx_klines = []
try:
    bs.login()
    rs = bs.query_history_k_data_plus(
        "sh.000001",
        'date,close',
        start_date='2021-01-01', end_date='2024-07-31',
        frequency='d', adjustflag='2'
    )
    rows = []
    while (rs.error_code == '0') & rs.next():
        rows.append(rs.get_row_data())
    bs.logout()
    for r in rows:
        try:
            idx_klines.append({
                "date": r[0].replace("-", ""),
                "close": float(r[1]),
            })
        except:
            continue
    log(f"  Index klines: {len(idx_klines)} days ({idx_klines[0]['date']} ~ {idx_klines[-1]['date']})")
except Exception as e:
    idx_klines = []
    log(f"  WARN: Index fetch failed ({e}), market filter disabled")

def get_kline_series(klines, target_date, lookback=120):
    idx = None
    for i, k in enumerate(klines):
        if k["date"] == target_date:
            idx = i
            break
    if idx is None:
        return None
    start = max(0, idx - lookback + 1)
    return klines[start:idx + 1]

def is_bear_market_at(date_str, lookback=25):
    if not idx_klines:
        return False
    hist = get_kline_series(idx_klines, date_str, lookback)
    if hist is None or len(hist) < 22:
        return False
    closes = [d["close"] for d in hist]
    ma20 = sum(closes[-20:]) / 20
    return closes[-1] < ma20

# ========== Step 5: Run PICK3 simulation ==========
log("\nStep 5: Running PICK3 dual_ma_gc backtest...")
log(f"  Rebalance every {rebalance_freq} days, buy top {CONFIG['pick_count']}")

all_trades = []
open_positions = []
date_to_idx = {d: i for i, d in enumerate(backtest_dates)}
skipped_rebalances = 0
total_buys = 0

def score_golden_cross(code, klines, target_date, s_info):
    """Score a stock using dual_ma_gc logic: MA10 > MA30 + recent crossover."""
    hist = get_kline_series(klines, target_date, lookback=150)
    if hist is None or len(hist) < CONFIG["ma_long"] + 5:
        return None

    closes = [d["close"] for d in hist]
    close = closes[-1]
    if close <= 0:
        return None

    # Calculate MA10 and MA30
    ma_short = sum(closes[-CONFIG["ma_short"]:]) / CONFIG["ma_short"]
    ma_long = sum(closes[-CONFIG["ma_long"]:]) / CONFIG["ma_long"]

    if ma_short <= 0 or ma_long <= 0:
        return None

    # Condition: MA10 > MA30 (golden cross state)
    if not (ma_short > ma_long):
        return None

    # Recent crossover check: was MA10 <= MA30 in the last N days?
    gc_lookback = CONFIG["gc_lookback_days"]
    n = len(closes)
    gc_found = False
    gc_day = 0
    for lookback in range(1, min(gc_lookback + 1, n - CONFIG["ma_long"])):
        end_idx = n - lookback
        if end_idx < CONFIG["ma_long"]:
            break
        prev_ma_s = sum(closes[end_idx - CONFIG["ma_short"]:end_idx]) / CONFIG["ma_short"]
        prev_ma_l = sum(closes[end_idx - CONFIG["ma_long"]:end_idx]) / CONFIG["ma_long"]
        if prev_ma_s <= prev_ma_l:
            gc_found = True
            gc_day = lookback
            break

    if not gc_found:
        return None

    # Volume check (at least trading)
    volumes = [d.get("volume", 0) for d in hist]
    if len(volumes) < 20 or volumes[-1] <= 0:
        return None

    # Quality scoring
    score = 0.0

    # 1. MA alignment strength (30%)
    if ma_long > 0:
        ma_align = (ma_short - ma_long) / ma_long
        ma_score = min(max(ma_align * 5, 0), 1.0)
        score += 0.30 * ma_score

    # 2. Volume surge (20%)
    avg_vol = sum(volumes[-19:-1]) / 18 if sum(volumes[-19:-1]) > 0 else 1
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
    vol_score = min(vol_ratio / 3.0, 1.0)
    score += 0.20 * vol_score

    # 3. Momentum from klines (20%)
    if len(closes) >= 2:
        mom_pct = (closes[-1] - closes[-2]) / closes[-2] * 100
        mom_score = min(max(mom_pct, -10), 10) / 10.0
    else:
        mom_score = 0
    score += 0.20 * mom_score

    # 4. Small cap bonus (15%)
    mv = s_info.get("mv", 0)
    mv_norm = 1.0 - (mv / CONFIG["max_mv"]) if CONFIG["max_mv"] > 0 else 0.5
    score += 0.15 * max(0, mv_norm)

    # 5. Golden cross freshness (15%)
    recency_score = 1.0 - (gc_day - 1) / max(gc_lookback, 1)
    score += 0.15 * max(0, recency_score)

    return {
        "code": code,
        "name": s_info.get("name", ""),
        "close": close,
        "mv": mv,
        f"ma{CONFIG['ma_short']}": round(ma_short, 2),
        f"ma{CONFIG['ma_long']}": round(ma_long, 2),
        "score": round(score, 4),
        "vol_ratio": round(vol_ratio, 2),
        "gc_days_ago": gc_day,
    }

for ri, candidate_date in enumerate(rebalance_dates):
    # Market filter
    if is_bear_market_at(candidate_date):
        log(f"  [SKIP] {candidate_date}: bear market (上证<MA20)")
        skipped_rebalances += 1
        continue

    # Generate candidates
    candidates = []
    for s in universe:
        code = s["code"]
        klines = klines_data.get(code)
        if not klines:
            continue
        scored = score_golden_cross(code, klines, candidate_date, md_map.get(code, {}))
        if scored:
            candidates.append(scored)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top_candidates = candidates[:CONFIG["top_n"]]

    if not top_candidates:
        log(f"  [NO CANDIDATES] {candidate_date}")
        continue

    # Find next trading day
    cd_idx = date_to_idx.get(candidate_date)
    if cd_idx is None or cd_idx + 1 >= len(backtest_dates):
        continue
    buy_date = backtest_dates[cd_idx + 1]

    # Pick top 3
    buys = top_candidates[:CONFIG["pick_count"]]
    log(f"  [BUY+1] {buy_date}: {len(buys)} picks (pool={len(top_candidates)})")

    for pick in buys:
        code = pick["code"]
        klines = klines_data.get(code)
        if not klines:
            continue
        hist = get_kline_series(klines, buy_date, lookback=5)
        if hist is None or len(hist) < 1:
            continue
        actual_close = hist[-1]["close"]
        if actual_close <= 0:
            continue

        pos = Position(
            code=code, buy_date=buy_date,
            buy_price=actual_close,
            name=pick.get("name", ""),
            mv=pick.get("mv", 0),
        )
        open_positions.append(pos)
        total_buys += 1

    # Daily sell checks (A股 T+1)
    sell_ma_s = CONFIG["sell_ma_short"]
    sell_ma_l = CONFIG["sell_ma_long"]
    buy_idx = date_to_idx.get(buy_date, 0)
    for di in range(buy_idx + 1, len(backtest_dates)):
        current_date = backtest_dates[di]
        still_open = []
        for pos in open_positions:
            if pos.is_closed:
                all_trades.extend(pos.closed_trades)
                continue
            klines = klines_data.get(pos.code)
            if not klines:
                still_open.append(pos)
                continue

            hist = get_kline_series(klines, current_date, lookback=60)
            if hist is None or len(hist) < 25:
                still_open.append(pos)
                continue

            closes = [d["close"] for d in hist]
            close = closes[-1]
            ma5 = calc_ma(closes, sell_ma_s)
            ma18 = calc_ma(closes, sell_ma_l)

            if ma5 <= 0 or ma18 <= 0:
                still_open.append(pos)
                continue

            buy_price = pos.buy_price

            # Same sell rules
            if close <= buy_price * CONFIG["stoploss_pct"]:
                pos.sell_all(current_date, close, "SL_94pct")
            elif close < ma18:
                pos.sell_all(current_date, close, "MA18_below")
            elif pos.units >= 2:
                if close > ma5 * CONFIG["take_profit_mult"]:
                    pos.sell_half(current_date, close, "TP_MA5x1.3")
                elif close < ma5:
                    pos.sell_half(current_date, close, "SL_below_MA5")

            if not pos.is_closed:
                still_open.append(pos)
            else:
                all_trades.extend(pos.closed_trades)
        open_positions = still_open

# Close remaining
last_date = backtest_dates[-1]
for pos in open_positions:
    if pos.is_closed:
        all_trades.extend(pos.closed_trades)
        continue
    klines = klines_data.get(pos.code)
    if klines:
        last_close = klines[-1]["close"]
        pos.sell_all(last_date, last_close, "end_of_backtest")
    else:
        pos.sell_all(last_date, pos.buy_price, "no_data")
    all_trades.extend(pos.closed_trades)

# ========== Step 6: Compute results ==========
log("\nStep 6: Computing results...")

result = BacktestResult(CONFIG)
result.trades = all_trades
result.compute()

if not hasattr(result, 'sharpe_ratio'):
    if result.portfolio_returns and len(result.portfolio_returns) > 1:
        avg_r = sum(result.portfolio_returns) / len(result.portfolio_returns)
        std_r = statistics.stdev(result.portfolio_returns) if len(result.portfolio_returns) > 1 else 0
        if std_r > 1e-10:
            result.sharpe_ratio = (avg_r / std_r) * (252.0 / CONFIG["rebalance_freq_days"]) ** 0.5
        else:
            result.sharpe_ratio = 0.0
    else:
        result.sharpe_ratio = 0.0

label = "双均线金叉 PICK3 (熊市回测)"
print("\n" + "═" * 55)
print(f"  {label}")
print(f"  期间: {CONFIG['backtest_start']} → {CONFIG['backtest_end']}")
print(f"  股池: {CONFIG['universe_size']}只, {fetched}只可用K线")
print("═" * 55)
print(result.summary())
print(f"  Total buys:   {total_buys}")
print(f"  Skipped bear: {skipped_rebalances}/{len(rebalance_dates)}")

# Detail
reason_stats = defaultdict(lambda: {"count": 0, "total_return": 0.0, "wins": 0})
for t in all_trades:
    r = t.reason
    reason_stats[r]["count"] += 1
    reason_stats[r]["total_return"] += t.return_pct
    if t.return_pct > 0:
        reason_stats[r]["wins"] += 1

print("\n  ── 卖出原因分析 ──")
print(f"  {'原因':<18} {'次数':>4} {'占比':>5} {'平均收益':>9} {'胜率':>5}")
print(f"  {'-'*44}")
for reason, s in sorted(reason_stats.items(), key=lambda x: x[1]["count"], reverse=True):
    wr = s["wins"]/s["count"]*100
    avg = s["total_return"]/s["count"]
    print(f"  {reason:<18} {s['count']:>4} {s['count']/len(all_trades)*100:>4.1f}% {avg:>+7.2f}% {wr:>4.1f}%")

winners = [t for t in all_trades if t.return_pct > 0]
losers = [t for t in all_trades if t.return_pct <= 0]

if winners:
    win_rets = sorted([t.return_pct for t in winners])
    print(f"\n  盈利笔均: {sum(t.return_pct for t in winners)/len(winners):+.2f}% (中位数 {win_rets[len(win_rets)//2]:+.2f}%)")
    best = max(winners, key=lambda t: t.return_pct)
    print(f"  最佳交易: {best.code} {best.buy_date}→{best.sell_date} {best.return_pct:+.2f}% [{best.reason}]")
if losers:
    loss_rets = sorted([t.return_pct for t in losers])
    print(f"  亏损笔均: {sum(t.return_pct for t in losers)/len(losers):+.2f}% (中位数 {loss_rets[len(loss_rets)//2]:+.2f}%)")
    worst = min(losers, key=lambda t: t.return_pct)
    print(f"  最差交易: {worst.code} {worst.buy_date}→{worst.sell_date} {worst.return_pct:+.2f}% [{worst.reason}]")

# Save
output_path = os.path.join(PROJECT_DIR, "backtest_results", "dual_ma_gc_pick3_bear_market.json")
os.makedirs(os.path.dirname(output_path), exist_ok=True)
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
print(f"\n  结果已保存: {output_path}")
