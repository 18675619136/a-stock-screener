"""
Global and per-strategy configuration.
"""
from typing import Any

ENGINE_CONFIG: dict[str, Any] = {
    "sina_page_size": 5000,
    "tencent_batch_size": 80,
    "kline_batch_size": 30,
    "request_delay": 0.3,
    "kline_request_delay": 0.3,
    "timeout_sina": 30,
    "timeout_tencent": 15,
    "timeout_kline": 10,
    "kline_days": 120,
    "data_dir": "/home/super-user/screening",
}

STRATEGY_CONFIGS: dict[str, dict[str, Any]] = {
    "momentum_ma": {
        "max_mv": 1000,
        "min_circ": 0.5,
        "max_circ": 10,
        "min_momentum_pct": 0.0,
        "volume_surge_ratio": 1.5,
        "ma_short": 5,
        "ma_long": 20,
        "max_close_above_long": 1.10,
        "top_n": 30,
        "kline_check_limit": 150,
        "enable_volume_filter": True,
        "enable_ma_filter": True,
        "score_weights": {
            "momentum": 0.25,
            "ma_alignment": 0.25,
            "volume_surge": 0.15,
            "small_cap": 0.20,
            "price_position": 0.15,
        },
    },
    "golden_cross": {
        "max_mv": 1000,
        "min_mv": 0,
        "ma_short": 5,
        "ma_long": 18,
        "top_n": 50,
        "kline_check_limit": 100,
        "hot_track_boost": True,
    },
}
