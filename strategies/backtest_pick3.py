#!/usr/bin/env python3
"""
Backtest for momentum_ma v2 — buy 3 stocks from NEXT day's candidate list.

Logic:
  1. On each rebalance day T, run the strategy to generate candidate list.
  2. On day T+1 (next trading day), buy top 3 from T's candidates.
  3. Hold with daily sell monitoring (same sell rules as original).

Usage:
    python3 -m strategies.backtest_pick3 --universe 500 --days 240 --save
"""

import sys
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import json
import time
from collections import defaultdict
from datetime import datetime

# Reuse existing backtest machinery
from strategies.backtest import (
    DEFAULT_CONFIG, BacktestResult, TradeRecord, Position,
    MomentumMABacktester, fetch_kline_with_dates, build_common_dates,
    get_kline_series, calc_ma, BacktestResult, COST,
    fetch_kline_with_dates, DataFetcher, log,
    code_to_prefix, safe_float,
)
from strategies.data.fetcher import fetch_url, TENCENT_HEADERS


# ── Limit-up/down helpers ────────────────────────
def get_limit_pct(code: str) -> float:
    if code.startswith(("300", "688")):
        return 0.20
    elif code.startswith(("8", "4", "92")):
        return 0.30
    else:
        return 0.10

def is_limit_up_from_klines(klines, date, code):
    from strategies.backtest import get_kline_series
    hist = get_kline_series(klines, date, lookback=3)
    if hist is None or len(hist) < 2:
        return False
    today_close = hist[-1]["close"]
    yest_close = hist[-2]["close"]
    if yest_close <= 0:
        return False
    limit_pct = get_limit_pct(code)
    return today_close >= yest_close * (1 + limit_pct) * 0.995

def is_limit_down_from_klines(klines, date, code):
    from strategies.backtest import get_kline_series
    hist = get_kline_series(klines, date, lookback=3)
    if hist is None or len(hist) < 2:
        return False
    today_close = hist[-1]["close"]
    yest_close = hist[-2]["close"]
    if yest_close <= 0:
        return False
    limit_pct = get_limit_pct(code)
    return today_close <= yest_close * (1 - limit_pct) * 1.005


def select_picks_score(candidates, pick_count):
    """Pick top N by composite score."""
    return candidates[:pick_count]


def select_picks_sector(candidates, pick_count):
    """Pick top 1 from each of the top N hot sectors.

    Groups candidates by sector, picks the highest-scored stock in each,
    then takes the top pick_count sectors sorted by sector_strength.
    Falls back to score-based picking if no sector info available.
    """
    # Group by sector
    by_sector = defaultdict(list)
    for c in candidates:
        sector = c.get("sector", "其他")
        by_sector[sector].append(c)

    # For each sector, take the top 1 by score
    sector_best = []
    for sector, stocks in by_sector.items():
        stocks.sort(key=lambda x: x["score"], reverse=True)
        best = stocks[0]
        sector_best.append({
            "pick": best,
            "sector": sector,
            "sector_strength": best.get("sector_strength", 0),
        })

    # Sort sectors by strength descending, take top pick_count
    sector_best.sort(key=lambda x: x["sector_strength"], reverse=True)
    selected = [s["pick"] for s in sector_best[:pick_count]]

    if len(selected) < pick_count:
        # Not enough sectors — fill remaining from top-score candidates not yet picked
        picked_codes = {s["code"] for s in selected}
        remaining = [c for c in candidates if c["code"] not in picked_codes]
        remaining.sort(key=lambda x: x["score"], reverse=True)
        needed = pick_count - len(selected)
        selected.extend(remaining[:needed])

    return selected


PICK_MODE_FN = {
    "score": select_picks_score,
    "sector": select_picks_sector,
}


PICK3_CONFIG = {
    **DEFAULT_CONFIG,
    "pick_count": 3,           # buy top N from next-day candidate list
}


