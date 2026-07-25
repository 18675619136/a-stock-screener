#!/usr/bin/env python3
"""
风险因子模拟买卖工具 - Risk-balanced stock selection simulator

基于5因子风控模型对 momentum_ma 候选股进行二次筛选，
按风险收益比选择最优持仓组合。

用法:
    python3 risk_sim.py                          # 默认方案B
    python3 risk_sim.py --plan A                  # 进取型
    python3 risk_sim.py --plan B                  # 稳健型（默认）
    python3 risk_sim.py --plan C                  # 防御型
    python3 risk_sim.py --capital 300000          # 自定义总资金
    python3 risk_sim.py --list-plans              # 列出方案
    python3 risk_sim.py --output /path/to/file    # 输出到文件
"""

import sys
import os
import json
import argparse
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

# Report formatter
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
try:
    from report_formatter import FeishuFormatter
except ImportError:
    FeishuFormatter = None

from strategies.engine import Engine
from strategies.data.fetcher import log


# ═══════════════════════════════════════════════
# 风险因子权重配置
# ═══════════════════════════════════════════════

RISK_WEIGHTS = {
    "score":      0.25,   # 策略评分（动量+均线+赛道等）
    "chg":        0.20,   # 单日涨幅合理性
    "ma_deviation": 0.20, # 偏离MA18距离
    "volume":     0.20,   # 量比健康度
    "liquidity":  0.15,   # 流动性（市值+价格）
}

# 3种持仓方案
PLANS = {
    "A": {  # 进取型 — 评分优先
        "description": "进取型 — 评分优先，适当容忍偏离",
        "picks": 3,
        "allocation": [0.40, 0.40, 0.20],  # 仓位比例
        "risk_preference": "aggressive",
    },
    "B": {  # 稳健型 — 风险收益平衡（默认）
        "description": "稳健型 — 风险收益平衡",
        "picks": 3,
        "allocation": [0.35, 0.35, 0.30],
        "risk_preference": "balanced",
    },
    "C": {  # 防御型 — 保守选股
        "description": "防御型 — 风险第一，宁可错过",
        "picks": 2,
        "allocation": [0.50, 0.50],
        "risk_preference": "defensive",
    },
}


def compute_risk_score(s: dict) -> dict:
    """
    计算5因子风险评分（分数越高=风险越低=越适合买入）

    返回原始评分和综合分
    """
    chg = abs(s.get("changepercent", 0))
    vr = s.get("volume_ratio", 0)
    above = s.get("above_ma18_pct", 0)
    score = s.get("score", 0)
    mv = s.get("mv", 0)
    price = s.get("price", 0)
    ma5 = s.get("ma5", 0)
    ma18 = s.get("ma18", 0)

    components = {}

    # 1. 策略评分（直接归一化到0-1）
    components["score"] = min(score / 0.7, 1.0)

    # 2. 涨幅合理性：5-12%最优，过高或过低都扣分
    if 5 <= chg <= 8:
        components["chg"] = 1.0
    elif 8 < chg <= 12:
        components["chg"] = 0.9
    elif 12 < chg <= 15:
        components["chg"] = 0.6
    elif 15 < chg <= 20:
        components["chg"] = 0.3
    elif chg > 20:
        components["chg"] = 0.0
    elif 3 <= chg < 5:
        components["chg"] = 0.7  # 涨幅偏弱但可接受
    else:
        components["chg"] = 0.4  # 涨幅太弱

    # 3. 偏离MA18：15-25%最佳区间
    if 15 <= above <= 20:
        components["ma_deviation"] = 1.0
    elif 20 < above <= 25:
        components["ma_deviation"] = 0.9
    elif 10 <= above < 15:
        components["ma_deviation"] = 0.8
    elif 25 < above <= 30:
        components["ma_deviation"] = 0.6
    elif 30 < above <= 35:
        components["ma_deviation"] = 0.3
    elif above > 35:
        components["ma_deviation"] = 0.0
    else:
        components["ma_deviation"] = 0.5  # <10% 趋势偏弱

    # 4. 量比健康度：1.5-2.5最优
    if 1.5 <= vr <= 2.0:
        components["volume"] = 1.0
    elif 2.0 < vr <= 2.5:
        components["volume"] = 0.9
    elif 2.5 < vr <= 3.0:
        components["volume"] = 0.6
    elif 3.0 < vr <= 4.0:
        components["volume"] = 0.3
    elif vr > 4.0:
        components["volume"] = 0.0
    elif 1.2 <= vr < 1.5:
        components["volume"] = 0.7  # 刚过放量阈值
    else:
        components["volume"] = 0.3  # 缩量

    # 5. 流动性：市值100-300亿最佳
    if 100 <= mv <= 200:
        components["liquidity"] = 1.0
    elif 200 < mv <= 300:
        components["liquidity"] = 0.9
    elif 300 < mv <= 500:
        components["liquidity"] = 0.7
    elif 500 < mv <= 800:
        components["liquidity"] = 0.5
    elif mv > 800:
        components["liquidity"] = 0.2
    elif 50 <= mv < 100:
        components["liquidity"] = 0.7  # 小市值但可接受
    else:
        components["liquidity"] = 0.3

    # 高价罚分
    if price > 150:
        components["liquidity"] *= 0.8

    # 综合风险评分 (0-1)
    total = sum(components[k] * RISK_WEIGHTS[k] for k in RISK_WEIGHTS)
    # 归一化到0-1
    total = total / sum(RISK_WEIGHTS.values())

    return {
        "risk_score": round(total, 4),
        "components": components,
        "flags": [],
    }


