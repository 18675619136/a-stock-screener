#!/usr/bin/env python3
"""
Combined strategy backtest: momentum_ma (bull) + dual_ma_gc (bear).
No market filter — always picks the right strategy for each rebalance.

Logic:
  1. On each rebalance date, check market regime (中证全指 vs MA18)
  2. 🟢 BULL (index ≥ MA18) → run momentum_ma strategy
  3. 🟡 BEAR (index < MA18) → run dual_ma_gc strategy
  4. Same unified sell rules throughout
  5. No skipped rebalances — all data included

Usage:
    python3 -m strategies.backtest_combined --universe 500 --days 480 --freq 5 --save
"""
import sys, os, json, time
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from strategies.backtest import (
    DEFAULT_CONFIG as MOM_CONFIG,
    BacktestResult, Position, MomentumMABacktester,
    fetch_kline_with_dates, build_common_dates, get_kline_series,
    calc_ma, COST, PEAK_TP_PCT, DataFetcher, log,
    code_to_prefix, safe_float, get_limit_pct,
    is_limit_up_from_klines, is_limit_down_from_klines,
)

from strategies.backtest_dual_ma_gc import (
    DEFAULT_CONFIG as DUAL_CONFIG,
    DualMAGoldenCrossBacktester,
)

# ── Market regime detection (same as daily_stock_report.py) ──
def get_market_regime(klines_data, date):
    """Check if CSI All-Share (000985) is above or below MA18 on the given date."""
    idx_kd = klines_data.get("sh000985")
    if not idx_kd or len(idx_kd) < 25:
        return "bull", 0, 0
    hist = get_kline_series(idx_kd, date, lookback=30)
    if hist is None or len(hist) < 20:
        return "bull", 0, 0
    closes = [d["close"] for d in hist]
    index_price = closes[-1]
    ma18 = sum(closes[-18:]) / 18
    regime = "bull" if index_price >= ma18 else "bear"
    return regime, index_price, ma18


