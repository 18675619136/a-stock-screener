#!/usr/bin/env python3
"""
Backtester for dual_ma_gc_limitup strategy — MA金叉 + 涨停过滤.

与 backtest_dual_ma_gc.py 完全相同的回测引擎，仅策略类替换为 dual_ma_gc_limitup。
在 dual_ma_gc 筛选基础上，增加"10日内有涨停但最近3日未涨停"条件。

Usage:
    python3 -m strategies.backtest_dual_ma_gc_limitup --help
    python3 -m strategies.backtest_dual_ma_gc_limitup --universe 1000 --save
"""
import sys
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import json
import time
from collections import Counter
from datetime import datetime
from typing import Any

from strategies.data.fetcher import (
    DataFetcher, log, safe_float, code_to_prefix, fetch_url,
    SINA_HEADERS, SINA_ALL_URL, TENCENT_HEADERS,
)

DEFAULT_CONFIG = {
    "universe_size": 1000,
    "rebalance_freq_days": 5,
    "backtest_days": 120,
    "max_mv": 1000,
    "min_total_shares": 0.5,
    "max_total_shares": 10,
    "ma_short": 5,
    "ma_long": 18,
    "ma_stop": 10,       # MA for hard stop (MA30)
    "gc_lookback_days": 3,  # golden cross within last N days
    "top_n": 30,
    "take_profit_mult": 1.3,   # price > MA5 * 1.3 → sell half
    "stoploss_pct": 0.94,      # price < buy_price * 0.94 → sell all
    "kline_delay": 0.2,
}

COST = 0.001
ST_NAME_PREFIXES = ("ST", "*ST", "S")


def is_st(name: str) -> bool:
    return name.startswith(ST_NAME_PREFIXES) or "退" in name


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


class TradeRecord:
    __slots__ = ("code", "buy_date", "sell_date", "buy_price", "sell_price",
                 "return_pct", "holding_days", "reason")
    def __init__(self, code: str, buy_date: str, buy_price: float,
                 sell_date: str = "", sell_price: float = 0.0,
                 return_pct: float = 0.0, holding_days: int = 0,
                 reason: str = ""):
        self.code = code
        self.buy_date = buy_date
        self.sell_date = sell_date
        self.buy_price = buy_price
        self.sell_price = sell_price
        self.return_pct = return_pct
        self.holding_days = holding_days
        self.reason = reason

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "buy_date": self.buy_date,
            "sell_date": self.sell_date,
            "buy_price": round(self.buy_price, 2),
            "sell_price": round(self.sell_price, 2),
            "return_pct": round(self.return_pct, 2),
            "holding_days": self.holding_days,
            "reason": self.reason,
        }


class BacktestResult:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.trades: list[TradeRecord] = []
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_return_pct = 0.0
        self.avg_return_pct = 0.0
        self.win_rate = 0.0
        self.max_drawdown = 0.0
        self.equity_curve: list[float] = []
        self.date_labels: list[str] = []

    def compute(self):
        self.total_trades = len(self.trades)
        if self.total_trades == 0:
            return
        self.winning_trades = sum(1 for t in self.trades if t.return_pct > 0)
        self.losing_trades = self.total_trades - self.winning_trades
        self.win_rate = self.winning_trades / self.total_trades * 100
        returns = [t.return_pct for t in self.trades]
        self.avg_return_pct = sum(returns) / len(returns)
        by_batch: dict[str, list[float]] = {}
        for t in self.trades:
            by_batch.setdefault(t.buy_date, []).append(t.return_pct)
        portfolio_returns = []
        cum = 1.0
        cum_returns = [1.0]
        date_labels = []
        for bdate, rets in sorted(by_batch.items()):
            batch_avg = sum(rets) / len(rets)
            portfolio_returns.append(batch_avg)
            cum *= (1 + batch_avg / 100)
            cum_returns.append(cum)
            date_labels.append(bdate)
        self.portfolio_returns = portfolio_returns
        self.total_return_pct = (cum - 1.0) * 100
        self.equity_curve = cum_returns
        self.date_labels = date_labels
        peak = 1.0
        dd = 0.0
        for v in cum_returns:
            if v > peak:
                peak = v
            dd = max(dd, (peak - v) / peak)
        self.max_drawdown = dd * 100
        if len(portfolio_returns) > 1:
            import statistics
            avg_r = sum(portfolio_returns) / len(portfolio_returns)
            std_r = statistics.stdev(portfolio_returns)
            freq = self.cfg.get("rebalance_freq_days", 5)
            if std_r > 1e-10:
                self.sharpe_ratio = (avg_r / std_r) * (252.0 / freq) ** 0.5
            else:
                self.sharpe_ratio = 0.0
        else:
            self.sharpe_ratio = 0.0