def compute_risk_flags(s: dict) -> list[str]:
    """返回风险标记列表"""
    flags = []
    chg = abs(s.get("changepercent", 0))
    vr = s.get("volume_ratio", 0)
    above = s.get("above_ma18_pct", 0)
    mv = s.get("mv", 0)
    price = s.get("price", 0)

    if chg > 15:
        flags.append("追高风险")
    elif chg > 12:
        flags.append("涨幅偏高")
    if above > 30:
        flags.append("偏离过大")
    if vr > 3:
        flags.append("量比异常")
    if vr < 1.3:
        flags.append("放量不足")
    if price > 150:
        flags.append("高价")
    if mv < 80:
        flags.append("小市值")
    if mv > 500:
        flags.append("市值偏高")

    return flags if flags else ["✅"]


def select_by_plan(candidates: list[dict], plan_name: str) -> list[dict]:
    """按方案选择持仓"""
    plan = PLANS[plan_name]
    pref = plan["risk_preference"]
    n_picks = plan["picks"]
    allocations = plan["allocation"]

    # 对每个候选股计算综合推荐分
    scored = []
    for s in candidates:
        risk_info = compute_risk_score(s)
        flags = compute_risk_flags(s)

        risk_score = risk_info["risk_score"]
        strategy_score = s.get("score", 0)

        # 综合推荐分 = 策略评分 × (1 - preference_factor) + 风控评分 × preference_factor
        if pref == "aggressive":
            pref_factor = 0.30  # 30%风控权重
        elif pref == "balanced":
            pref_factor = 0.50  # 50%风控权重
        else:  # defensive
            pref_factor = 0.70  # 70%风控权重

        composite = strategy_score * (1 - pref_factor) + risk_score * pref_factor

        scored.append({
            **s,
            "risk_score": risk_score,
            "risk_flags": flags,
            "composite": round(composite, 4),
        })

    # 按综合推荐分降序
    scored.sort(key=lambda x: x["composite"], reverse=True)

    # 取前N只
    selected = scored[:n_picks]

    # 分配仓位
    total_capital = PLAN_CONFIG.get("capital", 240000)
    for i, s in enumerate(selected):
        alloc_pct = allocations[i] if i < len(allocations) else 0.33
        alloc_amount = total_capital * alloc_pct
        price = s.get("latest_price", s.get("price", 0))
        if price > 0:
            shares = int(alloc_amount / price / 100) * 100
            s["sim_shares"] = shares
            s["sim_cost"] = shares * price
            s["allocation_pct"] = alloc_pct

    return selected


