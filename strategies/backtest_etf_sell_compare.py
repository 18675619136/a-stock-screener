#!/usr/bin/env python3
"""
ETF 策略卖出条件对比回测 — 跑多个变体并输出汇总
"""
import sys, os, json, time, math
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategies.data.fetcher import fetch_url, TENCENT_HEADERS, log

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ETF_LIST = [
    {"code": "513100", "prefix": "sh", "name": "纳指ETF"},
    {"code": "513300", "prefix": "sh", "name": "标普ETF"},
    {"code": "159957", "prefix": "sz", "name": "中证1000ETF"},
    {"code": "159915", "prefix": "sz", "name": "创业板ETF"},
]

INITIAL_CAPITAL = 1_000_000
TRADE_FEE_RATE = 0.0005

def calc_ma(closes, period):
    mas = []
    for i in range(len(closes)):
        if i < period - 1: mas.append(0.0)
        else: mas.append(sum(closes[i-period+1:i+1]) / period)
    return mas

def fetch_etf_klines(sym, max_days=500):
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,500,qfq"
    for attempt in range(3):
        raw = fetch_url(url, headers=TENCENT_HEADERS, timeout=15)
        if not raw or len(raw) < 50: time.sleep(1.5); continue
        try:
            parsed = json.loads(raw); data = parsed.get("data", {})
            target_key = None
            for k in data:
                if sym.replace("/", "") in k: target_key = k; break
            if not target_key: return None
            klines = data[target_key].get("qfqday", data[target_key].get("day", []))
            if not klines or len(klines) < 25: return None
            result = []
            for e in klines:
                if len(e) >= 6:
                    try:
                        result.append({"date": str(e[0]), "open": float(e[1]), "close": float(e[2]),
                                       "high": float(e[3]), "low": float(e[4]), "volume": float(e[5]) or 0})
                    except: continue
            return result[-max_days:]
        except: time.sleep(1.5)
    return None

