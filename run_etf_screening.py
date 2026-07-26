#!/usr/bin/env python3
"""
ETF 选股筛选脚本 — 每日16:30执行
基于最新 ETF 选股方案：MA5金叉MA18 + 逐级卖出

策略规则:
  🟢 买入信号: MA5上穿MA18（金叉）+ MA18向上 → 可买入
  🟡 持仓中:   MA5 > MA18，均线多头排列 → 继续持有
  🟠 减仓预警: MA5掉头向下（首次） → 卖一半
  🔴 卖出信号: MA5下穿MA18（死叉） → 全部卖出
  ⚪ 空仓观望: MA5 < MA18，均线空头排列 → 等待机会

输出格式: 统一飞书报告格式（FeishuFormatter）
"""

import json
import os
import sys
import time
import urllib.request

# ── 导入报告格式化工具 ──
HERMES_SCRIPTS = os.path.expanduser("~/.hermes/scripts")
if HERMES_SCRIPTS not in sys.path:
    sys.path.insert(0, HERMES_SCRIPTS)
from report_formatter import FeishuFormatter

# ── 配置 ──
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
POOL_FILE = os.path.join(PROJECT_DIR, "etf_candidate_pool.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com",
}
SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.sina.com.cn",
}

MA_SHORT = 5
MA_LONG = 18
MAX_KDAYS = 120

# 信号符号
SIG = {
    "buy": "🟢",
    "hold": "🟡",
    "warn": "🟠",
    "sell": "🔴",
    "wait": "⚪",
    "error": "❓",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch_url(url: str, headers: dict, timeout: int = 15) -> bytes | None:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def fetch_etf_klines(sym: str, max_days: int = MAX_KDAYS) -> list[dict] | None:
    """获取ETF K线，优先腾讯fqkline，回退到新浪"""
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{max_days},qfq"
    for attempt in range(2):
        raw = fetch_url(url, HEADERS, timeout=15)
        if raw and len(raw) > 50:
            try:
                parsed = json.loads(raw)
                data = parsed.get("data", {})
                target_key = None
                for k in data:
                    if sym.replace("/", "") in k:
                        target_key = k
                        break
                if target_key:
                    klines = data[target_key].get("qfqday", data[target_key].get("day", []))
                    if klines and len(klines) >= 25:
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
                                        "volume": float(e[5]) or 0,
                                    })
                                except (ValueError, IndexError):
                                    continue
                        return result[-max_days:]
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        time.sleep(1)

    # 回退到新浪
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={max_days}")
    raw = fetch_url(url, SINA_HEADERS, timeout=15)
    if raw and len(raw) > 20:
        try:
            data = json.loads(raw.decode("gbk"))
            return [{
                "date": e["day"],
                "open": float(e["open"]),
                "close": float(e["close"]),
                "high": float(e["high"]),
                "low": float(e["low"]),
                "volume": float(e["volume"]),
            } for e in data]
        except Exception:
            pass
    return None


def calc_ma(closes: list[float], period: int) -> list[float]:
    mas = []
    for i in range(len(closes)):
        if i < period - 1:
            mas.append(0.0)
        else:
            mas.append(sum(closes[i - period + 1: i + 1]) / period)
    return mas


