#!/usr/bin/env python3
"""
ETF 宽基指数回测 — MA5 金叉MA18 + MA18向上 买入 + 两极卖出策略

策略规则:
  买入: MA5 上穿 MA18（金叉）+ MA18 向上 → 全仓买入
  卖出（按顺序执行）:
    Stage 0 → MA5 掉头向下 → 卖出一半
    Stage 1 → MA5 下穿 MA18（死叉）→ 全部卖出

ETF 列表:
  - 513100 (sh) 纳指ETF
  - 513300 (sh) 标普ETF
  - 159957 (sz) 中证1000ETF
  - 159915 (sz) 创业板ETF

用法:
    python3 -m strategies.backtest_etf_ma5_ma18 [--days 500]
"""

import sys
import os
import json
import time
import math
from datetime import datetime, timedelta
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from strategies.data.fetcher import (
    DataFetcher, log, safe_float, fetch_url, TENCENT_HEADERS, code_to_prefix,
)

# ── ETF 配置 ──────────────────────────────────────────────

ETF_LIST = [
    {"code": "513100", "prefix": "sh", "name": "纳指ETF"},
    {"code": "513300", "prefix": "sh", "name": "标普ETF"},
    {"code": "159957", "prefix": "sz", "name": "中证1000ETF"},
    {"code": "159915", "prefix": "sz", "name": "创业板ETF"},
]

INITIAL_CAPITAL = 1_000_000  # 每只ETF 100万初始资金
MA_SHORT = 5
MA_LONG = 18
TRADE_FEE_RATE = 0.0005  # 万五手续费，双向


def calc_ma(closes: list[float], period: int) -> list[float]:
    """计算均线序列"""
    mas = []
    for i in range(len(closes)):
        if i < period - 1:
            mas.append(0.0)
        else:
            mas.append(sum(closes[i - period + 1 : i + 1]) / period)
    return mas


def fetch_etf_klines(sym: str, max_days: int = 500) -> list[dict] | None:
    """获取ETF K线数据（含date字段）"""
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,500,qfq"
    for attempt in range(3):
        raw = fetch_url(url, headers=TENCENT_HEADERS, timeout=15)
        if not raw or len(raw) < 50:
            time.sleep(1.5)
            continue
        try:
            parsed = json.loads(raw)
            data = parsed.get("data", {})
            target_key = None
            for k in data:
                if sym.replace("/", "") in k:
                    target_key = k
                    break
            if not target_key:
                log(f"  [WARN] {sym}: no data key found")
                return None
            klines = data[target_key].get("qfqday", data[target_key].get("day", []))
            if not klines or len(klines) < 25:
                log(f"  [WARN] {sym}: insufficient klines ({len(klines) if klines else 0})")
                return None
            result = []
            for e in klines:
                if len(e) >= 6:
                    try:
                        result.append({
                            "date": str(e[0]),
                            "open": float(e[1]),
                            "close": float(e[2]),
                            "high": float(e[3]),
                            "low": float(e[4]),
                            "volume": float(e[5]) if e[5] else 0,
                        })
                    except (ValueError, IndexError):
                        continue
            return result[-max_days:]
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            log(f"  [WARN] {sym} parse error (attempt {attempt+1}): {e}")
            time.sleep(1.5)
    return None


