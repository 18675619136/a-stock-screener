#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtester: MA20 由跌转升 → 8个点上涨 → 回踩MA20 买入，跌破MA5卖半 / 跌破MA20清仓
=====================================================================================

策略定义（用户口径，2026-09-13）:

  买入（同一交易日 t 全部成立）:
    1) MA20 由下跌转为上升
       - 存在拐点 j ∈ [t-turn_lookback, t-1]:
             MA20[j] > MA20[j-1]  且  MA20[j-1] <= MA20[j-2]
    2) 趋势未破
       - MA20[t] >= MA20[j]      (转升以来中期均线未创新低)
    3) 区间 [j, t] 内"至少有过一次大于8个点的上涨"
       - rally_mode = single : 区间内存在单日涨幅 > 8%
       - rally_mode = leg    : 区间最大涨幅 max(close)/min(close)-1 > 8%
    4) 回落到20日均线附近
       - close[t] ∈ [MA20[t]*(1-band), MA20[t]*(1+band)]
    5) 非 ST / 非退市；可选市值与股本过滤（默认关闭，忠于原策略）

  卖出（逐日监控，T+1：买入当日不可卖）:
    a) 价格回升站上 MA5（armed）之后，首次 close <= MA5  → 卖出一半
    b) close < MA20                                      → 全部卖出
       （a 与 b 同日触发时，b 优先——跌破20日线是本策略的最终退出）

  资金: 总资金 --capital（默认 150000），最多 --slots 只并发（默认 3），
        每只预算 = 总资金/slots；手续费买卖各 --fee（默认 0.5%）。

数据: 通达信(TDX) sidecar 优先（前复权/快），失败回退腾讯 fqkline。
      全市场K线一次性批量拉取并落盘缓存，便于多口径反复回测。

Usage:
  python3 -m strategies.backtest_ma20_surge_pullback --universe 1000 --days 480 \
      --rally-mode single --cache /tmp/mkt_u1000.json --save -o mp_single.json
  python3 -m strategies.backtest_ma20_surge_pullback --universe 1000 --days 480 \
      --rally-mode leg    --cache /tmp/mkt_u1000.json --save -o mp_leg.json
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))

from strategies.data.fetcher import DataFetcher, log, code_to_prefix  # noqa: E402

try:
    import tdx_client as TDX
except Exception:                                    # pragma: no cover
    TDX = None

ST_PREFIXES = ("ST", "*ST", "S")
WARMUP_BARS = 25          # 需要 MA20 + 拐点窗口


# ══════════════════════════════════════════════════════════════════
#  基础工具
# ══════════════════════════════════════════════════════════════════

def is_st(name: str) -> bool:
    return name.startswith(ST_PREFIXES) or "退" in name


def get_limit_pct(code: str) -> float:
    if code.startswith(("300", "688")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def moving_average(arr: list[float], period: int) -> list[float]:
    """简单移动平均，前 period-1 位为 0.0"""
    n = len(arr)
    out = [0.0] * n
    if n < period:
        return out
    s = sum(arr[:period])
    out[period - 1] = s / period
    for i in range(period, n):
        s += arr[i] - arr[i - period]
        out[i] = s / period
    return out


# ══════════════════════════════════════════════════════════════════
#  数据获取（TDX 优先，腾讯回退）+ 磁盘缓存
# ══════════════════════════════════════════════════════════════════

def tdx_batch_klines(syms: list[str], days: int, chunk: int = 50) -> dict[str, list[dict]]:
    """批量拉日K。

    ⚠️ 坑 (2026-09-13 实测): tdx_client.klines_batch 内部超时 = max(TIMEOUT,
    0.5+0.06*n)，默认 TIMEOUT=6s —— 分片稍大（80只）就会客户端超时放弃，
    服务端仍在处理 → BrokenPipe，整批返回 None。且服务端对 /klines 全程持
    全局锁，分片过大会长时间阻塞其他调用方（cron 任务）。
    对策：把 TIMEOUT 调高、分片压到 50，超时随分片规模线性放大。
    """
    out: dict[str, list[dict]] = {}
    if TDX is None:
        return out
    try:
        TDX.TIMEOUT = 120.0
    except Exception:
        pass
    for i in range(0, len(syms), chunk):
        part = syms[i:i + chunk]
        try:
            data = TDX.klines_batch(part, days=days, qfq=True)
        except Exception as e:
            log(f"  [WARN] TDX batch 异常 chunk@{i}: {str(e)[:60]}")
            data = None
        if not data:
            log(f"  [WARN] TDX batch 空 chunk@{i} ({len(part)} 只)")
            continue
        for sym, bars in data.items():
            if bars and len(bars) >= 60:
                out[sym] = [{"date": b["date"], "open": b["open"], "high": b["high"],
                             "low": b["low"], "close": b["close"],
                             "volume": b.get("volume", 0)} for b in bars]
        log(f"  TDX {min(i+chunk, len(syms))}/{len(syms)} 只, 累计 ok={len(out)}")
    return out


def tencent_one_kline(sym: str, days: int = 500) -> list[dict] | None:
    url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sym},day,,,{max(days, 500)},qfq")
    from strategies.data.fetcher import fetch_url, TENCENT_HEADERS
    raw = fetch_url(url, headers={**TENCENT_HEADERS, "Referer": "https://gu.qq.com"},
                    timeout=12)
    if not raw or len(raw) < 50:
        return None
    try:
        parsed = json.loads(raw)
        data = parsed.get("data", {})
        key = next((k for k in data if sym in k), None)
        if not key:
            return None
        rows = data[key].get("qfqday", data[key].get("day", []))
        out = []
        for e in rows:
            if len(e) >= 6:
                try:
                    out.append({"date": str(e[0]), "open": float(e[1]), "close": float(e[2]),
                                "high": float(e[3]), "low": float(e[4]),
                                "volume": float(e[5]) if e[5] else 0.0})
                except (ValueError, IndexError):
                    continue
        return out or None
    except Exception:
        return None