class Position:
    __slots__ = ("code", "buy_price", "buy_date", "units", "shares_per_unit")
    def __init__(self, code: str, buy_price: float, buy_date: str,
                 units: int = 2, shares_per_unit: int = 100):
        self.code = code
        self.buy_price = buy_price
        self.buy_date = buy_date
        self.units = units
        self.shares_per_unit = shares_per_unit

    def sell_half(self, sell_date: str, sell_price: float, reason: str) -> TradeRecord:
        self.units -= 1
        ret = (sell_price - self.buy_price) / self.buy_price * 100
        return TradeRecord(self.code, self.buy_date, self.buy_price,
                           sell_date, sell_price, ret, 0, reason)

    def sell_all(self, sell_date: str, sell_price: float, reason: str) -> TradeRecord:
        sold_units = self.units
        self.units = 0
        ret = (sell_price - self.buy_price) / self.buy_price * 100
        return TradeRecord(self.code, self.buy_date, self.buy_price,
                           sell_date, sell_price, ret, 0, reason)


def calc_ma(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return 0.0
    return sum(closes[-period:]) / period


def get_kline_series(klines: list[dict], target_date: str, lookback: int = 60) -> list[dict] | None:
    """Get klines up to and including target_date (no look-ahead)."""
    if not klines:
        return None
    idx = None
    for i, d in enumerate(klines):
        if d.get("date", "") >= target_date:
            idx = i
            break
    if idx is None:
        return None
    start = max(0, idx - lookback + 1)
    result = klines[start:idx + 1]
    if len(result) < 20:
        return None
    return result


def fetch_kline_with_dates(symbol: str, delay: float = 0.2) -> list[dict] | None:
    """Fetch K-line data via Tencent API."""
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,1000,qfq"
    try:
        resp = fetch_url(url, headers=TENCENT_HEADERS)
        if resp:
            raw = resp.decode("utf-8", errors="replace")
            json_str = raw[raw.index("{"):raw.rindex("}") + 1]
            data = json.loads(json_str)
            if "data" in data and symbol in data["data"]:
                day_data = data["data"][symbol]
            elif symbol in data:
                day_data = data[symbol]
            else:
                return None
            klines_raw = None
            if "day" in day_data:
                klines_raw = day_data["day"]
            elif "qfqday" in day_data:
                klines_raw = day_data["qfqday"]
            if not klines_raw:
                return None
            klines = []
            for k in klines_raw:
                if isinstance(k, list) and len(k) >= 6:
                    klines.append({
                        "date": k[0],
                        "open": safe_float(k[1], 0),
                        "close": safe_float(k[2], 0),
                        "high": safe_float(k[3], 0),
                        "low": safe_float(k[4], 0),
                        "volume": safe_float(k[5], 0),
                    })
            if delay > 0:
                time.sleep(delay)
            return klines
    except Exception as e:
        log(f"  [WARN] fetch_kline({symbol}): {e}")
    return None


def build_common_dates(klines_data: dict) -> list[str]:
    """Build a sorted list of dates present in ALL stocks' klines."""
    all_dates: list[set] = []
    for code, klines in klines_data.items():
        if klines and len(klines) >= 250:
            dates = {d["date"] for d in klines}
            all_dates.append(dates)
    if not all_dates:
        return []
    common = set.intersection(*all_dates) if len(all_dates) > 1 else all_dates[0]
    return sorted(common)


class DualMAGCBacktesterLimitUp:
    """Backtester for dual_ma_gc with limit-up filter."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = dict(DEFAULT_CONFIG)
        if cfg:
            self.cfg.update(cfg)
        self.fetcher = DataFetcher(self.cfg)
        self._universe_cache: list[dict] = []

    def fetch_universe(self) -> tuple[list[dict], dict[str, dict]]:
        """Fetch basic stock universe using DataFetcher (same as original backtest)."""
        log("▶ Fetching all A-share stocks from Sina...")
        all_stocks = self.fetcher.get_all_a_stocks()
        log(f"  Total: {len(all_stocks)}")

        filtered = []
        for s in all_stocks:
            name = s.get("name", "")
            price = s.get("price", 0)
            if is_st(name) or price <= 0:
                continue
            filtered.append(s)
        log(f"  After ST/price filter: {len(filtered)}")

        log("▶ Fetching market data (MV, circ shares)...")
        market_data = self.fetcher.get_market_data(
            filtered, batch_size=self.cfg.get("tencent_batch_size", 80)
        )
        log(f"  Market data: {len(market_data)} stocks")

        enriched = []
        for s in filtered:
            code = s["code"]
            md = market_data.get(code)
            if not md:
                continue
            mv = md.get("mv", 0)
            name = md.get("name", "")
            if mv <= 0 or mv > self.cfg["max_mv"]:
                continue
            total_shares = md.get("total_shares", 0)
            min_shares = self.cfg.get("min_total_shares", 0.5)
            max_shares = self.cfg.get("max_total_shares", 10)
            if total_shares <= 0 or total_shares < min_shares or total_shares > max_shares:
                continue
            if is_st(name):
                continue
            enriched.append({
                "code": code,
                "name": name,
                "mv": mv,
                "total_shares": total_shares,
                "amount": s.get("amount", 0),
                "price": md.get("price", 0),
                "changepercent": s.get("changepercent", 0),
            })

        enriched.sort(key=lambda x: x["amount"], reverse=True)
        universe = enriched[:self.cfg["universe_size"]]
        log(f"  Universe: {len(universe)} stocks (MV<={self.cfg['max_mv']}亿, "
            f"shares {self.cfg['min_total_shares']}~{self.cfg['max_total_shares']}亿)")

        md_map = {s["code"]: s for s in universe}
        self._universe_cache = universe
        self._md_map_cache = md_map
        return universe, md_map

    def fetch_all_klines(self, universe: list[dict]) -> dict[str, list[dict]]:
        """Fetch klines for all stocks in universe."""
        total = len(universe)
        klines_data: dict[str, list[dict]] = {}

        # 1b: Fetch index klines FIRST (avoid API rate limit after stocks)
        idx_sym = "sh000985"
        log(f"  [1b] Fetching CSI All-Share {idx_sym} klines...")
        index_kd = fetch_kline_with_dates(idx_sym, self.cfg.get("kline_delay", 0.2))
        if index_kd:
            klines_data[idx_sym] = index_kd
            log(f"    {idx_sym}: {len(index_kd)} klines ({index_kd[0]['date']}~{index_kd[-1]['date']})")

        log(f"  Fetching klines for {total} stocks...")
        delay = self.cfg.get("kline_delay", 0.2)
        batch = 0
        for i, s in enumerate(universe):
            code = s["code"]
            prefix = code_to_prefix(code)
            if not prefix:
                continue
            sym = f"{prefix}{code}"
            kd = fetch_kline_with_dates(sym, delay if (i + 1) % 30 == 0 else 0)

            # Fallback: try alternate prefix
            if kd is None or len(kd) < 50:
                alt_prefix = "sz" if prefix == "sh" else "sh"
                alt_sym = f"{alt_prefix}{code}"
                if alt_sym != sym:
                    kd = fetch_kline_with_dates(alt_sym, 0.05)

            if kd and len(kd) >= 50:
                klines_data[code] = kd
            if (i + 1) % 200 == 0:
                batch += 1
                log(f"    {i+1}/{total} done, {len(klines_data)} valid")

        log(f"  Total valid kline sets: {len(klines_data)}/{total}")
        return klines_data

    def run_strategy_at_date(
        self,
        klines_data: dict[str, list[dict]],
        md_map: dict[str, dict],
        target_date: str,
    ) -> list[dict]:
        """Run via DualMAGCLimitUp strategy class."""
        from strategies.base import StrategyContext
        from strategies.builtins.dual_ma_gc_limitup import DualMAGCLimitUp

        truncated_klines: dict[str, list[dict]] = {}
        for code, klines in klines_data.items():
            hist = get_kline_series(klines, target_date, lookback=120)
            if hist and len(hist) >= 25:
                truncated_klines[code] = hist
            else:
                truncated_klines[code] = []

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

        strategy = DualMAGCLimitUp()
        picks = strategy.run(context)

        result = []
        for pick in picks:
            result.append({
                "code": pick["code"],
                "name": pick["name"],
                "close": pick["price"],
                "mv": pick["mv"],
                "ma5": pick.get("ma5", 0),
                "ma18": pick.get("ma18", 0),
                "score": pick.get("score", 0),
            })
        return result

    def check_sell_conditions(
        self,
        klines: list[dict],
        position: Position,
        current_date: str,
    ) -> bool:
        hist = get_kline_series(klines, current_date, lookback=60)
        if hist is None or len(hist) < 25:
            return False

        closes = [d["close"] for d in hist]
        close = closes[-1]

        ma5 = calc_ma(closes, self.cfg["ma_short"])
        ma18 = calc_ma(closes, self.cfg["ma_stop"])

        if ma5 <= 0 or ma18 <= 0:
            return False

        buy_price = position.buy_price

        # 跌停不卖
        if is_limit_down_from_klines(klines, current_date, position.code):
            return False

        # P1: Price < 94% of buy price
        stoploss_price = buy_price * self.cfg.get("stoploss_pct", 0.94)
        if close <= stoploss_price:
            position.sell_all(current_date, close, "SL_94pct")
            return True

        # P2: MA18 breakdown
        if close < ma18:
            position.sell_all(current_date, close, "MA18_below")
            return True

        # P3/4: half-sell conditions
        if position.units >= 2:
            tp_price = ma5 * self.cfg["take_profit_mult"]
            if close > tp_price:
                position.sell_half(current_date, close, "TP_MA5x1.3")
                return False
            if close < ma5:
                position.sell_half(current_date, close, "SL_below_MA5")
                return False
        return False

    def run(self) -> BacktestResult:
        result = BacktestResult(self.cfg)
        universe, md_map = self.fetch_universe()
        if not universe:
            log("[ERROR] Empty universe, aborting.")
            return result

        # Pre-compute kline delay: 0 for backtest (we fetch once, not per strategy run)
        self.cfg["kline_request_delay"] = 0
        klines_data = self.fetch_all_klines(universe)
        if not klines_data:
            log("[ERROR] No kline data, aborting.")
            return result

        # Build common dates for index
        idx_sym = "sh000985"
        idx_klines = klines_data.get(idx_sym)
        if not idx_klines:
            log("[ERROR] Index klines missing, aborting.")
            return result

        all_dates = sorted(set(d["date"] for d in idx_klines))
        if len(all_dates) < 50:
            log(f"[ERROR] Too few index dates ({len(all_dates)}), aborting.")
            return result

        days = self.cfg.get("backtest_days", 120)
        if days > len(all_dates) - 30:
            days = len(all_dates) - 30

        backtest_dates = all_dates[-days:]
        rebalance_freq = self.cfg.get("rebalance_freq_days", 5)
        rebalance_dates = backtest_dates[::rebalance_freq]

        log(f"Backtest: {len(backtest_dates)} days, "
            f"{len(rebalance_dates)} rebalances "
            f"({backtest_dates[0]} ~ {backtest_dates[-1]})")

        # Main loop
        positions: list[Position] = []
        bt_result = BacktestResult(self.cfg)
        skipped = 0

        for i, current_date in enumerate(backtest_dates):
            date_idx = all_dates.index(current_date)

            # Check sell conditions for all positions
            for pos in list(positions):
                if pos.units <= 0:
                    positions.remove(pos)
                    continue
                klines = klines_data.get(pos.code)
                if klines is None:
                    continue
                closed = self.check_sell_conditions(klines, pos, current_date)
                if closed:
                    positions.remove(pos)

            # Check if rebalance day
            is_rebalance = current_date in rebalance_dates

            if is_rebalance and len(positions) == 0:
                # Need to check market regime
                idx_close = None
                for d in idx_klines:
                    if d["date"] == current_date:
                        idx_close = d["close"]
                        break
                if idx_close is None:
                    skipped += 1
                    continue

                # Check MA20 market filter
                hist_idx = get_kline_series(idx_klines, current_date, lookback=60)
                if hist_idx and len(hist_idx) >= 21:
                    idx_ma20 = sum(d["close"] for d in hist_idx[-20:]) / 20
                    if idx_close < idx_ma20:
                        skipped += 1
                        continue

                # Run strategy on this date
                picks = self.run_strategy_at_date(klines_data, md_map, current_date)
                if not picks:
                    skipped += 1
                    continue

                # Buy top N (configured)
                buy_count = self.cfg.get("buy_count", 5)
                for pick in picks[:buy_count]:
                    code = pick["code"]
                    price = pick["close"]

                    # Find next trading day for actual buy
                    buy_date = current_date
                    for j in range(date_idx + 1, min(date_idx + 10, len(all_dates))):
                        next_date = all_dates[j]
                        # T+1 buy on next trading day
                        buy_date = next_date
                        break

                    # Get kline series on buy_date for actual price
                    klines = klines_data.get(code)
                    if klines is None:
                        continue
                    buy_hist = get_kline_series(klines, buy_date, lookback=5)
                    if buy_hist is None:
                        continue

                    # T+1 rule: buy on next day
                    actual_price = buy_hist[-1]["close"]
                    if actual_price <= 0:
                        continue

                    # 涨停不买
                    if is_limit_up_from_klines(klines, buy_date, code):
                        log(f"    [SKIP] {code} at 涨停 on {buy_date}, skipping buy")
                        continue

                    # Check if buy_date already processed
                    buy_date_idx = all_dates.index(buy_date) if buy_date in all_dates else -1
                    if buy_date_idx < 0:
                        continue

                    pos = Position(code, actual_price, buy_date, units=2)
                    positions.append(pos)
                    log(f"  BUY {code} {pick.get('name','')} @ {actual_price:.2f} on {buy_date}")
                    if self.cfg.get("max_positions", 0) > 0 and len(positions) >= self.cfg["max_positions"]:
                        break
            elif is_rebalance and len(positions) > 0:
                pass  # already have positions, skip

            # End of backtest: force close all positions
            if i == len(backtest_dates) - 1:
                for pos in list(positions):
                    klines = klines_data.get(pos.code)
                    if klines:
                        hist = get_kline_series(klines, current_date, lookback=5)
                        if hist:
                            close = hist[-1]["close"]
                            bt_result.trades.append(
                                pos.sell_all(current_date, close, "end_of_backtest")
                            )
                    positions.remove(pos)

        bt_result.compute()
        bt_result._rebalance_count = len(rebalance_dates)
        bt_result._skipped = skipped
        return bt_result


def run_backtest(cfg: dict) -> BacktestResult:
    bt = DualMAGCBacktesterLimitUp(cfg)
    result = bt.run()
    return result


def print_report(result: BacktestResult, label="dual_ma_gc_limitup"):
    print(f"\n{'='*70}")
    print(f"【{label} 回测结果】")
    print(f"{'='*70}")
    total = result.total_trades or 1
    print(f"  回测天数:        {result.cfg.get('backtest_days', 0)}")
    print(f"  再平衡次数:      {getattr(result, '_rebalance_count', 0)}")
    print(f"  市场过滤跳过:    {getattr(result, '_skipped', 0)}")
    print(f"  总交易次数:      {result.total_trades}")
    print(f"  胜率:            {result.win_rate:.1f}%")
    print(f"  平均单笔收益:    {result.avg_return_pct:.2f}%")
    print(f"  总收益率:        {result.total_return_pct:+.2f}%")
    print(f"  最大回撤:        {result.max_drawdown:.2f}%")
    if hasattr(result, 'sharpe_ratio'):
        print(f"  夏普比率:        {result.sharpe_ratio:.2f}")

    if result.trades:
        by_reason = Counter(t.reason for t in result.trades)
        print(f"\n  退出原因分布:")
        for reason, count in by_reason.most_common():
            reason_returns = [t.return_pct for t in result.trades if t.reason == reason]
            avg_r = sum(reason_returns) / len(reason_returns) if reason_returns else 0
            print(f"    {reason:<20} {count:>4}笔  平均{avg_r:+.2f}%")
        winners = [t for t in result.trades if t.return_pct > 0]
        losers = [t for t in result.trades if t.return_pct <= 0]
        if winners:
            print(f"\n  盈利交易: {len(winners)}笔")
            print(f"    最大盈利: {max(t.return_pct for t in winners):+.2f}%")
            print(f"    平均盈利: {sum(t.return_pct for t in winners)/len(winners):+.2f}%")
        if losers:
            print(f"  亏损交易: {len(losers)}笔")
            print(f"    最大亏损: {min(t.return_pct for t in losers):+.2f}%")
            print(f"    平均亏损: {sum(t.return_pct for t in losers)/len(losers):+.2f}%")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="dual_ma_gc + limitup filter backtest")
    parser.add_argument("--universe", type=int, default=500, help="Universe size")
    parser.add_argument("--days", type=int, default=120, help="Backtest days")
    parser.add_argument("--freq", type=int, default=5, help="Rebalance frequency (days)")
    parser.add_argument("--buy", type=int, default=5, help="Buy count per rebalance")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    parser.add_argument("--stoploss", type=float, default=0.94, help="Stop loss pct")
    parser.add_argument("--tp", type=float, default=1.3, help="Take profit multiplier")
    parser.add_argument("--max-pos", type=int, default=0, help="Max positions")
    parser.add_argument("--output", "-o", type=str, default="", help="Output file name")
    args = parser.parse_args()

    cfg = {
        "universe_size": args.universe,
        "backtest_days": args.days,
        "rebalance_freq_days": args.freq,
        "buy_count": args.buy,
        "stoploss_pct": args.stoploss,
        "take_profit_mult": args.tp,
        "max_positions": args.max_pos if args.max_pos > 0 else args.buy,
        "kline_delay": 0.15,
    }

    log("Starting dual_ma_gc_limitup backtest...")
    result = run_backtest(cfg)
    print_report(result)

    if args.save:
        import json
        out_name = args.output or f"backtest_dual_ma_gc_limitup.json"
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backtest_results")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, out_name)
        with open(out_path, "w") as f:
            json.dump({
                "config": cfg,
                "total_trades": result.total_trades,
                "win_rate": round(result.win_rate, 1),
                "avg_return_pct": round(result.avg_return_pct, 2),
                "total_return_pct": round(result.total_return_pct, 2),
                "max_drawdown": round(result.max_drawdown, 2),
                "sharpe_ratio": round(result.sharpe_ratio, 2) if hasattr(result, 'sharpe_ratio') else 0,
                "trades": [t.to_dict() for t in result.trades],
            }, f, indent=2, ensure_ascii=False)
        log(f"Results saved to {out_path}")