def analyze_etf(etf: dict) -> dict:
    """分析单只ETF的当前信号"""
    code = etf["code"]
    prefix = etf["prefix"]
    name = etf["name"]
    sym = f"{prefix}{code}"

    klines = fetch_etf_klines(sym)
    if klines is None or len(klines) < 25:
        return {"code": code, "name": name, "signal": SIG["error"], "signal_text": "数据不足", "error": True}

    closes = [k["close"] for k in klines]
    dates = [k["date"] for k in klines]
    ma5 = calc_ma(closes, MA_SHORT)
    ma18 = calc_ma(closes, MA_LONG)

    n = len(closes) - 1
    latest = klines[n]
    prev = klines[n - 1] if n >= 1 else latest

    cur_close = latest["close"]
    cur_ma5 = ma5[n]
    cur_ma18 = ma18[n]
    prev_ma5 = ma5[n - 1] if n >= 1 else 0
    prev_ma18 = ma18[n - 1] if n >= 1 else 0
    change_pct = (cur_close - prev["close"]) / prev["close"] * 100 if prev["close"] > 0 else 0

    # 判断信号
    golden_cross = cur_ma5 > cur_ma18 and prev_ma5 <= prev_ma18 and cur_ma18 > prev_ma18
    death_cross = cur_ma5 < cur_ma18 and prev_ma5 >= prev_ma18
    ma5_turning_down = cur_ma5 < prev_ma5
    above_ma18 = cur_ma5 > cur_ma18
    ma18_rising = cur_ma18 > prev_ma18

    signal = SIG["wait"]
    signal_text = "空仓观望"
    signal_detail = "均线空头排列"
    action = "等待"

    if golden_cross:
        signal = SIG["buy"]
        signal_text = "买入信号"
        signal_detail = f"MA5({cur_ma5:.2f})金叉MA18({cur_ma18:.2f})+MA18向上"
        action = "关注买入"
    elif death_cross:
        signal = SIG["sell"]
        signal_text = "卖出信号"
        signal_detail = f"MA5({cur_ma5:.2f})死叉MA18({cur_ma18:.2f})"
        action = "清仓卖出"
    elif above_ma18 and ma5_turning_down:
        signal = SIG["warn"]
        signal_text = "减仓预警"
        signal_detail = f"MA5掉头向下({prev_ma5:.2f}→{cur_ma5:.2f})"
        action = "卖出一半"
    elif above_ma18:
        signal = SIG["hold"]
        signal_text = "持仓中"
        signal_detail = f"均线多头 MA5({cur_ma5:.2f})>MA18({cur_ma18:.2f})"
        action = "继续持有"
    elif ma5_below_ma18(cur_ma5, cur_ma18):
        if ma18_rising:
            signal_detail = f"MA5<MA18但MA18向上({prev_ma18:.2f}→{cur_ma18:.2f})"
            action = "等待金叉"
        else:
            signal_detail = "MA5<MA18且MA18向下"
            action = "等待"

    # MA20偏离度
    ma20_start = max(0, n - 19)
    ma20 = sum(closes[ma20_start: n + 1]) / (n - ma20_start + 1) if n - ma20_start + 1 >= 20 else 0
    ma20_dev = (cur_close - ma20) / ma20 * 100 if ma20 > 0 else 0

    change_str = f"{'+' if change_pct >= 0 else ''}{change_pct:.2f}%"

    return {
        "code": code,
        "name": name,
        "category": etf.get("category", ""),
        "date": dates[n],
        "close": cur_close,
        "change_pct": change_pct,
        "change_str": change_str,
        "ma5": round(cur_ma5, 3),
        "ma18": round(cur_ma18, 3),
        "ma20_dev": round(ma20_dev, 2),
        "signal": signal,
        "signal_text": signal_text,
        "signal_detail": signal_detail,
        "action": action,
    }


def ma5_below_ma18(ma5, ma18):
    return ma5 < ma18