def build_market_cache(cfg: dict) -> dict:
    """构建 {universe:[...], klines:{code:[bars]}} 并落盘。"""
    fetcher = DataFetcher({})
    log("▶ 拉取全市场股票列表 (TDX 优先 / 新浪回退)...")
    all_stocks = []
    for attempt in range(4):
        all_stocks = fetcher.get_all_a_stocks() or []
        if all_stocks:
            break
        log(f"  [RETRY {attempt+1}/4] 空列表, 5s 后重试")
        time.sleep(5)
    log(f"  全市场 {len(all_stocks)} 只")

    keep = [s for s in all_stocks
            if not is_st(s.get("name", "")) and s.get("price", 0) > 0]
    log(f"  剔除 ST/退市/停牌后: {len(keep)}")

    max_mv = cfg.get("max_mv", 0)
    if max_mv:
        log("▶ 拉取市值/股本 (用于市值过滤)...")
        md = fetcher.get_market_data(keep, batch_size=cfg.get("tencent_batch_size", 80))
        min_sh = cfg.get("min_total_shares", 0)
        max_sh = cfg.get("max_total_shares", 0)
        flt = []
        for s in keep:
            m = md.get(s["code"])
            if not m:
                continue
            mv = m.get("mv", 0)
            if mv <= 0 or mv > max_mv:
                continue
            ts = m.get("total_shares", 0)
            if min_sh and ts < min_sh:
                continue
            if max_sh and ts > max_sh:
                continue
            flt.append(s)
        log(f"  市值/股本过滤后: {len(flt)}")
        keep = flt

    keep.sort(key=lambda x: x.get("amount", 0), reverse=True)
    universe = keep[: cfg["universe_size"]]
    log(f"  候选池(按成交额): {len(universe)} 只")

    syms = [f"{code_to_prefix(s['code'])}{s['code']}"
            for s in universe if code_to_prefix(s["code"])]
    src = cfg.get("data_source", "tdx")
    log(f"▶ 批量拉取 {len(syms)} 只日K (source={src}, {cfg['kline_days']} 根)...")
    t0 = time.time()
    kd_map = {}
    if src == "sina":
        from strategies.backtest_ma20_turn_pullback import sina_fetch_one
        n_days = cfg.get("sina_datalen", 1000)
        for i, sym in enumerate(syms):
            kd = sina_fetch_one(sym, datalen=n_days)
            if kd and len(kd) >= 60:
                kd_map[sym] = [{"date": k["date"], "open": k.get("open", 0),
                                "high": k.get("high", 0), "low": k.get("low", 0),
                                "close": k["close"], "volume": k.get("volume", 0)}
                               for k in kd]
            if (i + 1) % 100 == 0:
                log(f"  sina {i+1}/{len(syms)}, ok={len(kd_map)}")
            time.sleep(cfg.get("kline_delay", 0.05))
        log(f"  sina 返回 {len(kd_map)} 只, 耗时 {time.time()-t0:.1f}s")
    else:
        kd_map = tdx_batch_klines(syms, cfg["kline_days"])
        log(f"  TDX 返回 {len(kd_map)} 只, 耗时 {time.time()-t0:.1f}s")

    miss = [s for s in syms if s not in kd_map]
    if miss:
        log(f"  TDX 缺失 {len(miss)} 只 → 腾讯回退(限 {cfg.get('fallback_limit', 60)} 只)...")
        for sym in miss[: cfg.get("fallback_limit", 60)]:
            kd = tencent_one_kline(sym, cfg["kline_days"])
            if kd and len(kd) >= 60:
                kd_map[sym] = kd

    klines = {}
    for s in universe:
        sym = f"{code_to_prefix(s['code'])}{s['code']}"
        bars = kd_map.get(sym)
        if bars and len(bars) >= WARMUP_BARS + 40:
            klines[s["code"]] = bars
    log(f"  K线可用: {len(klines)} 只")

    return {"universe": universe, "klines": klines,
            "built_at": datetime.now().isoformat(timespec="seconds")}


