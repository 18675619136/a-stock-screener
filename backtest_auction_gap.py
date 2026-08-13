#!/usr/bin/env python3
"""
集合竞价高开策略回测 (backtest_auction_gap.py)

目标: 回答"9:26 集合竞价筛选高开股"的优化问题:
  1. 高开幅度阈值多少最优? (3% / 5% / 8% / 10%)
  2. 高开股持有几天收益最优? (T+1开盘 / T+1收盘 / T+3 / T+5 / T+10)
  3. 市值/成交额过滤是否提升胜率?
  4. 涨停开盘股(买不到)占比多少?

方法论:
  - Universe: 新浪全市场 → 市值<1000亿 + 非ST/退 → 按成交额取前 N 只（流动性代表）
  - 数据: 腾讯 fqkline 日K线 (ifzq.gtimg.cn, 500天)
  - 信号: 当日 open / 昨日 close - 1 >= 阈值 → T日开盘买入
  - 规则: A股 T+1（T日买入不可卖，最短 T+1 卖出）; 手续费 0.5% 双向
  - 统计: 每笔信号的收益分布（不模拟资金管理，聚焦信号质量）

用法:
  python3 backtest_auction_gap.py --universe 300 --days 240 [--save]
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, "/home/super-user/screening")
from strategies.data.fetcher import DataFetcher, code_to_prefix, log

FEE_RATE = 0.005  # 0.5% 双向手续费（用户指定）
TENCENT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"}
SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}


def fetch_sina_all():
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeDataSimple?page=1&num=5000"
           "&sort=amount&asc=0&node=hs_a&symbol=&_s_r_a=page")
    req = urllib.request.Request(url, headers=SINA_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("gbk", errors="replace")
    return json.loads(raw)


def fetch_kline_with_dates(sym, days=500):
    """获取带日期的日K线 [date, open, close, high, low, volume]"""
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq"
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=TENCENT_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            if not raw or len(raw) < 50:
                time.sleep(1)
                continue
            parsed = json.loads(raw)
            data = parsed.get("data", {})
            target_key = None
            for k in data:
                if sym.replace("/", "") in k:
                    target_key = k
                    break
            if not target_key:
                return None
            klines = data[target_key].get("qfqday", data[target_key].get("day", []))
            if not klines or len(klines) < 5:
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
            return result
        except Exception:
            time.sleep(1.5)
    return None


def get_limit_pct(code):
    """板块涨跌幅限制"""
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=int, default=300)
    parser.add_argument("--days", type=int, default=240, help="回测K线天数(用最新N天)")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    # 1. 全市场 → 基础过滤 → 按成交额取 universe
    all_stocks = fetch_sina_all()
    log(f"[1] Sina total: {len(all_stocks)}")
    filtered = []
    for s in all_stocks:
        name = s.get("name", "")
        code = s.get("code", "")
        try:
            amount = float(s.get("amount", 0) or 0)
        except (ValueError, TypeError):
            continue
        if not name or name.startswith(("ST", "*ST", "S", "N", "C")) or "退" in name:
            continue
        filtered.append({"code": code, "name": name, "amount": amount, "mktcap": 0})
    filtered.sort(key=lambda x: x["amount"], reverse=True)
    # 取成交额前 2×universe 再用腾讯市值过滤，保证最终有 universe 只
    universe = filtered[: args.universe * 2]

    # 1b. 批量获取市值（腾讯 parts[44]，亿元）→ 过滤 mv<1000亿
    def fetch_market_caps(stocks):
        codes_with_prefix = []
        for st in stocks:
            prefix = code_to_prefix(st["code"])
            if prefix:
                codes_with_prefix.append(f"{prefix}{st['code']}")
        mvs = {}
        names = {}
        for i in range(0, len(codes_with_prefix), 80):
            batch = codes_with_prefix[i:i + 80]
            url = f"https://qt.gtimg.cn/q={','.join(batch)}"
            try:
                req = urllib.request.Request(url, headers=TENCENT_HEADERS)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode("gbk", errors="replace")
                for line in raw.strip().split("\n"):
                    if '="' not in line:
                        continue
                    val = line.split('="')[1].rstrip('"').rstrip(";")
                    parts = val.split("~")
                    if len(parts) < 45:
                        continue
                    code = parts[2]
                    names[code] = parts[1]
                    try:
                        mvs[code] = float(parts[44]) if parts[44] else 0
                    except ValueError:
                        mvs[code] = 0
            except Exception as e:
                log(f"  [WARN] mv batch failed: {e}")
            time.sleep(0.2)
        return mvs, names

    mvs, names_map = fetch_market_caps(universe)
    universe = [st for st in universe if 0 < mvs.get(st["code"], 0) <= 1000][: args.universe]
    for st in universe:
        st["mktcap"] = mvs.get(st["code"], 0)
        if names_map.get(st["code"]):
            st["name"] = names_map[st["code"]]
    log(f"[2] Universe: {len(universe)} stocks (amount-sorted, mv<1000亿, 腾讯市值过滤)")

    # 2. 批量获取K线
    klines_data = {}
    fetcher = DataFetcher({})
    for i, st in enumerate(universe):
        prefix = code_to_prefix(st["code"])
        if not prefix:
            continue
        sym = f"{prefix}{st['code']}"
        kd = fetch_kline_with_dates(sym)
        if kd and len(kd) >= 25:
            # 保留最新 args.days 根
            klines_data[st["code"]] = kd[-args.days:]
        if (i + 1) % 50 == 0:
            log(f"    kline {i+1}/{len(universe)} ok={len(klines_data)}")
        time.sleep(0.15)
    log(f"[3] Klines fetched: {len(klines_data)} stocks")
    if len(klines_data) < 20:
        log("FATAL: too few klines")
        return

    # 3. 逐日扫描高开信号
    # 信号定义: open[i] / close[i-1] - 1 >= threshold
    # 同时记录: 次日收益(open), 次日收盘, T+3, T+5, T+10 收盘收益
    # 以及 当日盘中冲高幅度 (high/open-1) 和 涨停开盘标记
    signals = []  # {code, name, date, gap, ret_open1, ret_close1, ret_close3, ret_close5, ret_close10, limit_open, day_high_pct}
    for code, kd in klines_data.items():
        name = next((x["name"] for x in universe if x["code"] == code), code)
        limit_pct = get_limit_pct(code)
        for i in range(1, len(kd) - 10):
            prev_close = kd[i - 1]["close"]
            if prev_close <= 0:
                continue
            op = kd[i]["open"]
            gap = (op / prev_close - 1) * 100
            if gap < 1.0:  # 只记录 gap>=1% 的信号（低阈值用于对比）
                continue
            # 涨停开盘（一字板/开盘即封板，实际买不到）
            limit_open = gap >= limit_pct * 0.98
            # 收益: 以开盘价买入（含手续费）
            def ret_from(kline_idx, price_field="open"):
                if kline_idx >= len(kd):
                    return None
                px = kd[kline_idx][price_field]
                if px <= 0:
                    return None
                return (px / op - 1) * 100 - FEE_RATE * 200  # 买卖各0.5%

            ret_open1 = ret_from(i + 1, "open")      # T+1 开盘卖
            ret_close1 = ret_from(i + 1, "close")    # T+1 收盘卖
            ret_close3 = ret_from(i + 3, "close")    # T+3 收盘卖
            ret_close5 = ret_from(i + 5, "close")    # T+5 收盘卖
            ret_close10 = ret_from(i + 10, "close")  # T+10 收盘卖
            # 当日盘中冲高（开盘买入后当日最大浮盈）
            day_high = (kd[i]["high"] / op - 1) * 100 if op > 0 else 0

            signals.append({
                "code": code, "name": name, "date": kd[i]["date"], "gap": gap,
                "limit_open": limit_open, "day_high_pct": day_high,
                "ret_open1": ret_open1, "ret_close1": ret_close1,
                "ret_close3": ret_close3, "ret_close5": ret_close5,
                "ret_close10": ret_close10,
            })
    log(f"[4] Signals collected: {len(signals)}")

    # 4. 统计
    def stats(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        n = len(vals)
        wins = sum(1 for v in vals if v > 0)
        avg = sum(vals) / n
        med = sorted(vals)[n // 2]
        gains = [v for v in vals if v > 0]
        losses = [v for v in vals if v <= 0]
        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        pl_ratio = abs(avg_gain / avg_loss) if avg_loss else float("inf")
        big_loss = sum(1 for v in vals if v < -8)
        return {
            "n": n, "win_rate": wins / n * 100, "avg": avg, "median": med,
            "avg_gain": avg_gain, "avg_loss": avg_loss, "pl_ratio": pl_ratio,
            "big_loss": big_loss, "big_loss_pct": big_loss / n * 100,
        }

    def fmt(st):
        if not st:
            return "  N/A"
        return (f"  n={st['n']:>5d} 胜率={st['win_rate']:>5.1f}% "
                f"均收益={st['avg']:>+6.2f}% 中位={st['median']:>+6.2f}% "
                f"盈亏比={st['pl_ratio']:>5.2f} 大亏(<-8%)={st['big_loss_pct']:>4.1f}%")

    # 排除涨停开盘（买不到）后的有效信号
    tradable = [s for s in signals if not s["limit_open"]]
    limit_open_cnt = sum(1 for s in signals if s["limit_open"])
    log(f"[5] tradable signals: {len(tradable)} (limit_open excluded: {limit_open_cnt})")

    # 4.1 全样本概览（按高开幅度分段）
    print("\n" + "=" * 80)
    print(f"集合竞价高开策略回测 | universe={len(klines_data)} | 周期={args.days}天 | 手续费0.5%双向")
    print(f"总信号: {len(signals)} | 涨停开盘(不可买): {limit_open_cnt} ({limit_open_cnt/len(signals)*100:.1f}%) | 可交易: {len(tradable)}")
    print("=" * 80)

    # 4.2 高开幅度分段 × 持有期（用可交易信号）
    gap_bins = [(1, 3), (3, 5), (5, 8), (8, 10), (10, 999)]
    hold_fields = [
        ("ret_open1", "T+1开盘卖"),
        ("ret_close1", "T+1收盘卖"),
        ("ret_close3", "T+3收盘卖"),
        ("ret_close5", "T+5收盘卖"),
        ("ret_close10", "T+10收盘卖"),
    ]

    print("\n─── 高开幅度 × 持有期 收益矩阵（可交易信号）───")
    print(f"{'高开区间':<10s}" + "".join(f"{label:>52s}" for _, label in hold_fields))
    for lo, hi in gap_bins:
        bin_signals = [s for s in tradable if lo <= s["gap"] < hi]
        if not bin_signals:
            continue
        label = f"{lo}~{hi}%"
        row = f"{label:<10s}"
        for fld, _ in hold_fields:
            row += f"{fmt(stats([s[fld] for s in bin_signals])):>52s}"
        print(row)

    # 4.3 全部可交易信号 × 持有期（总览）
    print("\n─── 全部可交易信号 × 持有期 ───")
    for fld, label in hold_fields:
        print(f"{label:<10s}{fmt(stats([s[fld] for s in tradable]))}")

    # 4.4 当日盘中浮盈（开盘买入当日最高浮盈分布）
    print("\n─── 当日盘中浮盈（开盘买入 → 当日最高）───")
    print(f"平均盘中最大浮盈: {sum(s['day_high_pct'] for s in tradable)/len(tradable):+.2f}%")
    print(f"盘中曾触及+5%的比例: {sum(1 for s in tradable if s['day_high_pct']>=5)/len(tradable)*100:.1f}%")
    print(f"盘中曾触及+10%的比例: {sum(1 for s in tradable if s['day_high_pct']>=10)/len(tradable)*100:.1f}%")

    # 4.5 市值分组 × T+1收盘（若mktcap可用）
    print("\n─── 市值分组 × T+1收盘卖（可交易信号）───")
    mv_bins = [(0, 50), (50, 150), (150, 400), (400, 1000)]
    code_mv = {x["code"]: x["mktcap"] for x in universe}
    for lo, hi in mv_bins:
        bin_signals = [s for s in tradable if lo <= code_mv.get(s["code"], 0) < hi]
        if not bin_signals:
            continue
        print(f"{lo}~{hi}亿  {fmt(stats([s['ret_close1'] for s in bin_signals]))}")

    # 4.6 高开幅度阈值敏感性（T+1收盘）
    print("\n─── 阈值敏感性（T+1收盘卖，可交易信号）───")
    for th in [1, 2, 3, 5, 7, 9]:
        bin_signals = [s for s in tradable if s["gap"] >= th]
        if bin_signals:
            print(f"高开≥{th}%  {fmt(stats([s['ret_close1'] for s in bin_signals]))}")

    # 5. 保存结果
    if args.save:
        out = {
            "universe": len(klines_data), "days": args.days,
            "total_signals": len(signals),
            "limit_open_cnt": limit_open_cnt,
            "tradable_cnt": len(tradable),
            "summary_by_gap": {},
            "summary_overall": {},
        }
        for lo, hi in gap_bins:
            bin_signals = [s for s in tradable if lo <= s["gap"] < hi]
            key = f"{lo}-{hi}"
            out["summary_by_gap"][key] = {
                fld: stats([s[fld] for s in bin_signals])
                for fld, _ in hold_fields
            }
        out["summary_overall"] = {
            fld: stats([s[fld] for s in tradable]) for fld, _ in hold_fields
        }
        path = f"/home/super-user/screening/backtest_results/auction_gap_{args.universe}_{args.days}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        log(f"Saved: {path}")


if __name__ == "__main__":
    main()