def build_report(results: list[dict]) -> str:
    """使用股票候选列表格式构建报告"""
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 8 * 3600))

    f = FeishuFormatter(mode='dm')

    # 主标题与日期
    f.title("📊 ETF 选 股 筛 选")
    f.date(now)

    # 分组
    buy_list = [r for r in results if r["signal"] == SIG["buy"]]
    hold_list = [r for r in results if r["signal"] == SIG["hold"]]
    warn_list = [r for r in results if r["signal"] == SIG["warn"]]
    sell_list = [r for r in results if r["signal"] == SIG["sell"]]
    wait_list = [r for r in results if r["signal"] == SIG["wait"]]
    err_list = [r for r in results if r.get("error")]

    def fmt_etf(r):
        """格式化单只ETF为候选列表样式：颜色 名称（代码）  价格  涨幅"""
        color = "🔴" if r["change_pct"] >= 0 else "🟢"
        return f"{color} {r['name']}（{r['code']}）  {r['close']:.3f}  {r['change_str']}"

    # ── 候选列表 ──
    # 先展示有信号的（买入/减仓/卖出/持仓），再展示观望
    active = buy_list + warn_list + sell_list + hold_list
    if active:
        f.section(f"🎯 ETF 候 选 列 表  · 共{len(active)}只")
        # 按分类分组展示
        for label, items in [("🟢 买入信号", buy_list),
                             ("🟠 减仓预警", warn_list),
                             ("🔴 卖出信号", sell_list),
                             ("🟡 持仓中", hold_list)]:
            if items:
                f.line(f"  {label} · {len(items)}只")
                for r in items:
                    f.line(f"    {fmt_etf(r)}")
                if items is not hold_list:  # 最后一组后不加空行
                    f.line("")

    # ── 空仓观望 ──
    if wait_list:
        if active:
            f.blank()
        f.section(f"⚪ 空仓观望 · MA5<MA18 · {len(wait_list)}只")
        # 按分类展示: 创业板 / 纳斯达克
        cyb = [r for r in wait_list if r["category"] == "创业板"]
        nas = [r for r in wait_list if r["category"] == "纳斯达克"]
        for label, items in [("创业板", cyb), ("纳斯达克", nas)]:
            if items:
                f.line(f"  {label} {len(items)}只")
                for r in items:
                    f.line(f"    {fmt_etf(r)}")

    # ── 数据异常 ──
    if err_list:
        f.blank()
        f.section(f"❓ 数据异常 · {len(err_list)}只")
        for r in err_list:
            f.line(f"  {r['code']} {r['name']}：{r.get('signal_text', '数据获取失败')}")

    # ── 汇总 ──
    f.blank()
    total_ok = len(results) - len(err_list)
    f.section("汇 总")
    f.line(f"🟢 买入 {len(buy_list)}  |  🟡 持仓 {len(hold_list)}  |  "
           f"🟠 减仓 {len(warn_list)}  |  🔴 卖出 {len(sell_list)}  |  "
           f"⚪ 观望 {len(wait_list)}")
    f.line(f"共筛选 {total_ok} 只ETF，异常 {len(err_list)} 只")
    f.line(f"策略：MA5金叉MA18+MA18向上买入 / 两极卖出")

    return f.render()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ETF 选股筛选")
    parser.add_argument("--days", type=int, default=MAX_KDAYS, help="K线天数")
    parser.add_argument("--output", type=str, default="",
                        help="输出文件路径")
    args = parser.parse_args()

    if not os.path.exists(POOL_FILE):
        log(f"❌ 未找到候选池文件: {POOL_FILE}")
        sys.exit(1)

    with open(POOL_FILE, "r", encoding="utf-8") as f:
        pool = json.load(f)

    etfs = pool["etfs"]
    log(f"📂 加载 ETF 候选池: {len(etfs)} 只基金\n")

    results = []
    for i, etf in enumerate(etfs):
        sym = f"{etf['prefix']}{etf['code']}"
        print(f"  [{i+1}/{len(etfs)}] {etf['name']}({sym}) ... ", end="", file=sys.stderr, flush=True)
        result = analyze_etf(etf)
        results.append(result)
        status = "✅" if not result.get("error") else "❌"
        log(f"{status} {result['signal']} {result['signal_text']}")
        time.sleep(0.5)

    # 构建并输出报告
    report = build_report(results)
    print(report)

    # 保存到文件（用于后续推送）
    date_str = time.strftime("%Y%m%d", time.localtime(time.time() + 8 * 3600))
    output_path = args.output or os.path.join(PROJECT_DIR, f"etf_screening_report_{date_str}.txt")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)
    log(f"\n📁 报告已保存: {output_path}")


if __name__ == "__main__":
    main()
