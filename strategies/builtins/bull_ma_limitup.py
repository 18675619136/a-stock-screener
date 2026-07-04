"""
Bull MA + Surge Strategy v2 — 价格>MA5>MA18 + 近20日涨幅>8%

Core logic:
1. 价格 > MA5 > MA18 (收盘价在5日均线上方，5日线在18日线上方)
2. 近20个交易日内至少有一次涨幅>8%
3. MV < 1000亿, 总股本 0.5~10亿
4. Sell rules (implemented in backtest):
   - price < buy_price × 0.94 → sell all (hard stop loss)
   - price > MA5 × 1.3 → sell half (take profit)
   - price < MA5 → sell half (stop loss)
   - price < MA18 → sell all (hard stop)
"""

import time
from typing import Any

from strategies.base import StrategyBase, StrategyContext
from strategies.registry import register_strategy
from strategies.data.fetcher import log, code_to_prefix, match_stock_to_hot_sector, DataFetcher


@register_strategy
class BullMALimitUpStrategy(StrategyBase):
    name = "bull_ma_limitup"
    description = (
        "价格>MA5>MA18 + 近20日涨幅>8%. "
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
        lookback_days = cfg.get("lookback_days", 20)
        surge_threshold = cfg.get("surge_threshold", 8.0)
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

        # ── Step 2: Sort by changepercent, take top ──
        candidates.sort(key=lambda x: x.get("changepercent", 0), reverse=True)
        check_list = candidates[:kline_limit]
        log(f"  Fetching klines for top {len(check_list)} candidates...")

        # ── Step 3: Kline analysis — price>MA5>MA18 + 涨幅>8% ────
        selected = []
        for i, c in enumerate(check_list):
            code = c["code"]
            prefix = code_to_prefix(code)
            if not prefix:
                continue

            kd = context.get_kline(code)
            if kd is None:
                fetcher = DataFetcher(context.engine_config)
                sym = f"{prefix}{code}"
                kd = fetcher.get_kline(sym)
                if kd is not None:
                    context.klines[code] = kd

            if kd is None or len(kd) < ma_long + 5:
                continue

            closes = [d["close"] for d in kd]
            n = len(closes)
            close = closes[-1]

            # Calculate MAs
            ma5_val = sum(closes[-ma_short:]) / ma_short
            ma18_val = sum(closes[-ma_long:]) / ma_long

            # Condition 1: 价格 > MA5 > MA18
            if not (close > ma5_val > ma18_val):
                continue

            # Condition 2: 近N个交易日内至少有一次涨幅>8%
            lookback = min(lookback_days + 1, n)
            has_surge = False
            surge_day = ""
            for j in range(1, lookback):
                prev_close = closes[-(j + 1)]
                curr_close = closes[-j]
                if prev_close <= 0:
                    continue
                pct = (curr_close - prev_close) / prev_close * 100
                if pct >= surge_threshold:
                    has_surge = True
                    surge_day = kd[-(j)]["date"]
                    break

            if not has_surge:
                continue

            selected.append({
                "code": code,
                "name": c["name"],
                "price": round(close, 2),
                "mv": c["mv"],
                "total_shares": c["total_shares"],
                "changepercent": c["changepercent"],
                "ma5": round(ma5_val, 2),
                "ma18": round(ma18_val, 2),
                "surge_date": surge_day,
            })

            if (i + 1) % 30 == 0:
                log(f"  Kline: {i+1}/{len(check_list)}, selected: {len(selected)}")
                if delay > 0:
                    time.sleep(1.5)

            if delay > 0 and (i + 1) % 30 != 0:
                time.sleep(delay)

        # ── Step 4: Sort by hot sector strength ────────
        if selected:
            fetcher = DataFetcher(context.engine_config)
            hot_sectors = fetcher.fetch_hot_sectors_with_strength(top_k=30)
            if hot_sectors:
                top5 = ", ".join(s["name"] for s in hot_sectors[:5])
                log(f"  Hot sectors (by 涨跌比): {top5}...")
                for s in selected:
                    sec_name, sec_strength = match_stock_to_hot_sector(
                        s["name"], hot_sectors
                    )
                    s["sector"] = sec_name
                    s["sector_strength"] = sec_strength
                # Sort: sector strength desc, then mv asc within same sector
                selected.sort(key=lambda x: (-x["sector_strength"], x["mv"]))
                matched = sum(1 for s in selected if s["sector_strength"] > 0)
                log(f"  Sector-matched: {matched}/{len(selected)} stocks")
            else:
                log("  Failed to fetch hot sectors, falling back to mv sort")
                selected.sort(key=lambda x: x["mv"])
        selected = selected[:top_n]

        log(f"  Final selections: {len(selected)} stocks")
        return selected
