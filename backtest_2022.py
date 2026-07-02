#!/usr/bin/env python3
"""
2022年 momentum_ma 策略回测（并发加速版）
"""
import baostock as bs
import pandas as pd
import numpy as np
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

COST = 0.001
DATA_DIR = '/home/super-user/screening/backtest_results'
os.makedirs(DATA_DIR, exist_ok=True)

def log(msg): print(msg, file=sys.stderr, flush=True)

# ========== 1. Get stock list ==========
log("Step 1: Getting stock list...")
bs.login()
rs = bs.query_all_stock(day='2022-12-30')
all_stocks = []
while (rs.error_code == '0') & rs.next():
    row = rs.get_row_data()
    code, status, name = row[0], row[1], row[2]
    if code.startswith(('sh.6', 'sh.9', 'sz.0', 'sz.3')) and status == '1':
        if 'ST' not in name and '退' not in name:
            all_stocks.append((code, name))
bs.logout()
log(f"Total A-share stocks: {len(all_stocks)}")

# ========== 2. Fetch klines concurrently ==========
log("Step 2: Fetching 2022 kline data...")

def fetch_one(code_name):
    code, name = code_name
    try:
        bs.login()
        rs = bs.query_history_k_data_plus(
            code, 'date,open,high,low,close,volume,amount',
            start_date='2022-01-01', end_date='2022-12-31',
            frequency='d', adjustflag='2'
        )
        rows = []
        while (rs.error_code == '0') & rs.next():
            row = rs.get_row_data()
            try:
                rows.append({
                    'date': row[0], 'open': float(row[1]), 'high': float(row[2]),
                    'low': float(row[3]), 'close': float(row[4]),
                    'volume': float(row[5]) if row[5] else 0,
                    'amount': float(row[6]) if row[6] else 0,
                })
            except: pass
        bs.logout()
        if len(rows) >= 200:  # 全年大部分交易日都有数据
            return (code, name, rows)
    except:
        try: bs.logout()
        except: pass
    return None

# Use 16 threads for parallel fetching
all_data = {}
fetched = 0
with ThreadPoolExecutor(max_workers=16) as executor:
    futures = {executor.submit(fetch_one, s): s for s in all_stocks}
    for i, future in enumerate(as_completed(futures)):
        result = future.result()
        if result:
            code, name, rows = result
            all_data[code] = {'name': name, 'klines': rows}
            fetched += 1
        if (i + 1) % 200 == 0:
            log(f"  Progress: {i+1}/{len(all_stocks)}, fetched: {fetched}")

log(f"Fetch complete: {fetched} stocks")

# ========== 3. Run backtest ==========
log("\nStep 3: Building data structures...")

# Get all trading dates
all_dates = set()
for data in all_data.values():
    for k in data['klines']:
        all_dates.add(k['date'])
all_dates = sorted(all_dates)
log(f"Trading days: {len(all_dates)}")

# Build numpy arrays
date_index = {d: i for i, d in enumerate(all_dates)}
n_dates = len(all_dates)
stock_close = {}
stock_vol = {}
stock_name = {}

for code, data in all_data.items():
    ca = np.full(n_dates, np.nan)
    va = np.full(n_dates, np.nan)
    for k in data['klines']:
        pos = date_index.get(k['date'])
        if pos is not None:
            ca[pos] = k['close']
            va[pos] = k['volume']
    stock_close[code] = ca
    stock_vol[code] = va
    stock_name[code] = data['name']

del all_data  # free memory

# ========== 4. Simulate ==========
log("Step 4: Running simulation...")

rebalance_interval = 10
hold_days = 10
top_n = 30
min_vol_ratio = 1.5
start_idx = 25

rebalance_indices = list(range(start_idx, n_dates - hold_days, rebalance_interval))
trades = []

