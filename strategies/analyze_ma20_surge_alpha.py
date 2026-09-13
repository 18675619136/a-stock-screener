#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信号前瞻收益研究 (signal alpha study)
=====================================
回答：MA20转升 + 8点上涨 + 回踩MA20 这个"入场信号"本身有没有预测力？

方法：对窗口内每一条买入信号日，计算买入后 +5/+10/+20 个交易日的
      前向收益（收盘价对收盘价），并与"全样本无条件漂移"（同窗口所有
      股票所有交易日的同长度前向收益）对比。

      信号均值 - 无条件均值 = 入场信号带来的超额（alpha）。
      这一步与卖出规则无关，可独立判断"是入场不行还是出场不行"。
"""
import argparse
import json
import os
import sys
import importlib

sys.path.insert(0, "/home/super-user/screening")
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))

_base = importlib.import_module("strategies.backtest_ma20_surge_pullback")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--universe", type=int, default=800)
    ap.add_argument("--kline-days", type=int, default=500)
    ap.add_argument("--rally-mode", choices=["single", "leg"], default="leg")
    ap.add_argument("--band", type=float, default=0.03)
    ap.add_argument("--turn-lookback", type=int, default=15)
    ap.add_argument("--window-start", default=None)
    ap.add_argument("--window-end", default=None)
    ap.add_argument("--horizons", default="5,10,20")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    cfg = dict(universe_size=a.universe, days=9999, kline_days=a.kline_days,
               turn_lookback=a.turn_lookback, band=a.band, rally_thresh=0.08,
               rally_mode=a.rally_mode, capital=150000, slots=3, fee=0.005,
               max_mv=0, min_total_shares=0, max_total_shares=0,
               fallback_limit=60, cache=a.cache, require_arm=True,
               ma20_buffer=0.0, data_source="tdx", sina_datalen=1000,
               window_start=a.window_start, window_end=a.window_end)
    mkt = _base.load_or_build_cache(cfg)
    nm = {s["code"]: s.get("name", s["code"]) for s in mkt["universe"]}
    hs = [int(x) for x in a.horizons.split(",")]

    sig_fwd = {h: [] for h in hs}
    base_fwd = {h: [] for h in hs}
    n_sig = 0
    n_days = 0

    for code, bars in mkt["klines"].items():
        s = _base.compute_signals(code, bars, cfg)
        s["name"] = nm.get(code, code)
        dates, closes = s["dates"], [b["close"] for b in bars]
        d2i = {d: i for i, d in enumerate(dates)}
        lo = a.window_start or dates[0]
        hi = a.window_end or dates[-1]
        sig_days = {d2i[d] for d in s["signals"] if lo <= d <= hi and d in d2i}
        n_sig += len(sig_days)
        for i in range(len(closes)):
            if not (lo <= dates[i] <= hi):
                continue
            n_days += 1
            for h in hs:
                if i + h >= len(closes) or closes[i] <= 0:
                    continue
                r = (closes[i + h] / closes[i] - 1) * 100
                base_fwd[h].append(r)
                if i in sig_days:
                    sig_fwd[h].append(r)

    def stat(v):
        if not v:
            return None
        v2 = sorted(v)
        return {"n": len(v), "mean": round(sum(v) / len(v), 3),
                "median": round(v2[len(v2) // 2], 3),
                "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100, 1)}

    res = {"rally_mode": a.rally_mode, "signals": n_sig, "sample_days": n_days,
           "window": [a.window_start, a.window_end], "horizons": {}}
    print("=" * 74)
    print(f"  信号前瞻收益研究  rally_mode={a.rally_mode}  信号数={n_sig}  "
          f"样本日={n_days}")
    print(f"  窗口 {a.window_start} → {a.window_end}")
    print("=" * 74)
    print(f"  {'H':>4}  {'信号均值':>9} {'信号中位':>9} {'胜率':>7} | "
          f"{'全样本均值':>10} {'全样本中位':>10} {'胜率':>7} | {'超额(均值)':>10}")
    for h in hs:
        s1, b1 = stat(sig_fwd[h]), stat(base_fwd[h])
        if not s1 or not b1:
            continue
        exc = s1["mean"] - b1["mean"]
        print(f"  {h:>4}  {s1['mean']:>8.2f}% {s1['median']:>8.2f}% {s1['win_rate']:>6.1f}% | "
              f"{b1['mean']:>9.2f}% {b1['median']:>9.2f}% {b1['win_rate']:>6.1f}% | "
              f"{exc:>+9.2f}%")
        res["horizons"][str(h)] = {"signal": s1, "baseline": b1, "excess_mean": round(exc, 3)}

    if a.out:
        p = os.path.join("/home/super-user/screening/backtest_results", a.out)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"  saved → {p}")


if __name__ == "__main__":
    main()
