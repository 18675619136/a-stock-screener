#!/usr/bin/env python3
"""
熊市回测：动量因子 PICK3（评分Top3）在 2022-01 ~ 2024-07 的熊市环境表现。

使用 AKShare 获取长周期历史K线数据（超越腾讯API的640天限制）。

策略逻辑与 momentum_ma v2 一致：
  - 买入: CLOSE > MA5 > MA18 + 放量(×1.2) + MV<1000亿 + 总股本0.5~10亿
  - 排序: 动量30% + MA强度20% + 放量10% + 小市值15% + 位置5%
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

# Reuse existing helpers
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
    "volume_surge_ratio": 1.2,
    "ma_short": 5,
    "ma_long": 18,
    "top_n": 15,
    "pick_count": 3,
    "pick_mode": "score",
    "stoploss_pct": 0.94,
    "take_profit_mult": 1.3,
}

ST_NAME_PREFIXES = ("ST", "*ST", "S")

def is_st(name):
    return name.startswith(ST_NAME_PREFIXES) or "退" in name

# ========== Step 1: Fetch universe (current, as proxy) ==========
log("=" * 60)
log("熊市回测 (2022-01 ~ 2024-07)：动量因子 PICK3")
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

# ========== Step 2: Fetch klines from AKShare ==========
log("\nStep 2: Fetching klines from AKShare (2022-01-01 ~ 2024-07-31)...")
import akshare as ak

# Build code mapping: AKShare uses code without prefix
def to_sina_code(raw_code):
    """Convert internal code (like '000858') to AKShare format (like '000858') - no prefix needed"""
    return raw_code

klines_data = {}
codes_to_fetch = [s["code"] for s in universe]
total = len(codes_to_fetch)

# Build code → baostock prefix map
def to_bs_code(code):
    if code.startswith(("6", "9", "68")):
        return f"sh.{code}"
    elif code.startswith(("0", "3")):
        return f"sz.{code}"
    elif code.startswith(("92", "8", "4")):
        return f"bj.{code}"
    return None

log(f"  Fetching {total} stocks via baostock (single session, sequential)...")

# Login once and query all stocks sequentially
bs.login()
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
        if len(rows) >= 100:
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

# Filter backtest period
backtest_dates = [d for d in all_dates if "20220101" <= d <= "20240731"]
log(f"  Backtest period: {backtest_dates[0]} ~ {backtest_dates[-1]}, {len(backtest_dates)} days")

rebalance_freq = CONFIG["rebalance_freq_days"]
rebalance_indices = list(range(0, len(backtest_dates), rebalance_freq))
rebalance_dates = [backtest_dates[i] for i in rebalance_indices]
log(f"  Rebalance dates: {len(rebalance_dates)}")

# ========== Step 4: Fetch CSI Index from baostock ==========
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
    """Same as backtest.py but dates are YYYYMMDD strings"""
    idx = None
    for i, k in enumerate(klines):
        if k["date"] == target_date:
            idx = i
            break
    if idx is None:
        return None
    start = max(0, idx - lookback + 1)
    return klines[start:idx + 1]

def get_index_value_at(idx_klines, target_date):
    """Get CSI close at or before target_date"""
    if not idx_klines:
        return None
    for k in reversed(idx_klines):
        if k["date"] <= target_date:
            return k["close"]
    return None

def is_bear_market_at(date_str, lookback=25):
    """Check if CSI all-share close < MA20 at given date"""
    if not idx_klines:
        return False
    hist = get_kline_series(idx_klines, date_str, lookback)
    if hist is None or len(hist) < 22:
        return False
    closes = [d["close"] for d in hist]
    ma20 = sum(closes[-20:]) / 20
    return closes[-1] < ma20

# ========== Step 5: Run PICK3 simulation ==========
log("\nStep 5: Running PICK3 backtest simulation...")
log(f"  Rebalance every {rebalance_freq} days, buy top {CONFIG['pick_count']}")

all_trades = []
open_positions = []
date_to_idx = {d: i for i, d in enumerate(backtest_dates)}
skipped_rebalances = 0
total_buys = 0

def score_stock(code, klines, target_date, s_info):
    """Score a stock using momentum factors (no sector factor)."""
    hist = get_kline_series(klines, target_date, lookback=120)
    if hist is None or len(hist) < 25:
        return None
    
    closes = [d["close"] for d in hist]
    volumes = [d["volume"] for d in hist]
    close = closes[-1]
    
    if close <= 0:
        return None
    
    ma5 = calc_ma(closes, 5)
    ma18 = calc_ma(closes, 18)
    
    if ma5 <= 0 or ma18 <= 0:
        return None
    if not (close > ma5 > ma18):
        return None
    
    # Volume filter
    avg_vol_18 = sum(volumes[-18:]) / 18 if len(volumes) >= 18 else sum(volumes)/len(volumes)
    if avg_vol_18 <= 0:
        return None
    vol_ratio = volumes[-1] / avg_vol_18
    if vol_ratio < CONFIG["volume_surge_ratio"]:
        return None
    
    # Scoring factors
    mom = (close - max(closes[-6:-1])) / max(closes[-6:-1]) * 100  # 5-day momentum
    ma_strength = (ma5 - ma18) / ma18 * 100  # MA divergence
    vol_factor = min(vol_ratio / 3.0, 1.0)  # Volume surge, capped
    mv = s_info.get("mv", 0)
    size_score = max(0, 1 - mv / 1000) if mv > 0 else 0
    price_pos = (close - ma18) / (ma5 - ma18) / 3 if ma5 != ma18 else 0.5  # Position b/w MA18 and MA5
    price_pos = max(0, min(1, price_pos))
    
    # Composite score (same weights as momentum_ma v3, minus sector)
    score = (mom * 0.30 + ma_strength * 0.20 + vol_factor * 0.10 +
             size_score * 0.15 + price_pos * 0.05)
    
    return {
        "code": code,
        "name": s_info.get("name", ""),
        "close": close,
        "mv": mv,
        "ma5": ma5,
        "ma18": ma18,
        "score": round(score, 4),
        "mom": round(mom, 2),
        "vol_ratio": round(vol_ratio, 2),
    }

for ri, candidate_date in enumerate(rebalance_dates):
    # Market filter: skip if bear market
    if is_bear_market_at(candidate_date):
        log(f"  [SKIP] {candidate_date}: bear market (CSI<MA20)")
        skipped_rebalances += 1
        continue
    
    # Generate candidates
    candidates = []
    for s in universe:
        code = s["code"]
        klines = klines_data.get(code)
        if not klines:
            continue
        scored = score_stock(code, klines, candidate_date, md_map.get(code, {}))
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
        log(f"  [SKIP] {candidate_date}: no next trading day")
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
            
            # Sell check (same logic as backtest.py)
            hist = get_kline_series(klines, current_date, lookback=60)
            if hist is None or len(hist) < 25:
                still_open.append(pos)
                continue
            
            closes = [d["close"] for d in hist]
            close = closes[-1]
            ma5 = calc_ma(closes, CONFIG["ma_short"])
            ma18 = calc_ma(closes, CONFIG["ma_long"])
            
            if ma5 <= 0 or ma18 <= 0:
                still_open.append(pos)
                continue
            
            buy_price = pos.buy_price
            closed_now = False
            
            # Priority 1: Price < 94% buy price → sell all
            if close <= buy_price * CONFIG["stoploss_pct"]:
                pos.sell_all(current_date, close, "SL_94pct")
                closed_now = True
            # Priority 2: MA18 breakdown → sell all
            elif close < ma18:
                pos.sell_all(current_date, close, "MA18_below")
                closed_now = True
            # Priority 3: Half sell conditions
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

# Close remaining at end
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

# Add sharpe to result if missing
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

# Print summary
label = "动量因子 PICK3 (熊市回测)"
print("\n" + "═" * 55)
print(f"  {label}")
print(f"  期间: {CONFIG['backtest_start']} → {CONFIG['backtest_end']}")
print(f"  股池: {CONFIG['universe_size']}只, 每{fetched}只可用K线")
print("═" * 55)
print(result.summary())
print(f"  Total buys:   {total_buys}")
print(f"  Skipped bear: {skipped_rebalances}/{len(rebalance_dates)}")

# Detailed analysis
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
output_path = os.path.join(PROJECT_DIR, "backtest_results", "momentum_ma_pick3_bear_market.json")
os.makedirs(os.path.dirname(output_path), exist_ok=True)
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
print(f"\n  结果已保存: {output_path}")