for ri, rebal_idx in enumerate(rebalance_indices):
    rebal_date = all_dates[rebal_idx]
    
    # Quick count of valid stocks
    valid = 0
    for code in stock_close:
        if not np.isnan(stock_close[code][rebal_idx]):
            valid += 1
    if valid < 50: continue
    
    candidates = []
    for code in stock_close:
        ca, va = stock_close[code], stock_vol[code]
        if rebal_idx < 20: continue
        if np.isnan(ca[rebal_idx]): continue
        if np.any(np.isnan(ca[rebal_idx-20:rebal_idx+1])): continue
        
        close = ca[rebal_idx]
        ma5 = np.nanmean(ca[rebal_idx-4:rebal_idx+1])
        ma20 = np.nanmean(ca[rebal_idx-19:rebal_idx+1])
        
        if ma5 <= ma20: continue
        if close > ma20 * 1.10: continue
        if np.isnan(va[rebal_idx]): continue
        
        recent_vol = va[rebal_idx]
        avg_vol = np.nanmean(va[max(0,rebal_idx-20):rebal_idx])
        if avg_vol <= 0 or recent_vol / avg_vol < min_vol_ratio: continue
        
        change_pct = (close - ca[rebal_idx-1]) / ca[rebal_idx-1] * 100 if not np.isnan(ca[rebal_idx-1]) else 0
        
        score = 0.25 * min(max(change_pct / 10, 0), 1.0)
        ma_align = (ma5 - ma20) / ma20
        score += 0.25 * min(max(ma_align * 5, 0), 1.0)
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1
        score += 0.15 * min(vol_ratio / 3.0, 1.0)
        
        r_high = np.nanmax(ca[rebal_idx-19:rebal_idx+1])
        r_low = np.nanmin(ca[rebal_idx-19:rebal_idx+1])
        pr = r_high - r_low
        ps = max(0, min(1.0, 1.0 - abs((close-r_low)/pr - 0.4) * 1.5)) if pr > 0 else 0.5
        score += 0.15 * ps
        
        candidates.append({'code': code, 'name': stock_name[code], 'price': close, 'score': score})
    
    candidates.sort(key=lambda x: x['score'], reverse=True)
    picks = candidates[:top_n]
    
    sell_idx = min(rebal_idx + hold_days, n_dates - 1)
    sell_date = all_dates[sell_idx]
    
    for p in picks:
        sp = stock_close[p['code']][sell_idx]
        if np.isnan(sp): continue
        raw_ret = (sp - p['price']) / p['price']
        net_ret = raw_ret - COST * 2
        trades.append({
            'code': p['code'].split('.')[-1], 'name': p['name'],
            'buy_date': rebal_date, 'sell_date': sell_date,
            'buy_price': round(p['price'], 2), 'sell_price': round(sp, 2),
            'raw_return_pct': round(raw_ret*100, 2),
            'net_return_pct': round(net_ret*100, 2),
            'score': round(p['score'], 4), 'is_win': net_ret > 0,
        })
    
    if (ri+1) % 5 == 0:
        log(f"  Rebalance {ri+1}/{len(rebalance_indices)} ({rebal_date}): {len(picks)} picks, {len(trades)} trades total")

# ========== 5. Results ==========
log(f"\nTotal trades: {len(trades)}")
if not trades:
    log("No trades generated!")
    exit()

df = pd.DataFrame(trades)
wins = df['is_win'].sum()
losses = len(df) - wins
win_rate = wins / len(df) * 100
avg_net = df['net_return_pct'].mean()
total_net = df['net_return_pct'].sum()

# Max drawdown
df['cum'] = (1 + df['net_return_pct']/100).cumprod()
peak = np.maximum.accumulate(df['cum'].values)
dd = (df['cum'].values - peak) / peak
max_dd = abs(min(dd)) * 100

# Sharpe
if len(df) > 1:
    sr = np.sqrt(250/hold_days) * df['net_return_pct'].mean()/100 / (df['net_return_pct'].std()/100) if df['net_return_pct'].std() > 0 else 0
else:
    sr = 0

# Best/worst
best = df.loc[df['net_return_pct'].idxmax()]
worst = df.loc[df['net_return_pct'].idxmin()]

print("\n" + "=" * 55)
print("   momentum_ma 2022 年回测结果")
print("=" * 55)
print(f"  回测期间:     2022全年（{all_dates[0]} → {all_dates[-1]}）")
print(f"  候选股票池:   {len(stock_close)} 只")
print(f"  调仓频率:     每 {rebalance_interval} 交易日")
print(f"  持有周期:     {hold_days} 交易日")
print(f"  每批选股:     top {top_n}")
print(f"  交易成本:     千分之一（买卖各一次）")
print(f"  量能条件:     ≥ {min_vol_ratio}× 20日均量")
print("-" * 55)
print(f"  总交易笔数:   {len(trades)}")
print(f"  调仓次数:     {len(rebalance_indices)}")
print(f"  盈利笔数:     {wins}")
print(f"  亏损笔数:     {losses}")
print(f"  胜率:         {win_rate:.1f}%")
print(f"  平均单笔净收益:  {avg_net:.2f}%")
print(f"  累计净收益:      {total_net:.2f}%")
print(f"  最大回撤:     {max_dd:.2f}%")
print(f"  夏普比率:     {sr:.2f}")
print("-" * 55)
print(f"\n最佳交易: {best['name']}({best['code']})  {best['net_return_pct']:+.2f}%")
print(f"最差交易: {worst['name']}({worst['code']})  {worst['net_return_pct']:+.2f}%")

# Save
with open(os.path.join(DATA_DIR, 'backtest_2022.json'), 'w') as f:
    json.dump({
        'period': f"{all_dates[0]} to {all_dates[-1]}",
        'total_trades': len(trades), 'rebalances': len(rebalance_indices),
        'win_rate': round(win_rate,1), 'avg_net_return': round(avg_net,2),
        'total_net_return': round(total_net,2), 'max_drawdown': round(max_dd,2),
        'sharpe': round(sr,2),
        'best': {'name': best['name'], 'code': best['code'], 'return': round(best['net_return_pct'],2)},
        'worst': {'name': worst['name'], 'code': worst['code'], 'return': round(worst['net_return_pct'],2)},
        'trades': trades,
    }, f, ensure_ascii=False, indent=2)
log(f"Saved to {DATA_DIR}/backtest_2022.json")
