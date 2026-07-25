#!/usr/bin/env python3
"""
Backtester for dual_ma_gc with modified sell rules:
  1. 硬止损: price < buy_price × 0.95 → sell_all  (原0.94)
  2. MA18破位: close < MA18 → sell_all (不变)
  3. MA5×1.3止盈: close > MA5×1.3 → sell_half (不变)
  4. MA5破位: close < MA5 → **sell SIXTY PERCENT (60%)**  (原50%)

Position用units=5表示满仓:
  sell_sixty → sell 3 units (60%), keep 2 units (40%)
  sell_rest  → sell remaining 2 units
  sell_all   → sell all 5 units

Usage:
    python3 -m strategies.backtest_dual_ma_gc_sell60 --universe 500 --days 120 --freq 5 --save
"""
import sys, os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import time, json
from collections import Counter
from datetime import datetime
from typing import Any

from strategies.data.fetcher import (
    DataFetcher, log, safe_float, code_to_prefix, fetch_url,
    SINA_HEADERS, SINA_ALL_URL, TENCENT_HEADERS,
)

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

COST = 0.001

# ── Modified Position class ─────────────────────
class Position60:
    """Position with units=5 for 60% sell support."""
    __slots__ = ("code", "buy_price", "buy_date", "units", "shares_per_unit")
    def __init__(self, code: str, buy_price: float, buy_date: str,
                 units: int = 5, shares_per_unit: int = 100):
        self.code = code
        self.buy_price = buy_price
        self.buy_date = buy_date
        self.units = units
        self.shares_per_unit = shares_per_unit

    def sell_half(self, sell_date: str, sell_price: float, reason: str):
        """Sell 2 units (40%)."""
        to_sell = min(self.units, 2)
        self.units -= to_sell
        ret = (sell_price - self.buy_price) / self.buy_price * 100
        return TradeRecord60(self.code, self.buy_date, self.buy_price,
                             sell_date, sell_price, ret, 0, f"{reason}(-40%)")

    def sell_sixty(self, sell_date: str, sell_price: float, reason: str):
        """Sell 3 units (60%)."""
        to_sell = min(self.units, 3)
        self.units -= to_sell
        ret = (sell_price - self.buy_price) / self.buy_price * 100
        return TradeRecord60(self.code, self.buy_date, self.buy_price,
                             sell_date, sell_price, ret, 0, f"{reason}(-60%)")

    def sell_all(self, sell_date: str, sell_price: float, reason: str):
        """Sell all remaining units."""
        sold_units = self.units
        self.units = 0
        ret = (sell_price - self.buy_price) / self.buy_price * 100
        return TradeRecord60(self.code, self.buy_date, self.buy_price,
                             sell_date, sell_price, ret, 0, reason)

    def sell_rest(self, sell_date: str, sell_price: float, reason: str):
        """Sell remaining units (when already partial)."""
        return self.sell_all(sell_date, sell_price, reason)


class TradeRecord60:
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


# ── Reuse from original backtest ────────────────
DEFAULT_CONFIG = {
    "universe_size": 500,
    "rebalance_freq_days": 5,
    "backtest_days": 120,
    "max_mv": 1000,
    "min_total_shares": 0.5,
    "max_total_shares": 10,
    "ma_short": 10,
    "ma_long": 30,
    "ma_stop": 30,
    "gc_lookback_days": 3,
    "top_n": 30,
    "take_profit_mult": 1.3,
    "stoploss_pct": 0.95,       # ← CHANGED: 0.94 → 0.95
    "kline_delay": 0.2,
}

COST = 0.001
ST_NAME_PREFIXES = ("ST", "*ST", "S")


def is_st(name: str) -> bool:
    return name.startswith(ST_NAME_PREFIXES) or "退" in name