def load_or_build_cache(cfg: dict) -> dict:
    path = cfg.get("cache")
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("klines") and data.get("universe"):
                log(f"[CACHE HIT] {path}: universe={len(data['universe'])}, "
                    f"klines={len(data['klines'])}")
                return data
        except Exception as e:
            log(f"  [WARN] 缓存读取失败: {e}")
    data = build_market_cache(cfg)
    if path:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            log(f"[CACHE SAVE] {path}")
        except Exception as e:
            log(f"  [WARN] 缓存写入失败: {e}")
    return data


# ══════════════════════════════════════════════════════════════════
#  信号引擎：为每只股票预计算全部买入信号日
# ══════════════════════════════════════════════════════════════════

def compute_signals(code: str, bars: list[dict], cfg: dict) -> dict[str, Any]:
    """返回 {dates, ma5, ma20, signals:{date: info}}"""
    closes = [b["close"] for b in bars]
    n = len(closes)
    ma5 = moving_average(closes, 5)
    ma20 = moving_average(closes, 20)
    dates = [b["date"] for b in bars]

    lookback = cfg["turn_lookback"]
    band = cfg["band"]
    rthr = cfg["rally_thresh"]
    rmode = cfg["rally_mode"]

    signals: dict[str, dict] = {}
    for t in range(max(WARMUP_BARS, 22), n):
        # ── ① MA20 由跌转升：找最近拐点 j ──
        turn_idx = None
        lo = max(t - lookback, 21)
        for j in range(t - 1, lo - 1, -1):
            if ma20[j] > ma20[j - 1] and ma20[j - 1] <= ma20[j - 2]:
                turn_idx = j
                break
        if turn_idx is None:
            continue
        # ── ② MA20 转升以来未创新低 ──
        if ma20[t] < ma20[turn_idx]:
            continue
        # ── ③ 区间内至少一次 >8 个点上涨 ──
        if rmode == "single":
            rally = max(closes[k] / closes[k - 1] - 1.0
                        for k in range(turn_idx + 1, t + 1) if closes[k - 1] > 0)
        else:  # leg
            seg = closes[turn_idx:t + 1]
            lo_c, hi_c = min(seg), max(seg)
            rally = (hi_c / lo_c - 1.0) if lo_c > 0 else 0.0
        if rally <= rthr:
            continue
        # ── ④ 回落到 MA20 附近 ──
        m20 = ma20[t]
        if m20 <= 0:
            continue
        dev = closes[t] / m20 - 1.0
        if not (-band <= dev <= band):
            continue

        signals[dates[t]] = {
            "turn_idx": turn_idx,
            "turn_days_ago": t - turn_idx,
            "rally_pct": round(rally * 100, 2),
            "dev_ma20_pct": round(dev * 100, 2),
            "ma5": round(ma5[t], 3),
            "ma20": round(m20, 3),
        }
    return {"dates": dates, "ma5": ma5, "ma20": ma20, "signals": signals,
            "bars": bars}


# ══════════════════════════════════════════════════════════════════
#  组合回测
# ══════════════════════════════════════════════════════════════════