def run_backtest(etf, klines, capital, sell_params):
    """
    sell_params = {
        'mode': str — one of:
            'ABC'  = Stage1=MA5↓50%, Stage2=price<MA5 50%, Stage3=MA5↓100% (baseline)
            'AC'   = Stage1=MA5↓50%, Stage2=price<MA5 100% (skip Stage3)
            'BC'   = Stage1=price<MA5 50%, Stage2=MA5↓100% (skip Stage1's MA5↓)
            'AB'   = Stage1=MA5↓50%, Stage2=price<MA5 50% (no Stage3, hold remaining)
            'A30'  = Stage1=MA5↓30%, Stage2=price<MA5 70%, Stage3=MA5↓100%
            'AMA18'= Stage1=MA5↓50%, Stage2=price<MA5 50%, Stage3=price<MA18 100%
            'AMA20'= Stage1=MA5↓50%, Stage2=price<MA5 50%, Stage3=price<MA20 100%
            'AMA10'= Stage1=MA5↓50%, Stage2=price<MA5 50%, Stage3=MA10↓100%
            'C'    = Stage1=price<MA5 100% (single exit)
    }
    """
    code = etf["code"]; name = etf["name"]
    closes = [k["close"] for k in klines]
    dates = [k["date"] for k in klines]
    n = len(closes)
    ma5 = calc_ma(closes, 5); ma10 = calc_ma(closes, 10)
    ma18 = calc_ma(closes, 18); ma20 = calc_ma(closes, 20)

    mode = sell_params['mode']
    cash = capital; shares = 0; buy_price = 0.0; buy_date = ""
    stage = 0; trades = []; nav_series = []

    for i in range(max(18, 20), n):
        date = dates[i]; close = closes[i]
        cur_ma5, cur_ma10, cur_ma18, cur_ma20 = ma5[i], ma10[i], ma18[i], ma20[i]
        prev_ma5, prev_ma10, prev_ma18, prev_ma20 = ma5[i-1], ma10[i-1], ma18[i-1], ma20[i-1]

        if cur_ma5 <= 0 or cur_ma18 <= 0: continue

        # ── Buy ──
        if shares == 0:
            if cur_ma5 > cur_ma18 and prev_ma5 <= prev_ma18 and cur_ma20 > prev_ma20:
                price_with_fee = close * (1 + TRADE_FEE_RATE)
                max_shares = int(cash / price_with_fee / 100) * 100
                if max_shares >= 100:
                    cost = round(max_shares * close * (1 + TRADE_FEE_RATE), 2)
                    if cost <= cash:
                        shares = max_shares; cash -= cost; buy_price = close; buy_date = date
                        stage = 0
                        trades.append({"date": date, "type": "BUY", "price": round(close, 3), "shares": shares, "cost": round(cost, 2), "cash_after": round(cash, 2)})
            continue

        if date == buy_date:
            nav_series.append(cash + shares * close)
            continue

        ma5_turning = cur_ma5 < prev_ma5
        price_break_ma5 = close < cur_ma5
        price_break_ma18 = close < cur_ma18
        price_break_ma20 = close < cur_ma20
        ma10_turning = cur_ma10 < prev_ma10

        sold_this_day = False

        if stage == 0:
            if mode == 'C':  # single exit: price < MA5 → 100%
                if price_break_ma5:
                    proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                    cash += proceeds; stage = 3
                    trades.append({"date": date, "type": "SELL_ALL", "price": round(close, 3), "shares_sold": shares, "proceeds": round(proceeds, 2), "cash_after": round(cash, 2), "signal": "price<MA5→100%"})
                    shares = 0; sold_this_day = True

            elif mode == 'BC':  # Stage1=price<MA5 50%, Stage2=MA5↓100%
                if price_break_ma5:
                    sell_shares = int(shares * 0.5 / 100) * 100
                    if sell_shares >= 100:
                        proceeds = round(sell_shares * close * (1 - TRADE_FEE_RATE), 2)
                        shares -= sell_shares; cash += proceeds; stage = 1
                        trades.append({"date": date, "type": "SELL_HALF", "price": round(close, 3), "shares_sold": sell_shares, "proceeds": round(proceeds, 2), "shares_remaining": shares, "cash_after": round(cash, 2), "signal": "BC: price<MA5→50%"})
                    else:
                        proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                        cash += proceeds; stage = 3
                        trades.append({"date": date, "type": "SELL_ALL", "price": round(close, 3), "shares_sold": shares, "proceeds": round(proceeds, 2), "cash_after": round(cash, 2), "signal": "BC: price<MA5不足→清仓"})
                        shares = 0
                    sold_this_day = True

            else:  # modes with MA5↓ as first signal
                if ma5_turning:
                    r1 = sell_params.get('r1', 0.5)
                    sell_shares = int(shares * r1 / 100) * 100
                    if sell_shares >= 100:
                        proceeds = round(sell_shares * close * (1 - TRADE_FEE_RATE), 2)
                        shares -= sell_shares; cash += proceeds; stage = 1
                        trades.append({"date": date, "type": "SELL_HALF", "price": round(close, 3), "shares_sold": sell_shares, "proceeds": round(proceeds, 2), "shares_remaining": shares, "cash_after": round(cash, 2), "signal": f"Stage0→1: MA5↓卖{r1*100:.0f}%"})
                    else:
                        proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                        cash += proceeds; stage = 3
                        trades.append({"date": date, "type": "SELL_ALL", "price": round(close, 3), "shares_sold": shares, "proceeds": round(proceeds, 2), "cash_after": round(cash, 2), "signal": "Stage0→3: MA5↓持仓不足"})
                        shares = 0
                    sold_this_day = True

        elif stage == 1:
            # Stage 1: triggered by something
            if mode == 'AC' or mode == 'A30' or mode == 'AMA18' or mode == 'AMA20' or mode == 'AMA10' or mode == 'ABC':
                # Check price < MA5 for Stage2
                if price_break_ma5:
                    r2 = sell_params.get('r2', 0.5)
                    sell_shares = int(shares * r2 / 100) * 100
                    if sell_shares >= 100:
                        proceeds = round(sell_shares * close * (1 - TRADE_FEE_RATE), 2)
                        shares -= sell_shares; cash += proceeds; stage = 2
                        trades.append({"date": date, "type": "SELL_HALF", "price": round(close, 3), "shares_sold": sell_shares, "proceeds": round(proceeds, 2), "shares_remaining": shares, "cash_after": round(cash, 2), "signal": f"Stage1→2: price<MA5卖{r2*100:.0f}%"})
                    else:
                        proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                        cash += proceeds; stage = 3
                        trades.append({"date": date, "type": "SELL_ALL", "price": round(close, 3), "shares_sold": shares, "proceeds": round(proceeds, 2), "cash_after": round(cash, 2), "signal": "Stage1→3: price<MA5不足"})
                        shares = 0
                    sold_this_day = True

            elif mode == 'BC':
                # Stage2 = MA5↓→100%
                if ma5_turning:
                    proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                    cash += proceeds; stage = 3
                    trades.append({"date": date, "type": "SELL_ALL", "price": round(close, 3), "shares_sold": shares, "proceeds": round(proceeds, 2), "cash_after": round(cash, 2), "signal": "BC Stage1→2: MA5↓→100%"})
                    shares = 0; sold_this_day = True

        elif stage == 2:
            if mode == 'ABC' or mode == 'A30':
                if ma5_turning:
                    proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                    cash += proceeds; stage = 3
                    trades.append({"date": date, "type": "SELL_ALL", "price": round(close, 3), "shares_sold": shares, "proceeds": round(proceeds, 2), "cash_after": round(cash, 2), "signal": "Stage2→3: MA5↓→100%"})
                    shares = 0; sold_this_day = True
            elif mode == 'AC':
                # Already cleared in Stage2
                pass
            elif mode == 'AB':
                # No Stage3 — just hold remaining
                pass
            elif mode == 'AMA18':
                if price_break_ma18:
                    proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                    cash += proceeds; stage = 3
                    trades.append({"date": date, "type": "SELL_ALL", "price": round(close, 3), "shares_sold": shares, "proceeds": round(proceeds, 2), "cash_after": round(cash, 2), "signal": "Stage2→3: price<MA18→100%"})
                    shares = 0; sold_this_day = True
            elif mode == 'AMA20':
                if price_break_ma20:
                    proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                    cash += proceeds; stage = 3
                    trades.append({"date": date, "type": "SELL_ALL", "price": round(close, 3), "shares_sold": shares, "proceeds": round(proceeds, 2), "cash_after": round(cash, 2), "signal": "Stage2→3: price<MA20→100%"})
                    shares = 0; sold_this_day = True
            elif mode == 'AMA10':
                if ma10_turning:
                    proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                    cash += proceeds; stage = 3
                    trades.append({"date": date, "type": "SELL_ALL", "price": round(close, 3), "shares_sold": shares, "proceeds": round(proceeds, 2), "cash_after": round(cash, 2), "signal": "Stage2→3: MA10↓→100%"})
                    shares = 0; sold_this_day = True

        nav_series.append(cash + (shares * close if shares > 0 else 0))

    final_value = cash + (shares * closes[-1] if shares > 0 else 0)
    total_return = (final_value - capital) / capital * 100

    # Win rate
    buy_trades = [t for t in trades if t["type"] == "BUY"]
    win = 0; loss = 0; total_pnl = 0.0
    for i, t in enumerate(trades):
        if t["type"] == "BUY":
            buy_cost = t["cost"]
            sell_total = 0.0
            j = i + 1
            while j < len(trades) and trades[j]["type"] != "BUY":
                if trades[j]["type"].startswith("SELL"):
                    sell_total += trades[j]["proceeds"]
                j += 1
            pnl = sell_total - buy_cost; total_pnl += pnl
            if pnl > 0: win += 1
            else: loss += 1
    win_rate = win / max(win + loss, 1) * 100

    # Max drawdown
    peak = nav_series[0] if nav_series else capital
    max_dd = 0.0
    for v in nav_series:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    return {
        "etf": f"{name}({code})",
        "return_pct": round(total_return, 2),
        "win_rate_pct": round(win_rate, 1),
        "trades_count": len(trades),
        "buy_count": len(buy_trades),
        "max_dd_pct": round(max_dd, 2),
        "final_value": round(final_value, 2),
    }