# ── TradeRecord / BacktestResult (same as original) ──
class BacktestResult:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.trades: list[TradeRecord60] = []

    @property
    def total_trades(self): return len(self.trades)

    @property
    def winning_trades(self): return sum(1 for t in self.trades if t.return_pct > 0)

    @property
    def losing_trades(self): return self.total_trades - self.winning_trades

    @property
    def win_rate(self):
        return self.winning_trades / self.total_trades * 100 if self.total_trades > 0 else 0

    @property
    def avg_return_pct(self):
        return sum(t.return_pct for t in self.trades) / self.total_trades if self.total_trades > 0 else 0

    @property
    def total_return_pct(self):
        by_batch: dict[str, list[float]] = {}
        for t in self.trades:
            by_batch.setdefault(t.buy_date, []).append(t.return_pct)
        cum = 1.0
        for bdate in sorted(by_batch):
            batch_avg = sum(by_batch[bdate]) / len(by_batch[bdate])
            cum *= (1 + batch_avg / 100)
        return (cum - 1.0) * 100

    @property
    def max_drawdown(self):
        by_batch: dict[str, list[float]] = {}
        for t in self.trades:
            by_batch.setdefault(t.buy_date, []).append(t.return_pct)
        cum = 1.0
        peak = 1.0
        dd = 0.0
        for bdate in sorted(by_batch):
            batch_avg = sum(by_batch[bdate]) / len(by_batch[bdate])
            cum *= (1 + batch_avg / 100)
            if cum > peak:
                peak = cum
            dd = max(dd, (peak - cum) / peak)
        return dd * 100

    @property
    def sharpe_ratio(self):
        by_batch: dict[str, list[float]] = {}
        for t in self.trades:
            by_batch.setdefault(t.buy_date, []).append(t.return_pct)
        port_rets = []
        for bdate in sorted(by_batch):
            port_rets.append(sum(by_batch[bdate]) / len(by_batch[bdate]))
        if len(port_rets) < 2: return 0.0
        import statistics
        avg_r = sum(port_rets) / len(port_rets)
        std_r = statistics.stdev(port_rets)
        if std_r < 1e-10: return 0.0
        freq = self.cfg.get("rebalance_freq_days", 5)
        return (avg_r / std_r) * (252.0 / freq) ** 0.5

    def summary(self) -> str:
        return (
            f"\n{'═'*50}"
            f"\n  dual_ma_gc 卖六成+止损95%"
            f"\n{'═'*50}"
            f"\n  总交易:      {self.total_trades}"
            f"\n  胜率:        {self.win_rate:.1f}%"
            f"\n  总收益:      {self.total_return_pct:+.2f}%"
            f"\n  平均每笔:    {self.avg_return_pct:+.2f}%"
            f"\n  最大回撤:    {self.max_drawdown:.2f}%"
            f"\n  夏普比率:    {self.sharpe_ratio:.2f}"
        )

    def to_dict(self):
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": round(self.win_rate, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "avg_return_per_trade_pct": round(self.avg_return_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "trades": [t.to_dict() for t in self.trades],
        }


# ── K-line & backtest engine (reused from original) ──
def calc_ma(closes: list[float], period: int) -> float:
    if len(closes) < period: return 0.0
    return sum(closes[-period:]) / period


def get_kline_series(klines: list[dict], target_date: str, lookback: int = 60) -> list[dict] | None:
    if not klines: return None
    for i, d in enumerate(klines):
        if d.get("date", "") >= target_date:
            start = max(0, i - lookback + 1)
            result = klines[start:i + 1]
            return result if len(result) >= 20 else None
    return None


def fetch_kline_with_dates(sym: str, config: dict) -> list[dict] | None:
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,500,qfq"
    headers = {**TENCENT_HEADERS, "Referer": "https://gu.qq.com"}
    for attempt in range(2):
        raw = fetch_url(url, headers=headers, timeout=10)
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
            if not target_key: return None
            day_data = data[target_key]
            klines_raw = day_data.get("qfqday") or day_data.get("day")
            if not klines_raw: return None
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
            if klines and attempt == 0:
                time.sleep(config.get("kline_delay", 0.2) if config else 0.2)
            return klines
        except Exception as e:
            log(f"  [WARN] fetch({sym}): {e}")
    return None