class Pos:
    __slots__ = ("code", "name", "buy_date", "buy_price", "shares", "armed",
                 "peak", "buy_idx", "half_done", "cost")

    def __init__(self, code, name, buy_date, buy_price, shares, buy_idx, peak):
        self.code = code
        self.name = name
        self.buy_date = buy_date
        self.buy_price = buy_price
        self.shares = shares
        self.buy_idx = buy_idx
        self.armed = False
        self.peak = peak
        self.half_done = False
        self.cost = shares * buy_price


def run_portfolio(sig_map: dict[str, dict], cfg: dict) -> dict[str, Any]:
    fee = cfg["fee"]
    capital = cfg["capital"]
    slots = cfg["slots"]
    per_pos = capital / slots

    # ── 全局交易日轴 ──
    all_dates = sorted({d for s in sig_map.values() for d in s["dates"]})
    if len(all_dates) < 60:
        raise SystemExit(f"ERROR: 交易日不足 ({len(all_dates)} 天), 无法回测")
    idx_of = {d: i for i, d in enumerate(all_dates)}
    for code, s in sig_map.items():
        s["d2i"] = {d: i for i, d in enumerate(s["dates"])}

    # ── 回测窗口 = 最后 cfg['days'] 个交易日 ──
    days = min(cfg["days"], len(all_dates))
    start_i = len(all_dates) - days
    win_dates = all_dates[start_i:]
    if cfg.get("window_start"):
        win_dates = [d for d in win_dates if d >= cfg["window_start"]]
    if cfg.get("window_end"):
        win_dates = [d for d in win_dates if d <= cfg["window_end"]]
    if len(win_dates) < 60:
        raise SystemExit(f"ERROR: 窗口内交易日不足 ({len(win_dates)})")

    # 每只股票在窗口内的 bar 索引区间
    for code, s in sig_map.items():
        s["w_start"] = None
        s["w_end"] = None

    cash = capital
    positions: list[Pos] = []
    trades: list[dict] = []
    signal_hits = 0
    skipped_no_cash = 0
    skipped_no_slot = 0
    skipped_held = 0

    nav_series: list[float] = []
    nav_dates: list[str] = []
    last_close: dict[str, float] = {}
    n_buys = 0

    for di, date in enumerate(win_dates):
        # ────── 1) 卖出检查（T+1：买入当日不卖） ──────
        still_open = []
        for p in positions:
            if date == p.buy_date:                      # T+1
                still_open.append(p)
                continue
            s = sig_map.get(p.code)
            if not s:
                still_open.append(p)
                continue
            i = s["d2i"].get(date)
            if i is None:                               # 停牌
                still_open.append(p)
                continue
            close = s["bars"][i]["close"]
            last_close[p.code] = close
            ma5, ma20 = s["ma5"][i], s["ma20"][i]
            if close > p.peak:
                p.peak = close
            if ma5 > 0 and close >= ma5:
                p.armed = True

            # (b) 跌破 MA20 → 全部卖出（最终退出，优先级高于卖半）
            if ma20 > 0 and close < ma20 * (1 - cfg.get("ma20_buffer", 0.0)):
                trades.append(_mk_trade(p, date, close, p.shares, "MA20_break", fee))
                cash += p.shares * close * (1 - fee)
                continue
            # (a) armed 后首次 close <= MA5 → 卖出一半
            if (not p.half_done) and ma5 > 0 and close <= ma5 and (
                    p.armed or not cfg.get("require_arm", True)):
                half = int(p.shares // 2 // 100) * 100
                if half <= 0:
                    half = p.shares
                trades.append(_mk_trade(p, date, close, half, "MA5_half", fee))
                cash += half * close * (1 - fee)
                p.shares -= half
                p.half_done = True
                if p.shares > 0:
                    still_open.append(p)
                continue
            still_open.append(p)
        positions = still_open

        # ────── 2) 买入检查 ──────
        for code, s in sig_map.items():
            if date not in s["signals"]:
                continue
            signal_hits += 1
            if any(p.code == code for p in positions):
                skipped_held += 1
                continue
            if len(positions) >= slots:
                skipped_no_slot += 1
                continue
            i = s["d2i"][date]
            bar = s["bars"][i]
            close = bar["close"]
            # 涨停无法买入
            if i > 0:
                prev = s["bars"][i - 1]["close"]
                if prev > 0 and close >= prev * (1 + get_limit_pct(code)) * 0.995:
                    continue
            budget = min(per_pos, cash)
            shares = int(budget / (close * (1 + fee)) // 100) * 100
            if shares <= 0:
                skipped_no_cash += 1
                continue
            cost = shares * close * (1 + fee)
            if cost > cash:
                skipped_no_cash += 1
                continue
            cash -= cost
            positions.append(Pos(code, s.get("name", code), date, close, shares, di,
                                 s["peak_seed"] if "peak_seed" in s else close))
            last_close[code] = close
            n_buys += 1

        # ────── 3) 每日净值 ──────
        mv = 0.0
        for p in positions:
            mv += p.shares * last_close.get(p.code, p.buy_price)
        nav_series.append(cash + mv)
        nav_dates.append(date)

    # ── 期末强制平仓 ──
    last_date = win_dates[-1]
    for p in positions:
        s = sig_map.get(p.code)
        close = last_close.get(p.code, p.buy_price)
        if s:
            i = s["d2i"].get(last_date)
            if i is not None:
                close = s["bars"][i]["close"]
        trades.append(_mk_trade(p, last_date, close, p.shares, "end_of_backtest", fee))
        cash += p.shares * close * (1 - fee)
        p.shares = 0

    return {
        "trades": trades, "nav_series": nav_series, "nav_dates": nav_dates,
        "final_cash": cash, "signal_hits": signal_hits, "n_buys": n_buys,
        "skipped_no_slot": skipped_no_slot, "skipped_no_cash": skipped_no_cash,
        "skipped_held": skipped_held, "win_dates": win_dates,
        "initial": capital,
    }


def _mk_trade(p: Pos, sell_date: str, sell_price: float, shares: int,
              reason: str, fee: float) -> dict:
    gross = (sell_price - p.buy_price) / p.buy_price
    net = gross - 2 * fee                    # 双边手续费
    try:
        d0 = datetime.strptime(p.buy_date, "%Y-%m-%d")
        d1 = datetime.strptime(sell_date, "%Y-%m-%d")
        hold = (d1 - d0).days
    except ValueError:
        hold = 0
    return {
        "code": p.code, "name": p.name, "buy_date": p.buy_date,
        "sell_date": sell_date, "buy_price": round(p.buy_price, 3),
        "sell_price": round(sell_price, 3), "shares": shares,
        "return_pct": round(net * 100, 2), "holding_days": hold, "reason": reason,
        "peak_gain_pct": round((p.peak / p.buy_price - 1) * 100, 2),
    }


def summarize(res: dict[str, Any], cfg: dict, sig_map: dict[str, dict]) -> dict:
    trades = res["trades"]
    nav = res["nav_series"]
    n = len(trades)
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]

    # ── 仓位级聚合（按 股票+买入日 合并卖半/清仓记录），这是不受资金槽位
    #    干扰的策略真实 edge 指标；按卖出记录统计会被"卖半"记录重复加权 ──
    pos_map: dict[tuple, dict] = {}
    for t in trades:
        k = (t["code"], t["buy_date"])
        e = pos_map.setdefault(k, {"ret_num": 0.0, "w": 0, "hold": t["holding_days"],
                                   "reasons": [], "peak": t["peak_gain_pct"],
                                   "name": t["name"]})
        e["ret_num"] += t["return_pct"] * t["shares"]
        e["w"] += t["shares"]
        e["reasons"].append(t["reason"])
        e["hold"] = max(e["hold"], t["holding_days"])
    pos_rets = []
    for e in pos_map.values():
        pos_rets.append(e["ret_num"] / e["w"] if e["w"] else 0.0)
    npos = len(pos_rets)
    pos_wins = [r for r in pos_rets if r > 0]
    pos_losses = [r for r in pos_rets if r <= 0]

    final = nav[-1] if nav else res["initial"]
    total_ret = (final / res["initial"] - 1) * 100

    peak = res["initial"]
    dd = 0.0
    for v in nav:
        if v > peak:
            peak = v
        dd = max(dd, (peak - v) / peak)

    sharpe = 0.0
    if len(nav) > 2:
        import statistics
        dr = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]
        sd = statistics.stdev(dr) if len(dr) > 1 else 0.0
        if sd > 1e-12:
            sharpe = (sum(dr) / len(dr)) / sd * (252 ** 0.5)

    years = max(len(nav) / 252.0, 1e-9)
    cagr = ((final / res["initial"]) ** (1 / years) - 1) * 100

    exit_cnt = Counter(t["reason"] for t in trades)
    exit_stats = {}
    for r in exit_cnt:
        rs = [t["return_pct"] for t in trades if t["reason"] == r]
        exit_stats[r] = {"n": len(rs), "avg": round(sum(rs) / len(rs), 2),
                         "win_rate": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1)}

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    pf = (avg_win / abs(avg_loss)) if (wins and losses and avg_loss != 0) else 0.0

    total_signals = sum(len(s["signals"]) for s in sig_map.values())

    # ── 基准：候选池等权买入持有（区间内 close_end/close_start 均值）──
    bm_rets = []
    for code, s in sig_map.items():
        d2i = s["d2i"]
        first, last = None, None
        for d in res["win_dates"]:
            if d in d2i:
                if first is None:
                    first = s["bars"][d2i[d]]["close"]
                last = s["bars"][d2i[d]]["close"]
        if first and last and first > 0:
            bm_rets.append((last / first - 1) * 100)
    benchmark = {
        "equal_weight_universe_buyhold_pct": round(sum(bm_rets) / len(bm_rets), 2)
                                             if bm_rets else 0.0,
        "universe_win_pct": round(sum(1 for r in bm_rets if r > 0) / len(bm_rets) * 100, 1)
                            if bm_rets else 0.0,
        "n_stocks_measured": len(bm_rets),
    }

    return {
        "config": {k: v for k, v in cfg.items() if k != "cache"},
        "period": f"{res['win_dates'][0]} → {res['win_dates'][-1]}",
        "trading_days": len(nav),
        "universe_size": len(sig_map),
        "total_signals_generated": total_signals,
        "signal_hits_in_window": res["signal_hits"],
        "buys_executed": res["n_buys"],
        "skipped_no_slot": res["skipped_no_slot"],
        "skipped_no_cash": res["skipped_no_cash"],
        "skipped_already_held": res["skipped_held"],
        # ── 仓位级（策略 edge，资金无关） ──
        "positions_closed": npos,
        "pos_win_rate_pct": round(len(pos_wins) / npos * 100, 2) if npos else 0.0,
        "pos_avg_return_pct": round(sum(pos_rets) / npos, 2) if npos else 0.0,
        "pos_median_return_pct": round(sorted(pos_rets)[npos // 2], 2) if npos else 0.0,
        "pos_avg_win_pct": round(sum(pos_wins) / len(pos_wins), 2) if pos_wins else 0.0,
        "pos_avg_loss_pct": round(sum(pos_losses) / len(pos_losses), 2) if pos_losses else 0.0,
        "pos_profit_factor": round((sum(pos_wins) / len(pos_wins)) /
                                   abs(sum(pos_losses) / len(pos_losses)), 2)
                                  if (pos_wins and pos_losses) else 0.0,
        "pos_avg_holding_days": round(sum(e["hold"] for e in pos_map.values()) / npos, 1)
                                if npos else 0.0,
        # ── 卖出记录级 ──
        "closed_trades": n,
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else 0.0,
        "avg_return_per_trade_pct": round(sum(rets) / n, 2) if n else 0.0,
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(pf, 2),
        "avg_holding_days": round(sum(t["holding_days"] for t in trades) / n, 1) if n else 0,
        # ── 组合层 ──
        "final_equity": round(final, 2),
        "total_return_pct": round(total_ret, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "benchmark": benchmark,
        "exit_reason_stats": exit_stats,
        "exit_reason_counts": dict(exit_cnt),
        "trades": trades,
        "nav_dates": res["nav_dates"],
        "nav_series": [round(v, 2) for v in nav],
    }


# ══════════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="MA20转升+8点上涨+回踩MA20 策略回测")
    ap.add_argument("--universe", type=int, default=1000)
    ap.add_argument("--days", type=int, default=480)
    ap.add_argument("--kline-days", type=int, default=500)
    ap.add_argument("--turn-lookback", type=int, default=15)
    ap.add_argument("--band", type=float, default=0.03)
    ap.add_argument("--ma20-buffer", type=float, default=0.0,
                    help="跌破 MA20 的容差（0=你的原始规则：close<MA20 即清仓）")
    ap.add_argument("--rally-thresh", type=float, default=0.08)
    ap.add_argument("--rally-mode", choices=["single", "leg"], default="single")
    ap.add_argument("--no-arm", action="store_true",
                    help="卖半不要求价格先站上MA5（买入后首次 close<=MA5 即卖半）")
    ap.add_argument("--capital", type=float, default=150000)
    ap.add_argument("--slots", type=int, default=3)
    ap.add_argument("--fee", type=float, default=0.005)
    ap.add_argument("--max-mv", type=float, default=0, help="0=不过滤")
    ap.add_argument("--min-total-shares", type=float, default=0)
    ap.add_argument("--max-total-shares", type=float, default=0)
    ap.add_argument("--fallback-limit", type=int, default=60)
    ap.add_argument("--data-source", choices=["tdx", "sina"], default="tdx")
    ap.add_argument("--sina-datalen", type=int, default=1000)
    ap.add_argument("--window-start", type=str, default=None)
    ap.add_argument("--window-end", type=str, default=None)
    ap.add_argument("--cache", type=str, default=None)
    ap.add_argument("--save", "-s", action="store_true")
    ap.add_argument("--output", "-o", type=str, default="ma20_surge_pullback.json")
    args = ap.parse_args()

    cfg = {
        "universe_size": args.universe, "days": args.days,
        "kline_days": args.kline_days, "turn_lookback": args.turn_lookback,
        "band": args.band, "rally_thresh": args.rally_thresh,
        "ma20_buffer": args.ma20_buffer,
        "rally_mode": args.rally_mode, "capital": args.capital,
        "slots": args.slots, "fee": args.fee, "max_mv": args.max_mv,
        "require_arm": not args.no_arm,
        "min_total_shares": args.min_total_shares,
        "max_total_shares": args.max_total_shares,
        "fallback_limit": args.fallback_limit, "cache": args.cache,
        "data_source": args.data_source, "sina_datalen": args.sina_datalen,
        "window_start": args.window_start, "window_end": args.window_end,
    }

    t0 = time.time()
    mkt = load_or_build_cache(cfg)
    universe, klines = mkt["universe"], mkt["klines"]
    name_map = {s["code"]: s.get("name", s["code"]) for s in universe}

    log(f"▶ 预计算信号 (rally_mode={cfg['rally_mode']})...")
    sig_map = {}
    for code, bars in klines.items():
        s = compute_signals(code, bars, cfg)
        s["name"] = name_map.get(code, code)
        sig_map[code] = s
    log(f"  信号股票数 {sum(1 for s in sig_map.values() if s['signals'])}"
        f" / {len(sig_map)}")

    res = run_portfolio(sig_map, cfg)
    out = summarize(res, cfg, sig_map)
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["data_built_at"] = mkt.get("built_at", "")

    print("\n" + "=" * 72)
    print(f"  MA20转升 + >{cfg['rally_thresh']*100:.0f}点上涨({cfg['rally_mode']}) "
          f"+ 回踩MA20  |  卖半={'armed' if cfg.get('require_arm', True) else 'immediate'}"
          f"  |  MA20止损容差={cfg.get('ma20_buffer', 0.0)*100:.0f}%")
    print("=" * 72)
    for k in ("period", "universe_size", "signal_hits_in_window", "buys_executed",
              "skipped_no_slot", "skipped_already_held",
              "positions_closed", "pos_win_rate_pct", "pos_avg_return_pct",
              "pos_median_return_pct", "pos_avg_win_pct", "pos_avg_loss_pct",
              "pos_profit_factor", "pos_avg_holding_days",
              "final_equity", "total_return_pct", "cagr_pct",
              "max_drawdown_pct", "sharpe"):
        print(f"  {k:<28s} {out[k]}")
    bm = out["benchmark"]
    print(f"  基准(候选池等权买入持有)        {bm['equal_weight_universe_buyhold_pct']}%"
          f"  (盈面 {bm['universe_win_pct']}%, n={bm['n_stocks_measured']})")
    print("  -- 退出原因(按卖出记录) --")
    for r, v in out["exit_reason_stats"].items():
        print(f"    {r:<18s} n={v['n']:<5d} avg={v['avg']:>7.2f}%  win={v['win_rate']}%")
    print(f"  elapsed {out['elapsed_s']}s")

    if args.save:
        d = os.path.join(PROJECT_DIR, "backtest_results")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, args.output)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"  saved → {p}")


if __name__ == "__main__":
    main()