# ── 法定假日（A股休市日）──
HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-05",
    "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",
    "2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-01",
    "2026-09-07",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}


def _prev_trading_day(d: datetime.date) -> datetime.date:
    """找到 d 之前最近的一个交易日"""
    from datetime import timedelta
    while True:
        d -= timedelta(days=1)
        if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in HOLIDAYS_2026:
            return d


def _next_n_trading_days(start: datetime.date, n: int) -> datetime.date:
    """从 start 往后数 n 个交易日"""
    cur = start
    cnt = 0
    while cnt < n:
        from datetime import timedelta
        cur += timedelta(days=1)
        if cur.weekday() >= 5:
            continue
        if cur.strftime("%Y-%m-%d") in HOLIDAYS_2026:
            continue
        cnt += 1
    return cur


def format_report(
    candidates: list[dict],
    selected: list[dict],
    plan_name: str,
    capital: float,
) -> str:
    """生成格式化报告"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d %H:%M")
    plan = PLANS[plan_name]

    # 计算买入日（最近交易日）和计划卖出日（+10交易日）
    buy_date = now.date()
    if buy_date.weekday() >= 5 or buy_date.strftime("%Y-%m-%d") in HOLIDAYS_2026:
        buy_date = _prev_trading_day(buy_date)
    sell_date = _next_n_trading_days(buy_date, 10)
    lines = []

    lines.append("📊 风 险 因 子 模 拟 买 卖")
    lines.append("")
    lines.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"── 方 案 配 置 ──")
    lines.append("")
    lines.append(f"  方案: {plan_name} — {plan['description']}")
    lines.append(f"  总资金: {capital:,.0f}元")
    lines.append(f"  选股池: {len(candidates)}只候选股")
    lines.append("")

    # ── 风险因子评分表 ──
    lines.append("── 候 选 股 风 险 评 分（Top 10）──")
    lines.append("")
    lines.append(f"{'排名':>4s} {'代码':>8s} {'名称':>10s} {'策略分':>7s} {'风控分':>7s} {'综合分':>7s} {'涨幅':>7s} {'偏离MA18':>8s} {'量比':>5s} {'风险':>12s}")
    lines.append("-" * 85)

    # 对前10个候选股计算并显示风险评分
    scored_all = []
    for s in candidates[:20]:
        ri = compute_risk_score(s)
        flags = compute_risk_flags(s)
        scored_all.append((s, ri, flags))

    # 按综合分排序显示（使用balanced权重展示）
    for item in scored_all:
        s, ri, flags = item
        # 临时用balanced权重算综合分展示
        composite = s.get("score", 0) * 0.5 + ri["risk_score"] * 0.5
        item = (composite, s, ri, flags)
        scored_all_items = [(s.get("score", 0) * 0.5 + compute_risk_score(s)["risk_score"] * 0.5, s, compute_risk_score(s), compute_risk_flags(s)) for s in candidates[:20]]
    
    scored_all_items = []
    for s in candidates[:20]:
        ri = compute_risk_score(s)
        flags = compute_risk_flags(s)
        composite = s.get("score", 0) * 0.5 + ri["risk_score"] * 0.5
        scored_all_items.append((composite, s, ri, flags))
    
    scored_all_items.sort(key=lambda x: x[0], reverse=True)

    for i, (composite, s, ri, flags) in enumerate(scored_all_items, 1):
        chg = s.get("changepercent", 0)
        vr = s.get("volume_ratio", 0)
        above = s.get("above_ma18_pct", 0)
        chg_str = f"🔴+{chg:.1f}%" if chg > 0 else f"🟢{chg:.1f}%"
        flag_str = ",".join(flags) if isinstance(flags, list) else flags
        lines.append(f"{i:>4d} {s['code']:>8s} {s['name']:>10s} {s.get('score',0):>7.4f} {ri['risk_score']:>7.4f} {composite:>7.4f} {chg_str:>7s} {above:>7.1f}% {vr:>4.1f}x {flag_str:>12s}")

    lines.append("")

    # ── 模拟买入详情 ──
    lines.append(f"── 模 拟 买 入（方案{plan_name}）──")
    lines.append("")

    total_cost = 0
    for i, s in enumerate(selected):
        code = s["code"]
        name = s["name"]
        price = s.get("latest_price", s.get("price", 0))
        chg = s.get("latest_chg_pct", s.get("changepercent", 0))
        shares = s.get("sim_shares", 0)
        cost = s.get("sim_cost", 0)
        score = s.get("score", 0)
        risk_score = s.get("risk_score", 0)
        composite = s.get("composite", 0)
        alloc = s.get("allocation_pct", 0) * 100
        total_cost += cost
        ma5 = s.get("ma5", 0)
        ma18 = s.get("ma18", 0)
        above = s.get("above_ma18_pct", 0)
        vr = s.get("volume_ratio", 0)
        flags = s.get("risk_flags", [])

        chg_str = f"🔴+{chg:.2f}%" if chg > 0 else f"🟢{chg:.2f}%"

        lines.append(f"🎯 {code} {name}")
        lines.append(f"   仓位: {alloc:.0f}%  |  买入价: {price:.2f}  {chg_str}")
        lines.append(f"   买入日: {buy_date.strftime('%m-%d')}  |  计划卖出: {sell_date.strftime('%m-%d')} (持仓10交易日)")
        lines.append(f"   买入量: {shares:,}股  |  投入: {cost:,.0f}元")
        lines.append(f"   综合分: {composite:.4f} (=策略{score:.4f}×50% + 风控{risk_score:.4f}×50%)")
        lines.append(f"   均线: MA5={ma5:.2f}  MA18={ma18:.2f}  |  偏离MA18: {above:.1f}%")
        lines.append(f"   量比: {vr:.2f}x  |  风险: {', '.join(flags) if isinstance(flags, list) else flags}")
        lines.append("")

    lines.append(f"总投入: {total_cost:,.0f}元 / 目标: {capital:,.0f}元  (剩余: {capital - total_cost:,.0f}元)")
    lines.append(f"买入日: {buy_date.strftime('%Y-%m-%d')}  |  计划卖出日: {sell_date.strftime('%Y-%m-%d')} (含10个交易日)")
    lines.append("")

    # ── 方案对比 ──
    lines.append("── 止 损 止 盈 参 考 ──")
    lines.append("")
    for s in selected:
        price = s.get("latest_price", s.get("price", 0))
        ma18 = s.get("ma18", 0)
        ma5 = s.get("ma5", 0)
        name = s["name"]
        code = s["code"]

        stop_loss_94 = price * 0.94
        stop_loss_ma18 = ma18
        take_profit_half = ma5 * 1.3 if ma5 > 0 else price * 1.15

        lines.append(f"{name}({code}) 买入价{price:.2f}")
        lines.append(f"   硬止损(94%): {stop_loss_94:.2f} (-{price-stop_loss_94:.2f})")
        lines.append(f"   MA18止损:    {stop_loss_ma18:.2f} (-{price-stop_loss_ma18:.2f})")
        lines.append(f"   半仓止盈:    {take_profit_half:.2f} (+{take_profit_half-price:.2f})")
        lines.append("")

    lines.append("── 免 责 声 明 ──")
    lines.append("")
    lines.append("本报告由AI自动生成，仅供参考，不构成投资建议。")
    lines.append("模拟买入非实盘操作，实际交易请自行判断风险。")
    lines.append("")
    lines.append(f"报告生成时间: {today}")

    # Convert empty lines to Braille Blank for Feishu compatibility
    lines = ["\u2800" if l == "" else l for l in lines]
    return "\n".join(lines)


def list_plans():
    """列出所有方案"""
    print("可用方案:")
    print()
    for name, plan in sorted(PLANS.items()):
        alloc_str = " + ".join(f"{int(p*100)}%" for p in plan["allocation"])
        print(f"  {name}: {plan['description']}")
        print(f"       选{plan['picks']}只, 仓位分配: {alloc_str}")
        print()


# ── 全局配置（可通过命令行覆盖）──
PLAN_CONFIG = {
    "capital": 240000,
}


def main():
    parser = argparse.ArgumentParser(
        description="风险因子模拟买卖工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 risk_sim.py                      # 默认方案B
  python3 risk_sim.py --plan A              # 进取型
  python3 risk_sim.py --plan B --capital 300000  # 30万资金方案B
  python3 risk_sim.py --list-plans          # 查看方案
  python3 risk_sim.py --output report.md    # 输出到文件
        """,
    )
    parser.add_argument("--plan", choices=["A", "B", "C"], default="B",
                        help="持仓方案 (默认: B 稳健型)")
    parser.add_argument("--capital", type=float, default=240000,
                        help="总资金 (默认: 240000)")
    parser.add_argument("--list-plans", action="store_true",
                        help="列出所有方案")
    parser.add_argument("--output", type=str, default=None,
                        help="输出到文件路径")

    args = parser.parse_args()

    if args.list_plans:
        list_plans()
        return

    PLAN_CONFIG["capital"] = args.capital

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 风险因子模拟买卖: {today} ===", file=sys.stderr)

    # ── Step 0: 清空上一日候选股数据 ──
    for f in ["candidates_momentum_ma.txt", "strategy_result.json",
              f"candidates_dual_ma_gc.txt"]:
        p = os.path.join(PROJECT_DIR, f)
        if os.path.exists(p):
            os.remove(p)
            log(f"  清空: {f}")

    # ── Step 1: Run strategy ──
    log("▶ Running momentum_ma strategy...")
    engine = Engine()
    result = engine.run("momentum_ma")
    candidates = result.final

    if not candidates:
        log("⚠️ 没有候选股！")
        return

    log(f"  候选股: {len(candidates)} 只")

    # ── Step 2: Fetch latest prices ──
    all_codes = [s["code"] for s in candidates]
    log("▶ Fetching latest prices...")
    latest = _fetch_latest_prices(all_codes)

    for s in candidates:
        c = s["code"]
        if c in latest:
            l = latest[c]
            s["latest_price"] = l["price"]
            s["latest_chg_pct"] = l["chg_pct"]
        else:
            s["latest_price"] = s.get("price", 0)
            s["latest_chg_pct"] = s.get("changepercent", 0)

    # ── Step 3: Select by plan ──
    selected = select_by_plan(candidates, args.plan)
    log(f"  方案{args.plan}选定 {len(selected)} 只")

    # ── Step 4: Generate report ──
    report = format_report(candidates, selected, args.plan, args.capital)

    # ── Step 5: Output ──
    # 不要在前面加空行（Feishu规则：消息开头直接标题）
    print(report)

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        log(f"报告已保存到 {args.output}")
    else:
        # 默认保存
        output_path = os.path.expanduser(f"~/.hermes/data/risk_sim_{today}.md")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        log(f"报告已保存到 {output_path}")


def _fetch_latest_prices(code_list):
    """从Tencent获取最新价格"""
    import urllib.request
    results = {}
    codes = list(code_list)
    syms = []
    for c in codes:
        if c.startswith("92"):
            syms.append(f"bj{c}")
        elif c.startswith(("6", "68")):
            syms.append(f"sh{c}")
        else:
            syms.append(f"sz{c}")

    if not syms:
        return results

    headers = {"User-Agent": "Mozilla/5.0"}
    url = f"http://qt.gtimg.cn/q={','.join(syms)}"
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15)
        raw = resp.read().decode("gbk", errors="replace")
        for line in raw.strip().split(";\n"):
            if not line.strip() or '="' not in line:
                continue
            try:
                val = line.split('="')[1].rstrip('";')
                parts = val.split("~")
                if len(parts) > 35:
                    code = parts[2]
                    results[code] = {
                        "price": float(parts[3]) if parts[3] else 0,
                        "chg_pct": float(parts[32]) if parts[32] else 0,
                    }
            except (ValueError, IndexError):
                continue
    except Exception as e:
        log(f"  [WARN] 获取价格失败: {e}")

    return results


if __name__ == "__main__":
    main()
