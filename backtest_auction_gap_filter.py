#!/usr/bin/env python3
"""
补充分析: 高开 + 趋势过滤 能否提升正期望?
测试过滤器:
  A. 前期强势: 信号日前收盘 MA5 > MA18 (趋势多头)
  B. 前日涨幅: 前一日涨跌幅 >= 0 (不追下跌反弹)
  C. 高开+强势组合
  D. 对比: 无过滤基线

用法: python3 backtest_auction_gap_filter.py --universe 200 --days 240
"""
import sys, time, json, urllib.request
sys.path.insert(0, "/home/super-user/screening")
from strategies.data.fetcher import DataFetcher, code_to_prefix

TENCENT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"}
SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
FEE_RATE = 0.005


def fetch_sina_all():
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeDataSimple?page=1&num=5000"
           "&sort=amount&asc=0&node=hs_a&symbol=&_s_r_a=page")
    req = urllib.request.Request(url, headers=SINA_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("gbk", errors="replace"))


def fetch_kline_with_dates(sym, days=500):
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq"
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=TENCENT_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            if not raw or len(raw) < 50:
                time.sleep(1); continue
            parsed = json.loads(raw)
            data = parsed.get("data", {})
            target_key = next((k for k in data if sym.replace("/", "") in k), None)
            if not target_key:
                return None
            klines = data[target_key].get("qfqday", data[target_key].get("day", []))
            if not klines or len(klines) < 5:
                return None
            result = []
            for e in klines:
                if len(e) >= 6:
                    try:
                        result.append({"date": str(e[0]), "open": float(e[1]),
                                       "close": float(e[2]), "high": float(e[3]),
                                       "low": float(e[4]), "volume": float(e[5]) if e[5] else 0})
                    except (ValueError, IndexError):
                        continue
            return result
        except Exception:
            time.sleep(1.5)
    return None


def get_limit_pct(code):
    if code.startswith(("300", "301", "688", "689")): return 20.0
    if code.startswith(("8", "4", "92")): return 30.0
    return 10.0


