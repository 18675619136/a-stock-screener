"""
ETF 候选池 — 供回测和监控脚本统一引用

用法:
    from etf_candidate_pool import ETF_LIST, ETF_DICT, get_etfs_by_category

直接从 JSON 加载，避免各脚本重复硬编码。
"""

import json
import os

_POOL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etf_candidate_pool.json")

with open(_POOL_FILE, "r", encoding="utf-8") as f:
    _data = json.load(f)

# ── 全部 ETF 列表 ──
ETF_LIST = _data["etfs"]

# ── code -> ETF 信息映射 ──
ETF_DICT = {e["code"]: e for e in ETF_LIST}


def get_etfs_by_category(category: str) -> list[dict]:
    """按分类筛选，如 category='创业板' 或 '纳斯达克'"""
    return [e for e in ETF_LIST if e["category"] == category]


def get_etf_codes(category: str | None = None) -> list[str]:
    """获取 ETF 代码列表，可选按分类过滤"""
    if category:
        return [e["code"] for e in ETF_LIST if e["category"] == category]
    return [e["code"] for e in ETF_LIST]


if __name__ == "__main__":
    print(f"ETF 候选池共 {len(ETF_LIST)} 只基金：")
    for e in ETF_LIST:
        print(f"  {e['prefix']}{e['code']:>6s}  {e['name']} ({e['category']})")
    print(f"\n  创业板ETF: {len(get_etfs_by_category('创业板'))} 只")
    print(f"  纳指ETF:   {len(get_etfs_by_category('纳斯达克'))} 只")