VARIANTS = {
    "📊 Baseline": {"mode": "ABC", "r1": 0.5, "r2": 0.5},   # Stage1=MA5↓50%, Stage2=price<MA5 50%, Stage3=MA5↓100%
    "🗑️ 删Stage1": {"mode": "BC", "r1": 0, "r2": 0},       # Skip MA5↓, go price<MA5→50% then MA5↓→100%
    "🗑️ 删Stage3": {"mode": "AC", "r1": 0.5, "r2": 1.0},    # Stage1=MA5↓50%, Stage2=price<MA5→100%
    "🎯 单次price<MA5": {"mode": "C", "r1": 0, "r2": 0},    # Single exit: price<MA5→100%
    "🔀 30/70比例": {"mode": "A30", "r1": 0.3, "r2": 0.7},  # Stage1=MA5↓30%, Stage2=price<MA5 70%, Stage3=MA5↓100%
    "🛡️ MA18止损": {"mode": "AMA18", "r1": 0.5, "r2": 0.5}, # Stage1=MA5↓50%, Stage2=price<MA5 50%, Stage3=price<MA18→100%
    "🛡️ MA20止损": {"mode": "AMA20", "r1": 0.5, "r2": 0.5}, # Stage1=MA5↓50%, Stage2=price<MA5 50%, Stage3=price<MA20→100%
    "🛡️ MA10掉头": {"mode": "AMA10", "r1": 0.5, "r2": 0.5}, # Stage1=MA5↓50%, Stage2=price<MA5 50%, Stage3=MA10↓→100%
}