def run_backtest(
    etf: dict,
    klines: list[dict],
    capital: float = INITIAL_CAPITAL,
) -> dict:
    """对单只ETF执行回测"""
    code = etf["code"]
    name = etf["name"]

    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    dates = [k["date"] for k in klines]
    n = len(closes)

    # 计算均线
    ma5 = calc_ma(closes, 5)
    ma18 = calc_ma(closes, 18)

    # ── 回测状态 ──
    cash = capital
    shares = 0  # 持仓股数
    position_value = 0.0  # 持仓市值
    buy_price = 0.0
    buy_date = ""

    # 状态机
    # stage: 0=满仓, 1=已卖一半, 2=已卖四分之三, 3=已清仓
    stage = 0
    trades = []
    nav_series = []  # 每日净值序列（用于最大回撤计算）
    ma5_turn_count = 0  # MA5掉头向下的次数（用于日志）
    price_break_ma5_count = 0

    for i in range(MA_LONG, n):
        date = dates[i]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        cur_ma5 = ma5[i]
        cur_ma18 = ma18[i]
        prev_ma5 = ma5[i - 1]
        prev_ma18 = ma18[i - 1]

        # 跳过MA数据未准备好
        if cur_ma5 <= 0 or cur_ma18 <= 0:
            continue

        # ── 买入信号: MA5 上穿 MA18 ──
        if shares == 0:
            # 金叉：当前MA5>MA18 且 前一日MA5<=MA18 + MA18向上
            if cur_ma5 > cur_ma18 and prev_ma5 <= prev_ma18 and cur_ma18 > prev_ma18:
                # 计算可买股数（全仓，含手续费）
                price_with_fee = close * (1 + TRADE_FEE_RATE)
                max_shares = int(cash / price_with_fee / 100) * 100
                if max_shares >= 100:
                    cost = round(max_shares * close * (1 + TRADE_FEE_RATE), 2)
                    if cost <= cash:
                        shares = max_shares
                        cash -= cost
                        buy_price = close
                        buy_date = date
                        position_value = shares * close
                        stage = 0
                        ma5_turn_count = 0
                        price_break_ma5_count = 0
                        trades.append({
                            "date": date,
                            "type": "BUY",
                            "price": round(close, 3),
                            "shares": shares,
                            "cost": round(cost, 2),
                            "cash_after": round(cash, 2),
                            "ma5": round(cur_ma5, 3),
                            "ma18": round(cur_ma18, 3),
                            "signal": "MA5金叉MA18+MA18向上",
                        })
                        log(f"  🟢 {date} {name} 买入 {shares}股 @{close:.3f} "
                            f"MA5={cur_ma5:.2f} MA18={cur_ma18:.2f}")
                    else:
                        log(f"  [SKIP] {date} {name} 现金不足: {cash:.2f} < {cost:.2f}")
                else:
                    log(f"  [SKIP] {date} {name} 价格{close:.3f}太高，100股需{close*100:.2f}>现金{cash:.2f}")
            continue  # 无持仓时跳过卖出检查

        # ── 有持仓 → 卖出检查 ──
        # T+1: 当天买入不可卖出
        if date == buy_date:
            # 仍然更新市值
            position_value = shares * close
            continue

        # 判断MA5方向
        ma5_turning_down = cur_ma5 < prev_ma5

        if stage == 0:
            # Stage 0: 满仓 → MA5掉头向下 → 卖一半
            if ma5_turning_down:
                sell_shares = int(shares * 0.5 / 100) * 100
                if sell_shares >= 100 and shares > 0:
                    proceeds = round(sell_shares * close * (1 - TRADE_FEE_RATE), 2)
                    shares -= sell_shares
                    cash += proceeds
                    position_value = shares * close
                    ma5_turn_count += 1
                    stage = 1
                    trades.append({
                        "date": date,
                        "type": "SELL_HALF",
                        "price": round(close, 3),
                        "shares_sold": sell_shares,
                        "proceeds": round(proceeds, 2),
                        "shares_remaining": shares,
                        "cash_after": round(cash, 2),
                        "ma5": round(cur_ma5, 3),
                        "prev_ma5": round(prev_ma5, 3),
                        "signal": "Stage0→1: MA5首次掉头向下",
                    })
                    log(f"  🟡 {date} {name} Stage0→1 MA5掉头向下 卖出{sell_shares}股 "
                        f"@{close:.3f} 剩余{shares}股 现金{cash:.2f}")
                else:
                    # 股数太少不足卖半 → 直接清仓
                    proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                    cash += proceeds
                    stage = 3
                    trades.append({
                        "date": date,
                        "type": "SELL_ALL",
                        "price": round(close, 3),
                        "shares_sold": shares,
                        "proceeds": round(proceeds, 2),
                        "shares_remaining": 0,
                        "cash_after": round(cash, 2),
                        "signal": "Stage0→3: MA5掉头但持仓不足半仓→清仓",
                    })
                    log(f"  🔴 {date} {name} 持仓不足半仓({shares}股)清仓 @{close:.3f} 现金{cash:.2f}")
                    shares = 0
                    position_value = 0

        elif stage == 1:
            # Stage 1: 已卖一半 → MA5下穿MA18（死叉）→ 全部卖出
            death_cross = cur_ma5 < cur_ma18 and prev_ma5 >= prev_ma18
            if death_cross:
                proceeds = round(shares * close * (1 - TRADE_FEE_RATE), 2)
                cash += proceeds
                stage = 3
                trades.append({
                    "date": date,
                    "type": "SELL_ALL",
                    "price": round(close, 3),
                    "shares_sold": shares,
                    "proceeds": round(proceeds, 2),
                    "shares_remaining": 0,
                    "cash_after": round(cash, 2),
                    "ma5": round(cur_ma5, 3),
                    "ma18": round(cur_ma18, 3),
                    "signal": "Stage1→3: MA5死叉MA18清仓",
                })
                log(f"  🔴 {date} {name} Stage1→3 MA5({cur_ma5:.2f})下穿MA18({cur_ma18:.2f}) 清仓@{close:.3f} 现金{cash:.2f}")
                shares = 0
                position_value = 0

        elif stage == 2:
            # Stage 2: 已无（两级卖出）— 不会到达此状态
            pass

        elif stage == 3:
            # 已清仓，不操作
            pass

        # 更新市值
        if shares > 0:
            position_value = shares * close
        
        # 记录每日净值（用于最大回撤）
        nav_series.append(cash + (shares * close if shares > 0 else 0))

    # ── 最终清算 ──
    final_value = cash + (shares * closes[-1] if shares > 0 else 0)
    total_return = (final_value - capital) / capital * 100

    # ── 统计 ──
    buy_trades = [t for t in trades if t["type"] == "BUY"]
    sell_trades = [t for t in trades if t["type"].startswith("SELL")]

    # 计算每笔交易的盈亏
    trade_pnl = []
    for t in trades:
        if t["type"] == "BUY":
            trade_pnl.append(t)

    # 胜率：按买入-卖出配对计算
    win_count = 0
    loss_count = 0
    total_pnl_amount = 0.0
    for i in range(len(trades)):
        if trades[i]["type"] == "BUY":
            buy_cost = trades[i]["cost"]
            # 找出后面一系列卖出直到下一次买入
            sell_total = 0.0
            shares_sold_total = 0
            j = i + 1
            while j < len(trades) and trades[j]["type"] != "BUY":
                if trades[j]["type"].startswith("SELL"):
                    sell_total += trades[j]["proceeds"]
                    shares_sold_total += trades[j]["shares_sold"]
                j += 1
            pnl = sell_total - buy_cost
            total_pnl_amount += pnl
            if pnl > 0:
                win_count += 1
            else:
                loss_count += 1

    win_rate = win_count / max(win_count + loss_count, 1) * 100

    # 最大回撤（从 nav_series 计算）
    peak = nav_series[0] if nav_series else capital
    max_drawdown = 0.0
    for v in nav_series:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_drawdown:
            max_drawdown = dd

    return {
        "etf": f"{name}({code})",
        "initial_capital": capital,
        "final_value": round(final_value, 2),
        "total_return_pct": round(total_return, 2),
        "total_trades": len(trades),
        "buy_count": len(buy_trades),
        "sell_count": len(sell_trades),
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl": round(total_pnl_amount, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "trades": trades,
        "date_range": f"{dates[MA_LONG]} ~ {dates[-1]}" if n > MA_LONG else "N/A",
        "days_traded": n - MA_LONG,
    }