def main():
    universe_size = 200
    days = 240
    all_stocks = fetch_sina_all()
    filtered = []
    for s in all_stocks:
        name = s.get("name", ""); code = s.get("code", "")
        try: amount = float(s.get("amount", 0) or 0)
        except (ValueError, TypeError): continue
        if not name or name.startswith(("ST", "*ST", "S", "N", "C")) or "退" in name: continue
        filtered.append({"code": code, "name": name, "amount": amount})
    filtered.sort(key=lambda x: x["amount"], reverse=True)
    universe = filtered[: universe_size * 2]

    # 市值过滤
    mvs = {}
    codes = [f"{code_to_prefix(st['code'])}{st['code']}" for st in universe if code_to_prefix(st['code'])]
    for i in range(0, len(codes), 80):
        batch = codes[i:i+80]
        url = f"https://qt.gtimg.cn/q={','.join(batch)}"
        try:
            req = urllib.request.Request(url, headers=TENCENT_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("gbk", errors="replace")
            for line in raw.strip().split("\n"):
                if '="' not in line: continue
                val = line.split('="')[1].rstrip('"').rstrip(";")
                parts = val.split("~")
                if len(parts) < 45: continue
                try: mvs[parts[2]] = float(parts[44]) if parts[44] else 0
                except ValueError: mvs[parts[2]] = 0
        except Exception as e:
            print(f"[WARN] mv batch: {e}", file=sys.stderr)
        time.sleep(0.2)
    universe = [st for st in universe if 0 < mvs.get(st["code"], 0) <= 1000][:universe_size]
    print(f"Universe: {len(universe)}", file=sys.stderr)

    klines_data = {}
    for i, st in enumerate(universe):
        prefix = code_to_prefix(st["code"])
        if not prefix: continue
        kd = fetch_kline_with_dates(f"{prefix}{st['code']}")
        if kd and len(kd) >= 30:
            klines_data[st["code"]] = kd[-days:]
        if (i+1) % 50 == 0: print(f"  kline {i+1}/{len(universe)} ok={len(klines_data)}", file=sys.stderr)
        time.sleep(0.15)
    print(f"Klines: {len(klines_data)}", file=sys.stderr)

    # 采集信号 + 前一日MA5/MA18状态 + 前日涨跌
    signals = []
    for code, kd in klines_data.items():
        name = next((x["name"] for x in universe if x["code"] == code), code)
        limit_pct = get_limit_pct(code)
        for i in range(20, len(kd) - 10):
            prev_close = kd[i-1]["close"]
            if prev_close <= 0: continue
            op = kd[i]["open"]
            gap = (op / prev_close - 1) * 100
            if gap < 3.0: continue  # 只研究高开>=3%
            if gap >= limit_pct * 0.98: continue  # 涨停开盘不可买
            closes = [kd[j]["close"] for j in range(i-18, i)]
            ma5 = sum(closes[-5:]) / 5
            ma18 = sum(closes) / 18
            prev_chg = (kd[i-1]["close"] / kd[i-2]["close"] - 1) * 100 if kd[i-2]["close"] > 0 else 0
            ret_open1 = (kd[i+1]["open"] / op - 1) * 100 - FEE_RATE * 200 if kd[i+1]["open"] > 0 else None
            ret_close1 = (kd[i+1]["close"] / op - 1) * 100 - FEE_RATE * 200 if kd[i+1]["close"] > 0 else None
            signals.append({
                "code": code, "name": name, "date": kd[i]["date"], "gap": gap,
                "ma5_gt_ma18": ma5 > ma18, "prev_chg": prev_chg,
                "ret_open1": ret_open1, "ret_close1": ret_close1,
            })
    print(f"Signals (gap>=3%): {len(signals)}", file=sys.stderr)

    def stats(vals):
        vals = [v for v in vals if v is not None]
        if not vals: return None
        n = len(vals); wins = sum(1 for v in vals if v > 0)
        avg = sum(vals) / n; med = sorted(vals)[n // 2]
        gains = [v for v in vals if v > 0]; losses = [v for v in vals if v <= 0]
        ag = sum(gains) / len(gains) if gains else 0
        al = sum(losses) / len(losses) if losses else 0
        return f"n={n:>5d} 胜率={wins/n*100:>5.1f}% 均={avg:>+6.2f}% 中位={med:>+6.2f}% 盈亏比={abs(ag/al) if al else float('inf'):>5.2f}"

    print()
    print("=" * 90)
    print(f"过滤器分析 (高开≥3%, universe={len(klines_data)}, {days}天)")
    print("=" * 90)
    print(f"\n── T+1 开盘卖 ──")
    print(f"基线(全部)              {stats([s['ret_open1'] for s in signals])}")
    print(f"A. MA5>MA18(强势)      {stats([s['ret_open1'] for s in signals if s['ma5_gt_ma18']])}")
    print(f"B. 前日涨(prev>=0)      {stats([s['ret_open1'] for s in signals if s['prev_chg'] >= 0])}")
    print(f"C. 强势+前日涨           {stats([s['ret_open1'] for s in signals if s['ma5_gt_ma18'] and s['prev_chg'] >= 0])}")
    print(f"D. 弱势(MA5<=MA18)      {stats([s['ret_open1'] for s in signals if not s['ma5_gt_ma18']])}")
    print(f"\n── T+1 收盘卖 ──")
    print(f"基线(全部)              {stats([s['ret_close1'] for s in signals])}")
    print(f"A. MA5>MA18(强势)      {stats([s['ret_close1'] for s in signals if s['ma5_gt_ma18']])}")
    print(f"B. 前日涨(prev>=0)      {stats([s['ret_close1'] for s in signals if s['prev_chg'] >= 0])}")
    print(f"C. 强势+前日涨           {stats([s['ret_close1'] for s in signals if s['ma5_gt_ma18'] and s['prev_chg'] >= 0])}")
    print(f"D. 弱势(MA5<=MA18)      {stats([s['ret_close1'] for s in signals if not s['ma5_gt_ma18']])}")
    print(f"\n── 高开5~8% 子集 (T+1收盘) ──")
    sub5 = [s for s in signals if 5 <= s["gap"] < 8]
    print(f"基线(高开5-8%)          {stats([s['ret_close1'] for s in sub5])}")
    print(f"A. 强势                 {stats([s['ret_close1'] for s in sub5 if s['ma5_gt_ma18']])}")
    print(f"C. 强势+前日涨           {stats([s['ret_close1'] for s in sub5 if s['ma5_gt_ma18'] and s['prev_chg'] >= 0])}")


if __name__ == "__main__":
    main()