def main():
    print("=" * 90)
    print("  ETF 卖出条件优化对比 — MA5金叉MA18 + MA20向上 买入")
    print("=" * 90)
    print(f"\n  初始资金: 每只ETF 100万 | 手续费: 买卖各0.5‰")
    print(f"  数据区间: 2024-07 ~ 2026-07 (约500根K线)")
    print(f"")

    # Fetch klines once
    all_klines = {}
    for etf in ETF_LIST:
        sym = f"{etf['prefix']}{etf['code']}"
        print(f"  📥 获取 {etf['name']}({sym})...", end=" ", flush=True)
        klines = fetch_etf_klines(sym, 500)
        if klines and len(klines) >= 25:
            print(f"✅ {len(klines)}根K线")
            all_klines[etf['code']] = klines
        else:
            print("❌ 跳过")
        time.sleep(0.5)

    if not all_klines:
        print("  ❌ 无有效数据"); return

    # Run all variants
    results = {}  # variant_name -> {etf_code -> result}
    for vname, vparams in VARIANTS.items():
        print(f"\n  ⏳ 运行 {vname}...")
        results[vname] = {}
        for etf in ETF_LIST:
            code = etf['code']
            if code not in all_klines: continue
            r = run_backtest(etf, all_klines[code], INITIAL_CAPITAL, vparams)
            results[vname][code] = r
            print(f"    {etf['name']:10s}: {r['return_pct']:+7.2f}% 胜率{r['win_rate_pct']:5.1f}%  {r['trades_count']:3d}笔 回撤{r['max_dd_pct']:5.2f}%")

    # Summary table
    print("\n" + "=" * 90)
    print("  📊 汇总排名（按总收益排序）")
    print("=" * 90)

    # Compute average return per variant
    avg_returns = {}
    for vname, etfs in results.items():
        returns = [r['return_pct'] for r in etfs.values()]
        avg_returns[vname] = sum(returns) / len(returns) if returns else 0

    # Sort by average return
    ranked = sorted(avg_returns.items(), key=lambda x: x[1], reverse=True)

    # Header
    print(f"\n  {'排名':>3s} {'变体':<20s} {'平均收益':>8s} {'纳指':>7s} {'标普':>7s} {'中证1000':>8s} {'创业板':>7s} {'平均回撤':>8s}")
    print(f"  {'─'*3} {'─'*20} {'─'*8} {'─'*7} {'─'*7} {'─'*8} {'─'*7} {'─'*8}")
    for rank, (vname, avg_ret) in enumerate(ranked, 1):
        etfs = results[vname]
        r1 = etfs.get('513100', {}).get('return_pct', 0)
        r2 = etfs.get('513300', {}).get('return_pct', 0)
        r3 = etfs.get('159957', {}).get('return_pct', 0)
        r4 = etfs.get('159915', {}).get('return_pct', 0)
        dds = [etfs.get(c, {}).get('max_dd_pct', 0) for c in ['513100','513300','159957','159915']]
        avg_dd = sum(dds) / len(dds) if dds else 0
        print(f"  {rank:>3d} {vname:<20s} {avg_ret:>+7.2f}% {r1:>+6.2f}% {r2:>+6.2f}% {r3:>+7.2f}% {r4:>+6.2f}% {avg_dd:>7.2f}%")

    # Best for each ETF
    print(f"\n")
    print(f"  🏆 各ETF最优变体:")
    for etf in ETF_LIST:
        code = etf['code']; name = etf['name']
        best_vname = ""; best_ret = -999
        for vname, etfs in results.items():
            r = etfs.get(code, {}).get('return_pct', -999)
            if r > best_ret:
                best_ret = r; best_vname = vname
        print(f"    {name:10s}: {best_vname}  ({best_ret:+.2f}%)")

if __name__ == "__main__":
    main()
