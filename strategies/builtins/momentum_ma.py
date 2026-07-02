"""
Momentum MA Strategy v2 — CLOSE > MA5 > MA18 + 放量 + 中小市值

买入条件（筛选逻辑）:
  1. CLOSE > MA5 > MA18（收盘价在5日均线上方，且5日线在18日线上方）
  2. 成交量 > 18日均量 × 1.2（放量）
  3. 市值 < 1000亿
  4. 总股本 0.5 ~ 10亿

卖出条件（在回测 backtest.py 中实现逐日监控）:
  1. 价格 < 买入价的94% → 全部卖出（硬止损）
  2. 价格 > MA5 × 1.3 → 卖出一半（止盈）
  3. 价格 < MA5 → 卖出一半（止损）
  4. 价格 < MA18 → 全部卖出（硬止损）
"""

import time
from typing import Any

from strategies.base import StrategyBase, StrategyContext
from strategies.registry import register_strategy
from strategies.data.fetcher import log, code_to_prefix, safe_float


@register_strategy
class MomentumMAStrategy(StrategyBase):
    name = "momentum_ma"
    display_name = "多因子动量 v2"
    description = (
        "动量v2 — CLOSE>MA5>MA18 + 18日均量×1.2放量 + "
        "市值<1000亿 + 总股本0.5~10亿"
    )

    def run(self, context: StrategyContext) -> list[dict[str, Any]]:
        cfg = context.config
        max_mv = cfg.get("max_mv", 1000)
        min_shares = cfg.get("min_total_shares", 0.5)
        max_shares = cfg.get("max_total_shares", 10)
        enable_volume = cfg.get("enable_volume_filter", True)
        enable_ma = cfg.get("enable_ma_filter", True)
        top_n = cfg.get("top_n", 30)
        kline_limit = cfg.get("kline_check_limit", 150)
        delay = context.engine_config.get("kline_request_delay", 0.3)

        # ── Step 1: Basic filters (no klines needed) ────────────────
        candidates = []
        for s in context.all_stocks:
            code = s.get("code", "")
            md = context.market_data.get(code)
            if not md:
                continue
            mv = md.get("mv", 0)
            total_shares = md.get("total_shares", 0)
            name = md.get("name", "")
            price = md.get("price", 0)
            change = s.get("changepercent", 0)

            # ST / 退市 exclusion
            if name.startswith(("ST", "*ST", "S")) or "退" in name:
                continue

            # Market cap filter
            if mv <= 0 or mv >= max_mv:
                continue

            # Total shares filter
            if total_shares <= 0 or total_shares < min_shares or total_shares > max_shares:
                continue

            candidates.append({
                "code": code,
                "name": name,
                "price": price,
                "mv": mv,
                "total_shares": total_shares,
                "changepercent": change,
                "amount": md.get("amount", 0),
            })

        log(f"  After basic filters: {len(candidates)} stocks")
        if not candidates:
            return []

        # ── Step 2: Pre-sort by amount (liquid first), take top kline_limit ──
        candidates.sort(key=lambda x: x.get("amount", 0), reverse=True)
        check_list = candidates[:kline_limit]
        log(f"  Fetching klines for top {len(check_list)} candidates...")

        # ── Step 3: Kline analysis — CLOSE > MA5 > MA18 + volume ─────
        final = []
        for i, c in enumerate(check_list):
            code = c["code"]
            prefix = code_to_prefix(code)
            if not prefix:
                continue

            kd = context.get_kline(code)
            if kd is None:
                fetcher = getattr(self, "_fetcher", None)
                if fetcher is None:
                    from strategies.data.fetcher import DataFetcher
                    fetcher = DataFetcher(context.engine_config)
                    self._fetcher = fetcher
                sym = f"{prefix}{code}"
                kd = fetcher.get_kline(sym)
                if kd is not None:
                    context.klines[code] = kd

            if kd is None or len(kd) < 25:
                continue

            closes = [d["close"] for d in kd]
            volumes = [d.get("volume", 0) for d in kd]
            close = closes[-1]

            # MA calculation
            ma5 = sum(closes[-5:]) / 5
            ma18 = sum(closes[-18:]) / 18

            # Condition 1: CLOSE > MA5 > MA18
            if not (close > ma5 > ma18):
                continue

            # Condition 2: Volume > 18-day avg × 1.2
            if enable_volume and len(volumes) >= 19:
                recent_vol = volumes[-1]
                avg_vol = sum(volumes[-19:-1]) / 18 if sum(volumes[-19:-1]) > 0 else 1
                vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
                min_ratio = cfg.get("volume_surge_ratio", 1.2)
                if vol_ratio < min_ratio:
                    continue
                vol_surge = min(vol_ratio / 3.0, 1.0)
            else:
                vol_surge = 1.0
                vol_ratio = 1.0

            # ── Composite score (simple ranking) ─────────────────────
            score = 0.0

            # Momentum component
            mom_score = min(max(c["changepercent"], -10), 10) / 10.0
            score += 0.30 * mom_score

            # MA alignment strength (how far above MA18)
            if ma18 > 0:
                ma_align = (close - ma18) / ma18
                ma_score = min(max(ma_align * 3, 0), 1.0)
                score += 0.25 * ma_score

            # Volume surge
            score += 0.15 * vol_surge

            # Small cap bonus
            mv_norm = 1.0 - (c["mv"] / max_mv) if max_mv > 0 else 0.5
            score += 0.20 * max(0, mv_norm)

            # Price position: prefer stocks between 15%-50% of 18-day range
            recent_high = max(closes[-18:])
            recent_low = min(closes[-18:])
            price_range = recent_high - recent_low
            if price_range > 0:
                pos = (close - recent_low) / price_range
                pos_score = 1.0 - abs(pos - 0.35) * 1.8
                pos_score = max(0, min(1.0, pos_score))
            else:
                pos_score = 0.5
            score += 0.10 * pos_score

            final.append({
                "code": code,
                "name": c["name"],
                "price": round(close, 2),
                "mv": c["mv"],
                "total_shares": c["total_shares"],
                "changepercent": c["changepercent"],
                "ma5": round(ma5, 2),
                "ma18": round(ma18, 2),
                "above_ma18_pct": round((close - ma18) / ma18 * 100, 2),
                "score": round(score, 4),
                "volume_ratio": round(vol_ratio, 2),
            })

            if (i + 1) % 30 == 0:
                log(f"  Kline: {i+1}/{len(check_list)}, passed: {len(final)}")

            if delay > 0:
                time.sleep(delay)

        # ── Step 4: Rank by score and cap at top_n ──────────────────
        final.sort(key=lambda x: x["score"], reverse=True)
        final = final[:top_n]

        log(f"  Final selections: {len(final)} stocks")
        return final
