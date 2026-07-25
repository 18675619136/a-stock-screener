#!/usr/bin/env python3
"""
Backtester for dual_ma_gc strategy — MA5上穿MA18买入 + 热门赛道 + 动态卖出.

Buy:
  - Strict MA5上穿MA18 golden cross
  - MV < 1000亿, 总股本 0.5~10亿

Sell (逐日监控):
  1. price < buy_price × 0.94 → 卖出全部 (hard stop loss)
  2. close < MA18 → 卖出全部 (hard stop)
  3. close > MA5 × 1.3 → 卖出一半 (take profit)
  4. close < MA5 → 卖出一半 (trailing stop)

Usage:
    python3 -m strategies.backtest_dual_ma_gc --help
    python3 -m strategies.backtest_dual_ma_gc --universe 1000 --save
"""

import sys
import os
import json
import time
from collections import Counter
from datetime import datetime
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

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
    "ma_stop": 10,       # MA for hard stop (MA10 — 买入看MA18金叉, 清仓看MA10)
    "gc_lookback_days": 3,  # golden cross within last N days
    "top_n": 30,
    "take_profit_mult": 1.3,   # price > MA5 * 1.3 → sell half
    "stoploss_pct": 0.94,      # price < buy_price * 0.94 → sell all
    "kline_delay": 0.2,
}

COST = 0.001
PEAK_TP_PCT = 1.15
ST_NAME_PREFIXES = ("ST", "*ST", "S")

# ── 涨停/跌停辅助函数 ──
def get_limit_pct(code: str) -> float:
    """返回涨跌幅限制比例"""
    if code.startswith(("300", "688")):
        return 0.20  # 创业板/科创板 20%
    elif code.startswith(("8", "4", "92")):
        return 0.30  # 北交所 30%
    else:
        return 0.10  # 主板 10%

def is_limit_up_from_klines(klines: list[dict], date: str, code: str) -> bool:
    """Check if the stock was at 涨停 on the given date, using kline data."""
    hist = get_kline_series(klines, date, lookback=3)
    if hist is None or len(hist) < 2:
        return False
    today_close = hist[-1]["close"]
    yest_close = hist[-2]["close"]
    if yest_close <= 0:
        return False
    limit_pct = get_limit_pct(code)
    return today_close >= yest_close * (1 + limit_pct) * 0.995

def is_limit_down_from_klines(klines: list[dict], date: str, code: str) -> bool:
    """Check if the stock was at 跌停 on the given date, using kline data."""
    hist = get_kline_series(klines, date, lookback=3)
    if hist is None or len(hist) < 2:
        return False
    today_close = hist[-1]["close"]
    yest_close = hist[-2]["close"]
    if yest_close <= 0:
        return False
    limit_pct = get_limit_pct(code)
    return today_close <= yest_close * (1 - limit_pct) * 1.005


def is_st(name: str) -> bool:
    return name.startswith(ST_NAME_PREFIXES) or "退" in name


class TradeRecord:
    """A single trade (represents half or full position)."""
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
        # Portfolio-level: group by buy_date batch, average returns within batch
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

    def summary(self) -> str:
        lines = [
            "╔══════════════════════════════════════════════╗",
            "║   dual_ma_gc Backtest (MA5/18 + dynamic sell) ║",
            "╠══════════════════════════════════════════════╣",
            f"║  Trades:      {self.total_trades:<5d}                         ║",
            f"║  Win Rate:    {self.win_rate:<6.1f}%                     ║",
            f"║  Total Ret:   {self.total_return_pct:<7.2f}%                ║",
            f"║  Avg/Trade:   {self.avg_return_pct:<7.2f}%                ║",
            f"║  Max DD:      {self.max_drawdown:<7.2f}%                ║",
            f"║  Sharpe:      {getattr(self, 'sharpe_ratio', 0):<7.2f}                   ║",
            f"╚══════════════════════════════════════════════╝",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": round(self.win_rate, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "avg_return_per_trade_pct": round(self.avg_return_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown, 2),
            "sharpe_ratio": round(getattr(self, 'sharpe_ratio', 0), 2),
            "trades": [t.to_dict() for t in self.trades],
        }


# ── K-line helpers (same as before) ────────────────────────────────