def run_combined_backtest(cfg: dict) -> BacktestResult:
    result = BacktestResult(cfg)
    # Will fill trades manually
    result.trades = []
    
    universe_size = cfg.get("universe_size", 500)
    backtest_days = cfg.get("backtest_days", 480)
    rebalance_freq = cfg.get("rebalance_freq_days", 5)
    top_n = cfg.get("top_n", 30)
    kline_delay = cfg.get("kline_delay", 0)
    delay = cfg.get("request_delay", 0)

    # ── Init both backtester instances ──
    mom_config = {**MOM_CONFIG, **cfg}
    dual_config = {**DUAL_CONFIG, **cfg}
    
    mom_bt = MomentumMABacktester(mom_config)
    dual_bt = DualMAGoldenCrossBacktester(dual_config)

    # ── Step 1: Fetch universe (shared) ──
    log("▶ Fetching all A-share stocks from Sina...")
    from strategies.data.fetcher import DataFetcher as DF
    fetcher = DF(mom_config)
    all_stocks = fetcher.get_all_a_stocks()
    log(f"  Total: {len(all_stocks)}")
    
    filtered = [s for s in all_stocks if not (s.get("name","").startswith(("ST","*ST","S")) or "退" in s.get("name","")) and s.get("price",0) > 0]
    log(f"  After ST/price filter: {len(filtered)}")
    
    log("▶ Fetching market data...")
    market_data = fetcher.get_market_data(filtered, batch_size=cfg.get("tencent_batch_size",80))
    log(f"  Market data: {len(market_data)} stocks")
    
    universe, md_map = [], {}
    for s in filtered:
        code = s["code"]
        md = market_data.get(code)
        if not md: continue
        mv = md.get("mv", 0)
        ts = md.get("total_shares", 0)
        if mv <= 0 or mv > cfg.get("max_mv", 1000): continue
        if ts <= 0 or ts < cfg.get("min_total_shares", 0.5) or ts > cfg.get("max_total_shares", 10): continue
        s["amount"] = md.get("amount", 0) or s.get("amount", 0)
        universe.append(s)
        md_map[code] = md
    # Sort by amount descending, take top N
    universe.sort(key=lambda x: x.get("amount", 0), reverse=True)
    universe = universe[:universe_size]
    log(f"  Universe: {len(universe)} stocks (top {universe_size} by amount, MV<=1000亿, shares 0.5~10亿)")

    # ── Step 2: Fetch klines ──
    log(f"▶ Fetching klines for {len(universe)} stocks...")
    klines_data = {}
    from strategies.data.fetcher import code_to_prefix as c2p
    for i, s in enumerate(universe):
        code = s["code"]
        prefix = c2p(code)
        if not prefix: continue
        sym = f"{prefix}{code}"
        kd = fetch_kline_with_dates(sym, cfg)
        if kd and len(kd) >= 50:
            klines_data[code] = kd
        if (i+1) % 50 == 0:
            log(f"  Klines: {i+1}/{len(universe)}, ok: {len(klines_data)}")
        if kline_delay > 0 and (i+1) % 30 == 0:
            time.sleep(1)
    log(f"  Klines fetched: {len(klines_data)} stocks")

    # ── Fetch index klines for market regime ──
    idx_kd = fetch_kline_with_dates("sh000985", cfg)
    if idx_kd and len(idx_kd) >= 60:
        klines_data["sh000985"] = idx_kd
    else:
        log("  ERROR: No index klines, cannot determine market regime")
        return result

    # ── Build date axis ──
    all_dates = build_common_dates(klines_data)
    log(f"  Total unique trading days: {len(all_dates)}")
    if len(all_dates) < 100:
        log("ERROR: Too few trading days.")
        return result

    # Use ALL available data (full period)
    if backtest_days >= len(all_dates):
        backtest_days = len(all_dates) // 2
    backtest_dates = all_dates[-backtest_days:]
    
    rebalance_indices = list(range(0, len(backtest_dates), rebalance_freq))
    rebalance_dates = [backtest_dates[i] for i in rebalance_indices]
    
    log(f"  Period: {backtest_dates[0]} → {backtest_dates[-1]}")
    log(f"  Rebalance dates: {len(rebalance_dates)}")
    log("  Market filter: DISABLED (combined strategy switches automatically)")

    # ── Run simulation ──
    all_trades = []
    open_positions = []
    date_to_idx = {d: i for i, d in enumerate(backtest_dates)}
    
    bull_count = 0
    bear_count = 0
    
    for ri, buy_date in enumerate(rebalance_dates):
        # Check market regime
        regime, idx_price, idx_ma18 = get_market_regime(klines_data, buy_date)
        if regime == "bull":
            bull_count += 1
            picks = mom_bt.run_strategy_at_date(klines_data, md_map, buy_date)
            strategy_label = "momentum_ma"
        else:
            bear_count += 1
            picks = dual_bt.run_strategy_at_date(klines_data, md_map, buy_date)
            strategy_label = "dual_ma_gc"
        
        regime_emoji = "🟢" if regime == "bull" else "🟡"
        log(f"  [{regime_emoji}{strategy_label}] {buy_date}: index={idx_price:.0f}, MA18={idx_ma18:.0f}, picks={len(picks) if picks else 0}")
        
        if picks:
            for pick in picks:
                code = pick["code"]
                # Skip if 涨停
                stock_klines = klines_data.get(code)
                if stock_klines and is_limit_up_from_klines(stock_klines, buy_date, code):
                    log(f"    [SKIP] {code} at 涨停 on {buy_date}")
                    continue
                pos = Position(
                    code=code, buy_date=buy_date,
                    buy_price=pick["close"],
                    name=pick.get("name", ""),
                    mv=pick.get("mv", 0),
                )
                open_positions.append(pos)
        
        # Daily sell checks (same logic for both strategies)
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
                # Use unified sell check (MA5/10/18 + 95% stoploss + upper shadow)
                _check_sell(klines, pos, current_date, cfg)
                if not pos.is_closed:
                    still_open.append(pos)
                else:
                    all_trades.extend(pos.closed_trades)
            open_positions = still_open
        
        log(f"  Open positions: {len(open_positions)}, total trades: {len(all_trades)}")
    
    log(f"  Regime: 🟢 BULL {bull_count}x  🟡 BEAR {bear_count}x")
    
    # Close remaining at last date
    last_date = backtest_dates[-1]
    for pos in open_positions:
        if pos.is_closed:
            all_trades.extend(pos.closed_trades)
            continue
        klines = klines_data.get(pos.code)
        if klines:
            pos.sell_all(last_date, klines[-1]["close"], "end_of_backtest")
        else:
            pos.sell_all(last_date, pos.buy_price, "no_data")
        all_trades.extend(pos.closed_trades)
    
    result.trades = all_trades
    # Manual stats computation
    result.total_trades = len(all_trades)
    result.winning_trades = sum(1 for t in all_trades if t.return_pct > 0)
    result.losing_trades = sum(1 for t in all_trades if t.return_pct <= 0)
    total_returns = [t.return_pct for t in all_trades]
    result.win_rate = result.winning_trades / result.total_trades * 100 if result.total_trades > 0 else 0
    cum = 1.0
    for r in total_returns:
        cum *= (1 + r / 100)
    result.total_return_pct = (cum - 1.0) * 100
    result.avg_return_pct = sum(total_returns) / len(total_returns) if total_returns else 0
    # Max drawdown from cumulative returns
    peak = 1.0
    dd = 0.0
    running_cum = 1.0
    for r in total_returns:
        running_cum *= (1 + r / 100)
        if running_cum > peak:
            peak = running_cum
        dd_val = (peak - running_cum) / peak * 100
        if dd_val > dd:
            dd = dd_val
    result.max_drawdown = dd
    result.sharpe_ratio = (result.total_return_pct / dd * 0.3) if dd > 0 else 0
    return result


