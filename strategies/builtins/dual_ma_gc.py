"""
Dual-MA Golden Cross Strategy v2 — MA5上穿MA18严格金叉

Core logic:
1. Strict golden cross: today MA5 > MA18 AND yesterday MA5 <= MA18
2. MV < 1000亿, 总股本 0.5~10亿
3. Sell rules (implemented in backtest):
   - price < buy_price × 0.94 → sell all (hard stop loss)
   - price > MA5 × 1.3 → sell half (take profit)
   - price < MA5 → sell half (stop loss)
   - price < MA18 → sell all (hard stop)
"""

import time
from typing import Any

from strategies.base import StrategyBase, StrategyContext
from strategies.registry import register_strategy
from strategies.data.fetcher import log, code_to_prefix


@register_strategy
class DualMAGoldenCross(StrategyBase):
    name = "dual_ma_gc"
    description = (
        "MA5上穿MA18严格金叉. "
        "筛选: 市值<1000亿, 总股本0.5~10亿."
    )

    def run(self, context: StrategyContext) -> list[dict[str, Any]]:
        cfg = context.config
        max_mv = cfg.get("max_mv", 1000)
        min_shares = cfg.get("min_total_shares", 0.5)
        max_shares = cfg.get("max_total_shares", 10)
        ma_short = cfg.get("ma_short", 5)
        ma_long = cfg.get("ma_long", 18)
        top_n = cfg.get("top_n", 30)
        kline_limit = cfg.get("kline_check_limit", 150)
        delay = context.engine_config.get("kline_request_delay", 0.3)

        # ── Step 1: Basic filters ──────────────────
        candidates = []
        for s in context.all_stocks:
            code = s.get("code", "")
            md = context.market_data.get(code)
            if not md:
                continue
            name = md.get("name", "")
            mv = md.get("mv", 0)
            total_shares = md.get("total_shares", 0)

            if name.startswith(("ST", "*ST", "S")) or "退" in name:
                continue
            if mv <= 0 or mv >= max_mv:
                continue
            if total_shares <= 0 or total_shares < min_shares or total_shares > max_shares:
                continue

            candidates.append({
                "code": code,
                "name": name,
                "price": md.get("price", 0),
                "mv": mv,
                "total_shares": total_shares,
                "changepercent": s.get("changepercent", 0),
            })

        log(f"  After basic filters: {len(candidates)} stocks")

        if not candidates:
            return []

        # ── Step 2: Priority sort — momentum first ──
        def priority(c):
            score = max(c["changepercent"], 0) * 5
            return score

        candidates.sort(key=priority, reverse=True)
        check_list = candidates[:kline_limit]
        log(f"  Fetching klines for top {len(check_list)} candidates...")

        # ── Step 3: Kline analysis — strict MA5上穿MA18 ────
        gc_stocks = []
        for i, c in enumerate(check_list):
            code = c["code"]
            prefix = code_to_prefix(code)
            if not prefix:
                continue

            kd = context.get_kline(code)
            if kd is None:
                from strategies.data.fetcher import DataFetcher
                fetcher = DataFetcher(context.engine_config)
                sym = f"{prefix}{code}"
                kd = fetcher.get_kline(sym)
                if kd is not None:
                    context.klines[code] = kd

            if kd is None or len(kd) < ma_long + 5:
                continue

            closes = [d["close"] for d in kd]
            n = len(closes)
            ma_s = sum(closes[-ma_short:]) / ma_short
            ma_l = sum(closes[-ma_long:]) / ma_long

            # Previous MAs (yesterday)
            if n >= ma_short + 1:
                ma_s_prev = sum(closes[-(ma_short + 1):-1]) / ma_short
            else:
                ma_s_prev = ma_s
            if n >= ma_long + 1:
                ma_l_prev = sum(closes[-(ma_long + 1):-1]) / ma_long
            else:
                ma_l_prev = ma_l

            # ── Strict golden cross: yesterday NO, today YES ───────
            if not (ma_s_prev <= ma_l_prev and ma_s > ma_l):
                continue

            gc_stocks.append({
                "code": code,
                "name": c["name"],
                "price": round(closes[-1], 2),
                "mv": c["mv"],
                "total_shares": c["total_shares"],
                "changepercent": c["changepercent"],
                f"ma{ma_short}": round(ma_s, 2),
                f"ma{ma_long}": round(ma_l, 2),
            })

            if (i + 1) % 30 == 0:
                log(f"  Kline: {i+1}/{len(check_list)}, GC found: {len(gc_stocks)}")
                if delay > 0:
                    time.sleep(1.5)

            if delay > 0 and (i + 1) % 30 != 0:
                time.sleep(delay)

        # ── Step 4: Sort — small cap first ────────────
        gc_stocks.sort(key=lambda x: x["mv"])
        gc_stocks = gc_stocks[:top_n]

        log(f"  Final selections: {len(gc_stocks)} stocks")
        return gc_stocks