def fetch_kline_with_dates(sym: str, config: dict) -> list[dict] | None:
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,500,qfq"
    headers = {**TENCENT_HEADERS, "Referer": "https://gu.qq.com"}
    for attempt in range(2):
        raw = fetch_url(url, headers=headers, timeout=config.get("timeout_kline", 10))
        if not raw or len(raw) < 50:
            time.sleep(1)
            continue
        try:
            parsed = json.loads(raw)
            data = parsed.get("data", {})
            target_key = None
            for k in data:
                if sym.replace("/", "") in k:
                    target_key = k
                    break
            if not target_key:
                return None
            klines = data[target_key].get("qfqday", data[target_key].get("day", []))
            if not klines or len(klines) < 5:
                return None
            result = []
            for e in klines:
                if len(e) >= 6:
                    try:
                        result.append({
                            "date": str(e[0]),
                            "open": float(e[1]),
                            "close": float(e[2]),
                            "high": float(e[3]),
                            "low": float(e[4]),
                            "volume": float(e[5]) if e[5] else 0,
                        })
                    except (ValueError, IndexError):
                        continue
            return result
        except (json.JSONDecodeError, KeyError, IndexError):
            time.sleep(1.5)
    return None


def build_common_dates(klines_data: dict[str, list[dict]]) -> list[str]:
    date_set = set()
    for code, klines in klines_data.items():
        for k in klines:
            date_set.add(k["date"])
    return sorted(date_set)


def get_kline_series(klines: list[dict], target_date: str, lookback: int = 120) -> list[dict] | None:
    idx = None
    for i, k in enumerate(klines):
        if k["date"] == target_date:
            idx = i
            break
    if idx is None:
        return None
    start = max(0, idx - lookback + 1)
    return klines[start:idx + 1]


# ── Position tracking ──────────────────────────────────────────────

class Position:
    """Tracks a single stock position with partial-sell support."""
    def __init__(self, code: str, buy_date: str, buy_price: float,
                 name: str = "", mv: float = 0):
        self.code = code
        self.buy_date = buy_date
        self.buy_price = buy_price
        self.name = name
        self.mv = mv
        self.units = 2  # start with 2 halves = full position
        self.closed_trades: list[TradeRecord] = []

    def sell_half(self, sell_date: str, sell_price: float, reason: str):
        """Sell 1 unit (half position)."""
        if self.units <= 0:
            return
        ret = (sell_price - self.buy_price) / self.buy_price * 100 - COST * 200
        days = _date_diff(self.buy_date, sell_date)
        trade = TradeRecord(
            code=self.code, buy_date=self.buy_date, buy_price=self.buy_price,
            sell_date=sell_date, sell_price=sell_price,
            return_pct=ret, holding_days=days, reason=reason,
        )
        self.closed_trades.append(trade)
        self.units -= 1

    def sell_all(self, sell_date: str, sell_price: float, reason: str):
        """Sell all remaining units."""
        while self.units > 0:
            self.sell_half(sell_date, sell_price, reason)

    @property
    def is_closed(self) -> bool:
        return self.units <= 0


def _date_diff(d1: str, d2: str) -> int:
    """Approximate trading day diff."""
    try:
        a = datetime.strptime(d1, "%Y-%m-%d")
        b = datetime.strptime(d2, "%Y-%m-%d")
        return (b - a).days
    except ValueError:
        return 0


# ── MA calculation ─────────────────────────────────────────────────