def print_report(results: list[dict]):
    """打印回测报告"""
    print("\n" + "=" * 72)
    print("  ETF 宽基指数回测报告 — MA5金叉MA18 + MA18向上 两极卖出")
    print("=" * 72)
    print(f"\n  策略规则:")
    print(f"    🟢 买入: MA5 上穿 MA18（金叉）+ MA18 向上 → 全仓买入")
    print(f"    🟡 Stage1: MA5 掉头向下 → 卖出一半")
    print(f"    🔴 Stage2: MA5 下穿 MA18（死叉）→ 全部卖出")
    print(f"\n  初始资金: 每只ETF {INITIAL_CAPITAL/10000:.0f}万元")
    print(f"  手续费: 买卖各 {TRADE_FEE_RATE*1000:.1f}‰")
    print(f"")

    for r in results:
        print(f"  ── {r['etf']} ──")
        print(f"    回测区间: {r['date_range']}")
        print(f"    交易日数: {r['days_traded']}天")
        print(f"    初始资金: {r['initial_capital']:>10,.0f}元")
        print(f"    最终价值: {r['final_value']:>10,.0f}元")
        print(f"    总收益率: {r['total_return_pct']:>+7.2f}%")
        print(f"    总盈亏:   {r['total_pnl']:>+10,.2f}元")
        print(f"    交易次数: {r['total_trades']}次 (买入{r['buy_count']}次/卖出{r['sell_count']}次)")
        print(f"    胜率:     {r['win_rate_pct']}% ({r['win_count']}胜/{r['loss_count']}负)")
        print(f"    最大回撤: {r['max_drawdown_pct']:.2f}%")

        # 打印最近5笔交易
        if r["trades"]:
            print(f"")
            print(f"    最近交易记录:")
            for t in r["trades"][-8:]:
                emoji = "🟢" if t["type"] == "BUY" else "🔴"
                signal_short = t.get("signal", "")[:30]
                if t["type"] == "BUY":
                    print(f"      {emoji} {t['date']} {t['type']:>10s} "
                          f"{t['shares']:>5d}股 @{t['price']:<8.3f} "
                          f"花费{t['cost']:<10.2f} | {signal_short}")
                else:
                    print(f"      {emoji} {t['date']} {t['type']:>10s} "
                          f"卖出{t['shares_sold']:>4d}股 @{t['price']:<8.3f} "
                          f"到账{t['proceeds']:<10.2f} | {signal_short}")
        print("")

    # 汇总
    print(f"  ── 汇总对比 ──")
    print(f"  {'ETF':<20s} {'收益率':>8s} {'胜率':>6s} {'交易次数':>8s} {'最大回撤':>8s}")
    print(f"  {'─'*20} {'─'*8} {'─'*6} {'─'*8} {'─'*8}")
    for r in results:
        print(f"  {r['etf']:<20s} {r['total_return_pct']:>+7.2f}% "
              f"{r['win_rate_pct']:>5.1f}% {r['total_trades']:>4d}次 "
              f"{r['max_drawdown_pct']:>6.2f}%")
    print(f"")
    print(f"  🔴 正收益率 = 上涨（中国市场配色）")
    print(f"  🟢 负收益率 = 下跌")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ETF MA5/18 金叉策略回测")
    parser.add_argument("--days", type=int, default=500, help="最大K线天数")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL, help="每只ETF初始资金")
    args = parser.parse_args()

    results = []
    for etf in ETF_LIST:
        sym = f"{etf['prefix']}{etf['code']}"
        name = etf["name"]
        print(f"\n📥 获取 {name}({sym}) K线数据...", file=sys.stderr)
        klines = fetch_etf_klines(sym, max_days=args.days)
        if klines is None or len(klines) < 25:
            print(f"  ❌ {name}({sym}) K线数据不足，跳过", file=sys.stderr)
            continue

        print(f"  ✅ 获取到 {len(klines)} 根K线 ({klines[0]['date']} ~ {klines[-1]['date']})", file=sys.stderr)
        time.sleep(0.5)  # API限流

        result = run_backtest(etf, klines, capital=args.capital)
        results.append(result)

    if results:
        print_report(results)

        # 保存结果到JSON
        output_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            f"backtest_etf_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        output_path = os.path.normpath(output_path)
        summary = []
        for r in results:
            summary.append({
                "etf": r["etf"],
                "initial_capital": r["initial_capital"],
                "final_value": r["final_value"],
                "total_return_pct": r["total_return_pct"],
                "win_rate_pct": r["win_rate_pct"],
                "total_trades": r["total_trades"],
                "max_drawdown_pct": r["max_drawdown_pct"],
            })
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"\n📁 结果已保存: {output_path}", file=sys.stderr)
    else:
        print("❌ 没有成功获取任何ETF数据", file=sys.stderr)


if __name__ == "__main__":
    main()
