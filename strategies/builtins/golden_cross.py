"""
MA5上穿MA18 金叉策略 + 热门赛道跟踪
"""

import time
from typing import Any

from strategies.base import StrategyBase, StrategyContext
from strategies.registry import register_strategy
from strategies.data.fetcher import log, code_to_prefix, match_track, DataFetcher


@register_strategy
class GoldenCrossStrategy(StrategyBase):
    name = "golden_cross"
    description = "MA5上穿MA18金叉策略 + 热门赛道跟踪"

    def run(self, context: StrategyContext) -> list[dict[str, Any]]:
        cfg = context.config
        max_mv = cfg.get("max_mv", 1000)
        top_n = cfg.get("top_n", 50)
        kline_limit = cfg.get("kline_check_limit", 100)
        delay = context.engine_config.get("kline_request_delay", 0.3)

        # Step 1: Market cap filter
        candidates = []
        for s in context.all_stocks:
            code = s.get("code", "")
            md = context.market_data.get(code)
            if not md: continue
            mv = md.get("mv", 0)
            name = md.get("name", "")
            if name.startswith(("ST", "*ST", "S")) or "退" in name: continue
            if mv <= 0 or mv >= max_mv: continue
            candidates.append({
                "code": code, "name": name, "price": md.get("price", 0),
                "mv": mv, "circ": md.get("circ_shares", 0),
                "changepercent": s.get("changepercent", 0),
            })

        log(f"  After MV filter: {len(candidates)} stocks")
        if not candidates: return []

        # Step 2: Kline check (MA5 > MA18 金叉)
        candidates.sort(key=lambda x: x["changepercent"], reverse=True)
        check_list = candidates[:kline_limit]
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
            close = closes[-1]
            ma5 = sum(closes[-5:]) / 5
            ma18 = sum(closes[-18:]) / 18

            if ma5 <= ma18: continue
            if close > ma18 * 1.10: continue

            diff_pct = (ma5 - ma18) / ma18 * 100
            score = diff_pct

            # Hot track boost
            if cfg.get("hot_track_boost", True):
                track = match_track(c["name"])
                hot_tracks = ["半导体/芯片", "AI/人工智能", "人形机器人", "低空经济", "智能驾驶", "创新药/医药"]
                if track in hot_tracks:
                    score += 0.5

            final.append({
                "code": code, "name": c["name"], "price": round(close, 2),
                "mv": c["mv"], "circ": c["circ"],
                "changepercent": c["changepercent"],
                "ma5": round(ma5, 2), "ma18": round(ma18, 2),
                "ma5_diff_pct": round(diff_pct, 2),
                "above_ma18_pct": round((close-ma18)/ma18*100, 2),
                "score": round(score, 4),
            })

            if (i + 1) % 30 == 0:
                log(f"  Kline: {i+1}/{len(check_list)}, passed: {len(final)}")
            if delay > 0: time.sleep(delay)

        final.sort(key=lambda x: x["score"], reverse=True)
        final = final[:top_n]
        log(f"  Final selections: {len(final)} stocks")
        return final