class BacktesterSell60:
    """dual_ma_gc backtester with modified sell rules."""
    
    def __init__(self, cfg: dict | None = None):
        self.cfg = dict(DEFAULT_CONFIG)
        if cfg:
            self.cfg.update(cfg)
        self.fetcher = DataFetcher(self.cfg)
        self._universe_cache: list[dict] = []
        self._md_map_cache: dict[str, dict] = {}

    def fetch_universe(self):
        log("Fetching A-share stocks...")
        all_stocks = self.fetcher.get_all_a_stocks()
        log(f"  Total: {len(all_stocks)}")
        filtered = [s for s in all_stocks if not is_st(s.get("name","")) and s.get("price",0) > 0]
        log(f"  After ST/price: {len(filtered)}")
        log("Fetching market data...")
        md_data = self.fetcher.get_market_data(filtered, batch_size=80)
        log(f"  Market data: {len(md_data)} stocks")
        enriched = []
        for s in filtered:
            code = s["code"]
            md = md_data.get(code) or {}
            mv = md.get("mv", 0)
            name = md.get("name", "")
            if mv <= 0 or mv > self.cfg["max_mv"]: continue
            ts = md.get("total_shares", 0)
            if ts <= 0 or ts < self.cfg["min_total_shares"] or ts > self.cfg["max_total_shares"]: continue
            if is_st(name): continue
            enriched.append({"code": code, "name": name, "mv": mv,
                             "total_shares": ts, "amount": s.get("amount", 0),
                             "price": md.get("price", 0),
                             "changepercent": s.get("changepercent", 0)})
        enriched.sort(key=lambda x: x["amount"], reverse=True)
        universe = enriched[:self.cfg["universe_size"]]
        md_map = {s["code"]: s for s in universe}
        self._universe_cache = universe
        self._md_map_cache = md_map
        log(f"  Universe: {len(universe)} stocks")
        return universe, md_map

    def fetch_klines(self, universe):
        log(f"Fetching klines for {len(universe)} stocks...")
        kd_data = {}
        # Index first
        idx_kd = fetch_kline_with_dates("sh000985", self.cfg)
        if idx_kd:
            kd_data["sh000985"] = idx_kd
            log(f"  sh000985: {len(idx_kd)} klines")
        delay = self.cfg.get("kline_delay", 0.2)
        for i, s in enumerate(universe):
            code = s["code"]
            prefix = code_to_prefix(code)
            if not prefix: continue
            sym = f"{prefix}{code}"
            kd = fetch_kline_with_dates(sym, self.cfg if (i+1) % 30 == 0 else {"kline_delay": 0, "kline_delay": 0})
            if kd and len(kd) >= 50:
                kd_data[code] = kd
            if (i+1) % 200 == 0:
                log(f"    {i+1}/{len(universe)}, valid: {len(kd_data)}")
        log(f"  Valid: {len(kd_data)}/{len(universe)}")
        return kd_data

    def run_strategy(self, kd_data, md_map, target_date):
        from strategies.base import StrategyContext
        from strategies.builtins.dual_ma_gc import DualMAGoldenCross
        truncated = {}
        for code, klines in kd_data.items():
            hist = get_kline_series(klines, target_date, 120)
            truncated[code] = hist if hist and len(hist) >= 25 else []
        all_s = []
        for s in self._universe_cache:
            code = s["code"]
            hist = truncated.get(code)
            if hist and len(hist) >= 2:
                pc = hist[-2]["close"]
                cc = hist[-1]["close"]
                hc = (cc - pc) / pc * 100 if pc > 0 else 0
                all_s.append({**s, "changepercent": hc})
            else:
                all_s.append(s)
        ctx = StrategyContext(all_stocks=all_s, market_data=md_map,
                              klines=truncated,
                              config={**self.cfg, "skip_market_check": True},
                              engine_config={**self.cfg, "kline_request_delay": 0})
        picks = DualMAGoldenCross().run(ctx)
        return [{"code": p["code"], "name": p["name"], "close": p["price"],
                 "mv": p["mv"], "ma5": p.get("ma5", 0), "ma18": p.get("ma18", 0),
                 "score": p.get("score", 0)} for p in picks]

    def check_sell(self, klines, position: Position60, current_date: str):
        """Modified sell logic: 95% stop loss, 60% sell on MA5 breakdown."""
        hist = get_kline_series(klines, current_date, 60)
        if hist is None or len(hist) < 25: return False
        closes = [d["close"] for d in hist]
        close = closes[-1]
        ma5 = calc_ma(closes, self.cfg["ma_short"])
        ma18 = calc_ma(closes, self.cfg["ma_stop"])
        if ma5 <= 0 or ma18 <= 0: return False

        buy_price = position.buy_price
        # 跌停不卖
        if is_limit_down_from_klines(klines, current_date, position.code):
            return False
        stop_loss = buy_price * self.cfg.get("stoploss_pct", 0.95)  # 0.95 instead of 0.94

        # P1: 硬止损95%
        if close <= stop_loss:
            self.result.trades.append(position.sell_all(current_date, close, "SL_95pct"))
            return True

        # P2: MA18破位
        if close < ma18:
            if position.units >= 5:
                self.result.trades.append(position.sell_all(current_date, close, "MA18_below"))
            else:
                self.result.trades.append(position.sell_rest(current_date, close, "MA18_below_残仓"))
            return True

        # P3: 止盈
        if position.units >= 3:  # still meaningful position
            tp_price = ma5 * self.cfg["take_profit_mult"]
            if close > tp_price:
                self.result.trades.append(position.sell_half(current_date, close, "TP_MA5x1.3"))
                return False

        # P4: MA5破位 → 卖六成 (modified!)
        if position.units >= 3:
            if close < ma5:
                self.result.trades.append(position.sell_sixty(current_date, close, "SL_below_MA5"))
                return False

        return False

    def run(self):
        self.result = BacktestResult(self.cfg)
        universe, md_map = self.fetch_universe()
        if not universe: return self.result

        self.cfg["kline_request_delay"] = 0
        kd_data = self.fetch_klines(universe)
        if not kd_data: return self.result

        idx_kd = kd_data.get("sh000985")
        if not idx_kd: return self.result
        all_dates = sorted(set(d["date"] for d in idx_kd))
        if len(all_dates) < 50: return self.result

        days = min(self.cfg["backtest_days"], len(all_dates) - 30)
        bt_dates = all_dates[-days:]
        freq = self.cfg["rebalance_freq_days"]
        reb_dates = set(bt_dates[::freq])

        log(f"Backtest: {len(bt_dates)} days, {len(reb_dates)} rebalances ({bt_dates[0]}~{bt_dates[-1]})")

        positions: list[Position60] = []
        skipped = 0
        total_rebs = len(reb_dates)

        for i, cur_date in enumerate(bt_dates):
            # Check sells
            for pos in list(positions):
                if pos.units <= 0:
                    positions.remove(pos)
                    continue
                kd = kd_data.get(pos.code)
                if not kd: continue
                closed = self.check_sell(kd, pos, cur_date)
                if closed:
                    positions.remove(pos)

            # Rebalance
            if cur_date in reb_dates and len(positions) == 0:
                # Market filter
                idx_price = None
                for d in idx_kd:
                    if d["date"] == cur_date:
                        idx_price = d["close"]
                        break
                if idx_price is None:
                    skipped += 1
                    continue
                hist_idx = get_kline_series(idx_kd, cur_date, 60)
                if hist_idx and len(hist_idx) >= 21:
                    idx_ma20 = sum(d["close"] for d in hist_idx[-20:]) / 20
                    if idx_price < idx_ma20:
                        skipped += 1
                        continue

                picks = self.run_strategy(kd_data, md_map, cur_date)
                if not picks:
                    skipped += 1
                    continue

                log(f"  [BUY] {cur_date}: {len(picks)} picks")
                for pick in picks:
                    # T+1 buy
                    buy_date = None
                    for j in range(all_dates.index(cur_date) + 1, min(all_dates.index(cur_date) + 10, len(all_dates))):
                        buy_date = all_dates[j]
                        break
                    if not buy_date: continue
                    kd = kd_data.get(pick["code"])
                    if not kd: continue
                    bh = get_kline_series(kd, buy_date, 5)
                    if not bh: continue
                    price = bh[-1]["close"]
                    if price <= 0: continue
                    if is_limit_up_from_klines(kd, buy_date, pick['code']): continue
                    pos = Position60(pick["code"], price, buy_date, units=5)
                    positions.append(pos)

            # End: force close
            if i == len(bt_dates) - 1:
                for pos in list(positions):
                    kd = kd_data.get(pos.code)
                    if kd:
                        bh = get_kline_series(kd, cur_date, 5)
                        if bh:
                            self.result.trades.append(pos.sell_all(cur_date, bh[-1]["close"], "end_of_backtest"))
                    positions.remove(pos)

        log(f"Market filter: skipped {skipped}/{total_rebs} rebalances")
        return self.result


def run_backtest(cfg: dict) -> BacktestResult:
    return BacktesterSell60(cfg).run()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="dual_ma_gc sell60 + stop95% backtest")
    parser.add_argument("--universe", type=int, default=500)
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--freq", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--tp", type=float, default=1.3)
    parser.add_argument("--stoploss", type=float, default=0.95)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "universe_size": args.universe,
        "top_n": args.top_n,
        "backtest_days": args.days,
        "rebalance_freq_days": args.freq,
        "kline_delay": args.delay,
        "take_profit_mult": args.tp,
        "stoploss_pct": args.stoploss,
    })

    log("Starting dual_ma_gc sell60 + stop95% backtest...")
    t0 = time.time()
    result = run_backtest(cfg)
    elapsed = time.time() - t0
    print(result.summary())
    print(f"\nBacktest completed in {elapsed:.1f}s")

    if args.save:
        out_dir = os.path.join(PROJECT_DIR, "backtest_results")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "dual_ma_gc_sell60_stop95.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        log(f"Results saved to {path}")