def _check_sell(klines, position, current_date, cfg):
    """Unified sell check — same logic for both strategies."""
    hist = get_kline_series(klines, current_date, lookback=60)
    if hist is None or len(hist) < 25:
        return False
    
    closes = [d["close"] for d in hist]
    close = closes[-1]
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma18 = calc_ma(closes, 18)
    
    if ma5 <= 0 or ma10 <= 0:
        return False
    
    buy_price = position.buy_price
    stoploss_pct = cfg.get("stoploss_pct", 0.95)
    
    # Check 跌停
    if is_limit_down_from_klines(klines, current_date, position.code):
        return False
    
    # P1: 硬止损 95%
    if close <= buy_price * stoploss_pct:
        position.sell_all(current_date, close, "SL_95pct")
        return True
    
    # P2: MA10破位 → 全卖
    if close < ma10:
        position.sell_all(current_date, close, "MA10_below")
        return True
    
    # Half-sell conditions
    if position.units >= 2:
        # P3: 止盈 MA5×1.3
        tp_price = ma5 * cfg.get("take_profit_mult", 1.3)
        if close > tp_price:
            position.sell_half(current_date, close, "TP_MA5x1.3")
            return False
        
        # P4: 长上影线
        kline_high = hist[-1].get("high", close)
        if kline_high >= close * PEAK_TP_PCT:
            position.sell_half(current_date, close, "UPPER_SHADOW_1.08x")
            return False
        
        # P5: MA5破位 → 卖一半
        if close < ma5:
            position.sell_half(current_date, close, "SL_below_MA5")
            return False
    
    return False


# ── CLI ──
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Combined strategy backtest (momentum_ma + dual_ma_gc)")
    parser.add_argument("--universe", type=int, default=500, help="Top N stocks by amount")
    parser.add_argument("--days", type=int, default=480, help="Backtest period in trading days")
    parser.add_argument("--freq", type=int, default=5, help="Rebalance frequency")
    parser.add_argument("--delay", type=float, default=0, help="Delay between kline requests")
    parser.add_argument("--save", "-s", action="store_true", help="Save results to JSON")
    args = parser.parse_args()
    
    cfg = {
        "universe_size": args.universe,
        "backtest_days": args.days,
        "rebalance_freq_days": args.freq,
        "kline_delay": args.delay,
        "request_delay": args.delay,
        "max_mv": 1000,
        "min_total_shares": 0.5,
        "max_total_shares": 10,
        "top_n": 30,
        "kline_request_delay": args.delay,
        "kline_check_limit": 150,
        "volume_surge_ratio": 1.2,
        "ma_short": 5,
        "ma_long": 18,
        "ma_stop": 10,
        "stoploss_pct": 0.95,
        "take_profit_mult": 1.3,
        "gc_lookback_days": 3,
        "sina_page_size": 5000,
        "tencent_batch_size": 80,
        "data_dir": PROJECT_DIR,
    }
    
    start = time.time()
    result = run_combined_backtest(cfg)
    elapsed = time.time() - start
    
    print(f"\n{'╔':═^60}{'╗'}")
    print(f"║   Combined Strategy Backtest (momentum_ma + dual_ma_gc)  ║")
    print(f"{'╠':═^60}{'╣'}")
    print(f"║  Period: {result.trades[0].buy_date if result.trades else 'N/A'} → {result.trades[-1].sell_date if result.trades else 'N/A'}")
    print(f"║  Trades:      {len(result.trades):<6}                      ║")
    print(f"║  Win Rate:    {result.win_rate:<6.1f} %                    ║")
    print(f"║  Total Ret:   {result.total_return_pct:<7.2f} %               ║")
    print(f"║  Avg/Trade:   {result.avg_return_pct:<6.2f} %               ║")
    print(f"║  Max DD:      {result.max_drawdown:<6.2f} %                 ║")
    print(f"║  Sharpe:      {result.sharpe_ratio:<6.2f}                      ║")
    print(f"{'╚':═^60}{'╝'}")
    print(f"\nBacktest completed in {elapsed:.1f}s")
    
    if args.save:
        save_dir = os.path.join(PROJECT_DIR, "backtest_results")
        os.makedirs(save_dir, exist_ok=True)
        fpath = os.path.join(save_dir, "combined_switching_backtest.json")
        with open(fpath, 'w') as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"Results saved to {fpath}")
