"""
Momentum + Volume Surge + MA Alignment + Small/Mid Cap Strategy

This is the default built-in strategy combining four factors:
1. Momentum (25%) — positive daily change
2. Volume surge (15%) — current volume > 1.5x 20-day average
3. MA alignment (25%) — MA5 > MA20 (金叉形态)
4. Small/mid cap (20%) — market cap < 1000亿
5. Price position (15%) — prefers stocks 20-60% off recent low
"""

import time
from typing import Any

from strategies.base import StrategyBase, StrategyContext
from strategies.registry import register_strategy
from strategies.data.fetcher import log, code_to_prefix, DataFetcher


@register_strategy
class MomentumMAStrategy(StrategyBase):
    name = "momentum_ma"
    description = "Momentum + Volume Surge + MA Alignment + Small/Mid Cap"

    def run(self, context: StrategyContext) -> list[dict[str, Any]]:
        cfg = context.config
        max_mv = cfg.get("max_mv", 1000)
        min_circ = cfg.get("min_circ", 0.5)
        max_circ = cfg.get("max_circ", 10)
        top_n = cfg.get("top_n", 30)
        kline_limit = cfg.get("kline_check_limit", 150)
        delay = context.engine_config.get("kline_request_delay", 0.3)
        enable_volume = cfg.get("enable_volume_filter", True)
        enable_ma = cfg.get("enable_ma_filter", True)
        weights = cfg.get("score_weights", {"momentum":0.25, "ma_alignment":0.25, "volume_surge":0.15, "small_cap":0.20, "price_position":0.15})

        # Step 1: Basic filters (no klines needed)
        candidates = []
        for s in context.all_stocks:
            code = s.get("code", "")
            md = context.market_data.get(code)
            if not md: continue
            mv = md.get("mv", 0)
            circ = md.get("circ_shares", 0)
            name = md.get("name", "")
            change = s.get("changepercent", 0)
            if name.startswith(("ST", "*ST", "S")) or "退" in name: continue
            if mv <= 0 or mv >= max_mv: continue
            if circ <= 0 or circ < min_circ or circ > max_circ: continue
            candidates.append({
                "code": code, "name": name, "price": md.get("price", 0),
                "mv": mv, "circ": circ, "changepercent": change,
                "amount": md.get("amount", 0),
            })

        log(f"  After basic filters: {len(candidates)} stocks")
        if not candidates: return []

        # Pre-score and sort
        for c in candidates:
            score = weights.get("momentum",0.25) * min(max(c["changepercent"],-10),10)/10.0
            score += weights.get("small_cap",0.20) * max(0, 1.0-c["mv"]/max_mv)
            c["pre_score"] = score

        candidates.sort(key=lambda x: x["pre_score"], reverse=True)
        check_list = candidates[:kline_limit]
        log(f"  Fetching klines for top {len(check_list)} candidates...")

        # Step 2: Kline analysis
        final = []
        fetcher = DataFetcher(context.engine_config)
        for i, c in enumerate(check_list):
            code = c["code"]
            prefix = code_to_prefix(code)
            if not prefix: continue

            kd = context.get_kline(code)
            if kd is None:
                kd = fetcher.get_kline(f"{prefix}{code}")
                if kd is not None:
                    context.klines[code] = kd

            if kd is None or len(kd) < 25: continue

            closes = [d["close"] for d in kd]
            volumes = [d.get("volume", 0) for d in kd]
            close = closes[-1]
            ma5 = sum(closes[-5:]) / 5
            ma20 = sum(closes[-20:]) / 20

            if enable_ma and ma5 <= ma20: continue
            max_above = cfg.get("max_close_above_long", 1.10)
            if close > ma20 * max_above: continue

            vol_surge = 1.0
            if enable_volume and len(volumes) >= 21:
                recent_vol = volumes[-1]
                avg_vol = sum(volumes[-21:-1]) / 20 if sum(volumes[-21:-1]) > 0 else 1
                vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
                min_ratio = cfg.get("volume_surge_ratio", 1.5)
                if vol_ratio < min_ratio: continue
                vol_surge = min(vol_ratio / 3.0, 1.0)

            score = weights.get("momentum",0.25) * min(max(c["changepercent"],-10),10)/10.0
            ma_align = (ma5 - ma20) / ma20
            score += weights.get("ma_alignment",0.25) * min(max(ma_align*5, 0), 1.0)
            score += weights.get("volume_surge",0.15) * vol_surge
            score += weights.get("small_cap",0.20) * max(0, 1.0-c["mv"]/max_mv)

            recent_high, recent_low = max(closes[-20:]), min(closes[-20:])
            price_range = recent_high - recent_low
            if price_range > 0:
                pos = (close - recent_low) / price_range
                pos_score = max(0, min(1.0, 1.0 - abs(pos - 0.4) * 1.5))
            else:
                pos_score = 0.5
            score += weights.get("price_position",0.15) * pos_score

            final.append({
                "code": code, "name": c["name"], "price": round(close, 2),
                "mv": c["mv"], "circ": c["circ"],
                "changepercent": c["changepercent"],
                "ma5": round(ma5, 2), "ma20": round(ma20, 2),
                "ma5_diff_pct": round((ma5-ma20)/ma20*100, 2),
                "above_ma20_pct": round((close-ma20)/ma20*100, 2),
                "score": round(score, 4),
            })

            if (i + 1) % 30 == 0:
                log(f"  Kline: {i+1}/{len(check_list)}, passed: {len(final)}")
            if delay > 0: time.sleep(delay)

        final.sort(key=lambda x: x["score"], reverse=True)
        final = final[:top_n]
        log(f"  Final selections: {len(final)} stocks")
        return final