class Pick3Backtester(MomentumMABacktester):
    """
    Backtester that: today's strategy candidates → buy top 3 tomorrow.
    """

    def run_strategy_at_date(
        self,
        klines_data: dict[str, list[dict]],
        md_map: dict[str, dict],
        target_date: str,
    ) -> list[dict]:
        """Run strategy and preserve sector/sector_strength fields."""
        from strategies.base import StrategyContext
        from strategies.builtins.momentum_ma import MomentumMAStrategy

        # Truncate klines to target_date (no look-ahead bias)
        truncated_klines: dict[str, list[dict]] = {}
        for code, klines in klines_data.items():
            hist = get_kline_series(klines, target_date, lookback=120)
            if hist and len(hist) >= 25:
                truncated_klines[code] = hist
            else:
                truncated_klines[code] = []

        # Build all_stocks with historical changepercent
        all_stocks: list[dict] = []
        for s in self._universe_cache:
            code = s["code"]
            hist = truncated_klines.get(code)
            if hist and len(hist) >= 2:
                prev_close = hist[-2]["close"]
                curr_close = hist[-1]["close"]
                hist_change = (
                    (curr_close - prev_close) / prev_close * 100
                    if prev_close > 0 else 0
                )
                s_with_hist = {**s, "changepercent": hist_change}
            else:
                s_with_hist = s
            all_stocks.append(s_with_hist)

        context = StrategyContext(
            all_stocks=all_stocks,
            market_data=md_map,
            klines=truncated_klines,
            config={**self.cfg, "skip_market_check": True},
            engine_config={**self.cfg, "kline_request_delay": 0},
        )

        strategy = MomentumMAStrategy()
        picks = strategy.run(context)

        result = []
        for pick in picks:
            result.append({
                "code": pick["code"],
                "name": pick["name"],
                "close": pick["price"],
                "mv": pick["mv"],
                "ma5": pick["ma5"],
                "ma18": pick["ma18"],
                "score": pick["score"],
                "sector": pick.get("sector", "其他"),
                "sector_strength": pick.get("sector_strength", 0),
            })
        return result

    def run(self) -> BacktestResult:
        result = BacktestResult(self.cfg)

        # ── 1. Fetch universe ────────────────────────────────────────
        universe, md_map = self.fetch_universe()
        if not universe:
            log("ERROR: Empty universe, cannot backtest.")
            return result

        # ── 1b. Fetch index ──────────────────────────────────────────
        idx_sym = "sh000985"
        idx_klines = fetch_kline_with_dates(idx_sym, self.cfg)
        index_code = "sh000985"
        idx_prefix = "sh"
        if idx_klines and len(idx_klines) >= 60:
            log(f"  Index klines: {len(idx_klines)} days, "
                f"{idx_klines[0]['date']} ~ {idx_klines[-1]['date']}")
        else:
            log("  WARN: No index klines, market filter disabled")
            idx_prefix = None

        # ── 2. Fetch stock klines ────────────────────────────────────
        klines_data = self.fetch_klines(universe)
        if not klines_data:
            log("ERROR: No kline data.")
            return result
        if idx_klines and len(idx_klines) >= 60:
            klines_data[index_code] = idx_klines

        # ── 3. Build date axis ───────────────────────────────────────
        all_dates = build_common_dates(klines_data)
        log(f"  Total unique trading days: {len(all_dates)}")
        if len(all_dates) < 150:
            log("ERROR: Too few trading days.")
            return result

        backtest_days = self.cfg["backtest_days"]
        if backtest_days >= len(all_dates):
            backtest_days = len(all_dates) // 2
        backtest_mode = self.cfg.get("backtest_mode", "late")
        if backtest_mode == "early":
            backtest_dates = all_dates[:backtest_days]
        else:
            start_idx = len(all_dates) - backtest_days
            backtest_dates = all_dates[start_idx:]

        rebalance_freq = self.cfg["rebalance_freq_days"]
        rebalance_indices = list(range(0, len(backtest_dates), rebalance_freq))
        rebalance_dates = [backtest_dates[i] for i in rebalance_indices]

        log(f"  Period: {backtest_dates[0]} → {backtest_dates[-1]}")
        log(f"  Rebalance dates: {len(rebalance_dates)}")

        # ── 4. Run simulation ────────────────────────────────────────
        all_trades: list[TradeRecord] = []
        open_positions: list[Position] = []
        date_to_idx = {d: i for i, d in enumerate(backtest_dates)}

        def is_bear_market(date: str) -> bool:
            idx_kd = klines_data.get(index_code) if idx_prefix else None
            if not idx_kd or len(idx_kd) < 25:
                return False
            hist = get_kline_series(idx_kd, date, lookback=30)
            if hist is None or len(hist) < 22:
                return False
            h_closes = [d["close"] for d in hist]
            h_ma20 = sum(h_closes[-20:]) / 20
            return h_closes[-1] < h_ma20

        skipped_rebalances = 0
        total_buys = 0

        for ri, candidate_date in enumerate(rebalance_dates):
            # ── Market filter ────────────────────────────────────────
            if idx_prefix and is_bear_market(candidate_date):
                log(f"  [SKIP] {candidate_date}: bear market")
                skipped_rebalances += 1
                continue

            # ── Generate candidates for today ────────────────────────
            picks = self.run_strategy_at_date(klines_data, md_map, candidate_date)
            if not picks:
                log(f"  [NO CANDIDATES] {candidate_date}")
                continue

            # Sort by score descending, take top N for candidate pool
            picks.sort(key=lambda x: x["score"], reverse=True)
            candidates = picks[:self.cfg["top_n"]]

            # ── Find next trading day for execution ──────────────────
            cd_idx = date_to_idx.get(candidate_date)
            if cd_idx is None or cd_idx + 1 >= len(backtest_dates):
                log(f"  [SKIP] {candidate_date}: no next trading day")
                continue
            buy_date = backtest_dates[cd_idx + 1]

            # ── Buy picks from candidates ───────────────────────────
            pick_fn = PICK_MODE_FN.get(
                self.cfg.get("pick_mode", "score"),
                select_picks_score,
            )
            buys = pick_fn(candidates, self.cfg["pick_count"])

            log(f"  [BUY+1] {buy_date} (candidates from {candidate_date}): "
                f"{len(buys)} picks ({self.cfg['pick_mode']}, "
                f"pool={len(candidates)})")

            for pick in buys:
                code = pick["code"]
                # Get actual price on buy_date
                klines = klines_data.get(code)
                if not klines:
                    continue
                hist = get_kline_series(klines, buy_date, lookback=5)
                if hist is None or len(hist) < 1:
                    continue
                actual_close = hist[-1]["close"]
                if actual_close <= 0:
                    continue

                # 涨停不买
                if is_limit_up_from_klines(klines, buy_date, code):
                    log(f"    [SKIP] {code} at 涨停 on {buy_date}, skipping buy")
                    continue

                pos = Position(
                    code=code, buy_date=buy_date,
                    buy_price=actual_close,
                    name=pick.get("name", ""),
                    mv=pick.get("mv", 0),
                )
                open_positions.append(pos)
                total_buys += 1

            # ── Daily sell checks for all open positions ─────────────
            # A股 T+1: sell checks start from buy_date + 1 (next trading day)
            buy_idx = date_to_idx.get(buy_date, 0)
            for di in range(buy_idx + 1, len(backtest_dates)):
                current_date = backtest_dates[di]
                still_open = []
                for pos in open_positions:
                    if pos.is_closed:
                        all_trades.extend(pos.closed_trades)
                        continue
                    klines = klines_data.get(pos.code)
                    if not klines:
                        still_open.append(pos)
                        continue
                    self.check_sell_conditions(klines, pos, current_date)
                    if not pos.is_closed:
                        still_open.append(pos)
                    else:
                        all_trades.extend(pos.closed_trades)
                open_positions = still_open

        # ── 5. Close remaining positions ─────────────────────────────
        last_date = backtest_dates[-1]
        for pos in open_positions:
            if pos.is_closed:
                all_trades.extend(pos.closed_trades)
                continue
            klines = klines_data.get(pos.code)
            if klines:
                last_close = klines[-1]["close"]
                pos.sell_all(last_date, last_close, "end_of_backtest")
            else:
                pos.sell_all(last_date, pos.buy_price, "no_data")
            all_trades.extend(pos.closed_trades)

        result.trades = all_trades
        result.compute()

        log(f"\n  Backtest complete:")
        log(f"    Trades:       {result.total_trades}")
        log(f"    Win Rate:     {result.win_rate:.1f}%")
        log(f"    Total Ret:    {result.total_return_pct:.2f}%")
        log(f"    Avg/Trade:    {result.avg_return_pct:.2f}%")
        log(f"    Max DD:       {result.max_drawdown:.2f}%")
        log(f"    Sharpe:       {getattr(result, 'sharpe_ratio', 0):.2f}")
        log(f"    Total buys:   {total_buys}")
        log(f"    Skipped bear: {skipped_rebalances}/{len(rebalance_dates)}")

        return result


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Backtest momentum_ma v2 — buy top 3 from NEXT day's candidates"
    )
    parser.add_argument("--universe", type=int, default=500,
                        help="Top N stocks by amount")
    parser.add_argument("--top-n", type=int, default=15,
                        help="Candidate pool size from strategy")
    parser.add_argument("--pick", type=int, default=3,
                        help="How many to buy (default: 3)")
    parser.add_argument("--pick-mode", type=str, default="score",
                        choices=["score", "sector"],
                        help="Pick strategy: 'score' = top N by score, "
                             "'sector' = top 1 from top N hot sectors")
    parser.add_argument("--freq", type=int, default=5,
                        help="Rebalance frequency in trading days")
    parser.add_argument("--days", type=int, default=240,
                        help="Backtest period in trading days")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="Delay between kline requests (seconds)")
    parser.add_argument("--stoploss", type=float, default=0.94,
                        help="Hard stop loss as fraction of buy price")
    parser.add_argument("--tp", type=float, default=1.3,
                        help="Take profit multiplier (price > MA5 * N)")
    parser.add_argument("--save", "-s", action="store_true",
                        help="Save results to JSON")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output filename")
    parser.add_argument("--early", action="store_true",
                        help="Use earliest data instead of most recent")
    args = parser.parse_args()

    bt_config = {
        "universe_size": args.universe,
        "top_n": args.top_n,
        "pick_count": args.pick,
        "pick_mode": args.pick_mode,
        "rebalance_freq_days": args.freq,
        "backtest_days": args.days,
        "kline_delay": args.delay,
        "stoploss_pct": args.stoploss,
        "take_profit_mult": args.tp,
    }
    if args.early:
        bt_config["backtest_mode"] = "early"

    bt = Pick3Backtester(bt_config)
    t0 = time.time()
    result = bt.run()
    elapsed = time.time() - t0

    print("\n" + result.summary())
    print(f"\nBacktest completed in {elapsed:.1f}s")

    # Override the title in summary for clarity
    mode_label = "PICK3-score" if args.pick_mode == "score" else "PICK3-sector"
    summary_lines = result.summary().split("\n")
    summary_lines[1] = f"║   momentum_ma v2 {mode_label} (明日Top3)             ║"
    print("\n".join(summary_lines))

    if args.save:
        output_dir = os.path.join(PROJECT_DIR, "backtest_results")
        os.makedirs(output_dir, exist_ok=True)
        fname = args.output if args.output else "momentum_ma_pick3_backtest.json"
        path = os.path.join(output_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"Results saved to {path}")

    # Print trade summary
    if result.trades:
        winners = [t for t in result.trades if t.return_pct > 0]
        losers = [t for t in result.trades if t.return_pct <= 0]
        if winners:
            best = max(winners, key=lambda t: t.return_pct)
            print(f"\n  Best trade:  {best.code} {best.buy_date}→{best.sell_date}"
                  f" {best.return_pct:+.2f}% ({best.reason})")
        if losers:
            worst = min(losers, key=lambda t: t.return_pct)
            print(f"  Worst trade: {worst.code} {worst.buy_date}→{worst.sell_date}"
                  f" {worst.return_pct:+.2f}% ({worst.reason})")


if __name__ == "__main__":
    main()
