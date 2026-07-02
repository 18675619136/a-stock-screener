"""
Global and per-strategy configuration.
"""

from typing import Any

# ── Global engine defaults ────────────────────────────────────────
ENGINE_CONFIG: dict[str, Any] = {
    "sina_page_size": 5000,
    "tencent_batch_size": 80,
    "kline_batch_size": 30,
    "request_delay": 0.3,
    "kline_request_delay": 0.3,
    "timeout_sina": 30,
    "timeout_tencent": 15,
    "timeout_kline": 10,
    "kline_days": 120,  # how many recent klines to keep
    "data_dir": "/home/super-user/screening",
}

# ── Per-strategy defaults (overridden by user or strategy config) ──
STRATEGY_CONFIGS: dict[str, dict[str, Any]] = {
    "momentum_ma": {
        "max_mv": 1000,                  # max market cap (1000亿)
        "min_total_shares": 0.5,         # min total shares (亿)
        "max_total_shares": 10,          # max total shares (亿)
        "volume_surge_ratio": 1.2,       # volume vs 18-day avg
        "ma_short": 5,                   # short MA period
        "ma_long": 18,                   # long MA period
        "top_n": 30,                     # max stocks to return
        "kline_check_limit": 150,        # max klines to fetch
        "enable_volume_filter": True,
        "enable_ma_filter": True,
    },
    "bull_ma_limitup": {
        "max_mv": 1000,
        "min_total_shares": 0.5,
        "max_total_shares": 10,
        "ma_short": 5,
        "ma_long": 18,
        "surge_threshold": 8.0,
        "top_n": 30,
        "kline_check_limit": 150,
        "lookback_days": 20,
    },
    "dual_ma_gc": {
        "max_mv": 1000,
        "min_total_shares": 0.5,
        "max_total_shares": 10,
        "ma_short": 5,
        "ma_long": 18,
        "top_n": 30,
        "kline_check_limit": 150,
    },
}