def calc_ma(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return sum(closes) / len(closes) if closes else 0
    return sum(closes[-period:]) / period


# ── Backtester ────────────────────────────────────────────────────

class DualMAGoldenCrossBacktester:
    def __init__(self, config: dict | None = None):
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.fetcher = DataFetcher(self.cfg)
        self._universe_cache: list[dict] = []
        self._md_map_cache: dict[str, dict] = {}

    def fetch_universe(self) -> tuple[list[dict], dict[str, dict]]:
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
        log(f"  Universe: {len(universe)} stocks (MV<={self.cfg['max_mv']}亿, shares {self.cfg['min_total_shares']}~{self.cfg['max_total_shares']}亿)")

        md_map = {s["code"]: s for s in universe}
        self._universe_cache = universe
        self._md_map_cache = md_map
        return universe, md_map

    def fetch_klines(self, universe: list[dict]) -> dict[str, list[dict]]:
        log(f"▶ Fetching klines for {len(universe)} stocks...")
        klines_data = {}
        delay = self.cfg.get("kline_delay", 0.2)

        for i, s in enumerate(universe):
            code = s["code"]
            prefix = code_to_prefix(code)
            if not prefix:
                continue
            sym = f"{prefix}{code}"
            kd = fetch_kline_with_dates(sym, self.cfg)
            if kd and len(kd) >= 60:
                klines_data[code] = kd
            if (i + 1) % 50 == 0:
                log(f"  Klines: {i+1}/{len(universe)}, ok: {len(klines_data)}")
            if delay > 0:
                time.sleep(delay)

        log(f"  Klines fetched: {len(klines_data)} stocks")
        return klines_data

    def run_strategy_at_date(
        self,
        klines_data: dict[str, list[dict]],
        md_map: dict[str, dict],
        target_date: str,
    ) -> list[dict]:
        """Run via DualMAGoldenCross strategy class (v3: recent GC + quality scoring)."""
        from strategies.base import StrategyContext
        from strategies.builtins.dual_ma_gc import DualMAGoldenCross

        # Truncate klines to avoid look-ahead
        truncated_klines: dict[str, list[dict]] = {}
        for code, klines in klines_data.items():
            hist = get_kline_series(klines, target_date, lookback=120)
            if hist and len(hist) >= 25:
                truncated_klines[code] = hist
            else:
                truncated_klines[code] = []  # marker

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

        strategy = DualMAGoldenCross()
        picks = strategy.run(context)

        # Map to backtest format
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
        """Check and execute sell conditions for one position on one date.
        Returns True if position was fully closed."""
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

        # ── Check if stock is at 跌停 (cannot sell) ──
        if is_limit_down_from_klines(klines, current_date, position.code):
            return False

        # Priority 1: Price < 94% of buy price → sell all (hard stop loss)
        stoploss_price = buy_price * self.cfg.get("stoploss_pct", 0.94)
        if close <= stoploss_price:
            position.sell_all(current_date, close, "SL_94pct")
            return True

        # Priority 2: MA18 breakdown → sell all
        if close < ma18:
            position.sell_all(current_date, close, "MA18_below")
            return True

        # Priority 3: half-sell conditions (only if still has 2 units)
        if position.units >= 2:
            tp_price = ma5 * self.cfg["take_profit_mult"]
            if close > tp_price:
                position.sell_half(current_date, close, "TP_MA5x1.3")
                return False  # still has the other half
            if close < ma5:
                position.sell_half(current_date, close, "SL_below_MA5")
                return False  # still has the other half

        return False

    def run(self) -> BacktestResult:
        result = BacktestResult(self.cfg)

        # 1. Fetch universe
        universe, md_map = self.fetch_universe()
        if not universe:
            log("ERROR: Empty universe, cannot backtest.")
            return result

        # 1b. Fetch CSI All-Share index klines FIRST (before stock klines, to avoid API rate limiting)
        idx_sym = "sh000985"
        idx_klines = fetch_kline_with_dates(idx_sym, self.cfg)
        index_code = "sh000985"
        idx_prefix = "sh"
        if idx_klines and len(idx_klines) >= 60:
            log(f"  Index klines (sh000985): {len(idx_klines)} days, {idx_klines[0]['date']} ~ {idx_klines[-1]['date']}")
        else:
            log("  WARN: No index klines, market filter disabled")
            idx_prefix = None

        # 2. Fetch stock klines
        klines_data = self.fetch_klines(universe)
        if not klines_data:
            log("ERROR: No kline data, cannot backtest.")
            return result

        # Store index klines in the same dict (won't conflict with stock codes)
        if idx_klines and len(idx_klines) >= 60:
            klines_data[index_code] = idx_klines

        # 3. Build date axis
        all_dates = build_common_dates(klines_data)
        log(f"  Total unique trading days: {len(all_dates)}")
        if len(all_dates) < 150:
            log("ERROR: Too few trading days.")
            return result

        # 4. Determine backtest period
        backtest_days = self.cfg["backtest_days"]
        if backtest_days >= len(all_dates):
            backtest_days = len(all_dates) // 2
        start_idx = len(all_dates) - backtest_days
        backtest_dates = all_dates[start_idx:]

        rebalance_freq = self.cfg["rebalance_freq_days"]
        rebalance_indices = list(range(0, len(backtest_dates), rebalance_freq))
        rebalance_dates = [backtest_dates[i] for i in rebalance_indices]

        log(f"  Period: {backtest_dates[0]} → {backtest_dates[-1]}")
        log(f"  Rebalance dates: {len(rebalance_dates)}")

        # 6. Run simulation
        all_trades: list[TradeRecord] = []
        open_positions: list[Position] = []  # (code, Position)

        # Build a date-to-index map for quick lookup
        date_to_idx = {d: i for i, d in enumerate(backtest_dates)}

        # ── Market state check helper ──────────────────────
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

        for ri, buy_date in enumerate(rebalance_dates):
            # ── Market filter: skip rebalance in bear market ────────
            if not self.cfg.get("no_market_filter", False) and idx_prefix and is_bear_market(buy_date):
                log(f"  [SKIP] {buy_date}: bear market (index < MA20)")
                skipped_rebalances += 1
                continue

            # ── Buy signals ─────────────────────────────────────────
            picks = self.run_strategy_at_date(klines_data, md_map, buy_date)
            if picks:
                log(f"  [BUY] {buy_date}: {len(picks)} picks")
                for pick in picks:
                    code = pick["code"]
                    # ── Skip if stock is at 涨停 on buy date ──
                    klines = klines_data.get(code)
                    if klines and is_limit_up_from_klines(klines, buy_date, code):
                        log(f"    [SKIP] {code} at 涨停 on {buy_date}, skipping buy")
                        continue
                    pos = Position(
                        code=code, buy_date=buy_date,
                        buy_price=pick["close"],
                        name=pick.get("name", ""),
                        mv=pick.get("mv", 0),
                    )
                    open_positions.append(pos)

            # ── Daily sell checks for all open positions ────────────
            # Only check days AFTER this rebalance date
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
                        # Can't check, keep open
                        still_open.append(pos)
                        continue
                    self.check_sell_conditions(klines, pos, current_date)
                    if not pos.is_closed:
                        still_open.append(pos)
                    else:
                        all_trades.extend(pos.closed_trades)
                open_positions = still_open

            log(f"  Open positions: {len(open_positions)}")

        # 7. Close remaining positions at last available date
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

        if skipped_rebalances > 0:
            total = len(rebalance_dates)
            log(f"  Market filter: skipped {skipped_rebalances}/{total} "
                f"rebalances ({skipped_rebalances*100//total}% bear market)")

        return result


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Backtest dual_ma_gc (MA5/18 golden cross + hot sector + dynamic sell) strategy",
    )
    parser.add_argument("--universe", type=int, default=1000,
                        help="Top N stocks by amount to use")
    parser.add_argument("--top-n", type=int, default=30,
                        help="Max stocks to pick per rebalance")
    parser.add_argument("--freq", type=int, default=5,
                        help="Rebalance frequency in trading days")
    parser.add_argument("--days", type=int, default=120,
                        help="Backtest period in trading days")
    parser.add_argument("--delay", type=float, default=0.2,
                        help="Delay between kline requests (seconds)")
    parser.add_argument("--tp", type=float, default=1.3,
                        help="Take profit multiplier (price > MA5 * N)")
    parser.add_argument("--stoploss", type=float, default=0.94,
                        help="Hard stop loss as fraction of buy price")
    parser.add_argument("--save", "-s", action="store_true",
                        help="Save results to JSON")
    parser.add_argument("--no-market-filter", action="store_true",
                        help="Disable bear market skip filter")
    args = parser.parse_args()

    bt_config = {
        "universe_size": args.universe,
        "top_n": args.top_n,
        "rebalance_freq_days": args.freq,
        "backtest_days": args.days,
        "kline_delay": args.delay,
        "take_profit_mult": args.tp,
        "stoploss_pct": args.stoploss,
    }
    if args.no_market_filter:
        bt_config["no_market_filter"] = True

    bt = DualMAGoldenCrossBacktester(bt_config)
    t0 = time.time()
    result = bt.run()
    elapsed = time.time() - t0

    print("\n" + result.summary())
    print(f"\nBacktest completed in {elapsed:.1f}s")

    if args.save:
        output_dir = os.path.join(PROJECT_DIR, "backtest_results")
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "dual_ma_gc_backtest.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"Results saved to {path}")


if __name__ == "__main__":
    main()
