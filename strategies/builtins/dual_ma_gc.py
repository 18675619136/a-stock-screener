"""
Dual-MA Golden Cross Strategy v3 — 近期金叉 + 质量评分

买入逻辑:
1. MA5 > MA18 (均线多头排列, 已形成金叉)
2. 金叉发生在最近 gc_lookback_days 个交易日内（默认3天）
3. 成交量 > 0（至少正常交易）
4. MV < 1000亿, 总股本 0.5~10亿
5. 排除 ST/*ST/退市

评分排序:
- 均线强度: MA5与MA18的偏离度
- 放量程度: 当日量比
- 赛道强度: 热门赛道涨跌比
- 小市值加分

卖出条件（在回测中实现统一四层卖出）:
1. price < buy_price × 0.94 → 全卖（硬止损）
2. price < MA18 → 全卖（趋势破位）
3. price > MA5 × 1.3 → 卖一半（止盈，极难触发）
4. price < MA5 → 卖一半（半仓止损）
"""

import time
from typing import Any

from strategies.base import StrategyBase, StrategyContext
from strategies.registry import register_strategy
from strategies.data.fetcher import log, code_to_prefix, match_stock_to_hot_sector, DataFetcher


@register_strategy
class DualMAGoldenCross(StrategyBase):
    name = "dual_ma_gc"
    display_name = "近期金叉质量 v3"
    description = (
        "MA5>MA18近期金叉 + 均线强度/放量/赛道评分. "
        "筛选: 市值<1000亿, 总股本0.5~10亿."
    )

    def run(self, context: StrategyContext) -> list[dict[str, Any]]:
        cfg = context.config
        max_mv = cfg.get("max_mv", 1000)
        min_shares = cfg.get("min_total_shares", 0.5)
        max_shares = cfg.get("max_total_shares", 10)
        ma_short = cfg.get("ma_short", 10)
        ma_long = cfg.get("ma_long", 30)
        gc_lookback = cfg.get("gc_lookback_days", 3)  # 金叉在最近N天内
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
        candidates.sort(key=lambda x: max(x["changepercent"], 0) * 5, reverse=True)
        check_list = candidates[:kline_limit]
        log(f"  Fetching klines for top {len(check_list)} candidates...")

        # ── Fetch hot sectors (cached at class level) ──
        hot_sectors = getattr(DualMAGoldenCross, "_hot_sectors_cache", None)
        if hot_sectors is None:
            hot_sectors = []
            try:
                sector_fetcher = DataFetcher(context.engine_config)
                hot_sectors = sector_fetcher.fetch_hot_sectors_with_strength(top_k=30)
                if hot_sectors:
                    top5 = ", ".join(s["name"] for s in hot_sectors[:5])
                    log(f"  Hot sectors (by 涨跌比): {top5}...")
            except Exception:
                pass
            DualMAGoldenCross._hot_sectors_cache = hot_sectors

        # ── Step 3: Kline analysis — MA5>MA18 + recent crossover ────
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
            volumes = [d.get("volume", 0) for d in kd]
            close = closes[-1]
            n = len(closes)

            # Current MAs
            ma_s = sum(closes[-ma_short:]) / ma_short
            ma_l = sum(closes[-ma_long:]) / ma_long

            # Condition: MA5 > MA18 (golden cross state)
            if not (ma_s > ma_l):
                continue

            # Recent crossover check: was there a crossover in the last N days?
            gc_found = False
            gc_day = 0  # how many days ago the crossover happened
            for lookback in range(1, min(gc_lookback + 1, n - ma_long)):
                # Check if MA5 <= MA18 at lookback days ago
                prev_ma_s = sum(closes[-(ma_short + lookback):-lookback or None]) / ma_short
                prev_ma_l = sum(closes[-(ma_long + lookback):-lookback or None]) / ma_long
                if prev_ma_s <= prev_ma_l:
                    gc_found = True
                    gc_day = lookback
                    break

            if not gc_found:
                continue

            # ── Quality scoring ────────────────────────────────
            score = 0.0

            # 1. MA alignment strength (how far MA5 is above MA18)
            if ma_l > 0:
                ma_align = (ma_s - ma_l) / ma_l
                ma_score = min(max(ma_align * 5, 0), 1.0)  # 20% separation = full score
                score += 0.30 * ma_score

            # Volume surge
            if len(volumes) >= 19:
                recent_vol = volumes[-1]
                avg_vol = sum(volumes[-19:-1]) / 18 if sum(volumes[-19:-1]) > 0 else 1
                vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
                vol_score = min(vol_ratio / 3.0, 1.0)
            else:
                vol_ratio = 1.0
                vol_score = 0.5
            score += 0.20 * vol_score

            # 3. Momentum (当日涨幅)
            mom_score = min(max(c["changepercent"], -10), 10) / 10.0
            score += 0.20 * mom_score

            # 4. Small cap bonus
            mv_norm = 1.0 - (c["mv"] / max_mv) if max_mv > 0 else 0.5
            score += 0.15 * max(0, mv_norm)

            # 5. How recent the crossover was (recent = better)
            recency_score = 1.0 - (gc_day - 1) / max(gc_lookback, 1)  # day 1=1.0, day 3=0.33
            score += 0.15 * max(0, recency_score)

            # 6. Sector strength (bonus, not in base score to keep sorting clean)
            sector_name, sector_strength = (
                match_stock_to_hot_sector(c["name"], hot_sectors)
                if hot_sectors else ("其他", 0.0)
            )
            sec_score = min(sector_strength / 10.0, 1.0)
            # Combined score = base + sector bonus
            final_score = score + 0.20 * sec_score

            selected.append({
                "code": code,
                "name": c["name"],
                "price": round(close, 2),
                "mv": c["mv"],
                "total_shares": c["total_shares"],
                "changepercent": c["changepercent"],
                f"ma{ma_short}": round(ma_s, 2),
                f"ma{ma_long}": round(ma_l, 2),
                "score": round(final_score, 4),
                "volume_ratio": round(vol_ratio, 2),
                "gc_days_ago": gc_day,
                "sector": sector_name,
                "sector_strength": sector_strength,
            })

            if (i + 1) % 30 == 0:
                log(f"  Kline: {i+1}/{len(check_list)}, selected: {len(selected)}")
                if delay > 0:
                    time.sleep(1.5)

            if delay > 0 and (i + 1) % 30 != 0:
                time.sleep(delay)

        # ── Step 4: Sort by score desc, then sector strength desc ──
        selected.sort(key=lambda x: (-x["score"], -x["sector_strength"]))
        selected = selected[:top_n]

        log(f"  Final selections: {len(selected)} stocks")
        return selected
