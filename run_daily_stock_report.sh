#!/usr/bin/env python3
"""每日选股报告入口脚本 - 检测交易日并生成报告"""
import sys, os, json, urllib.request
from datetime import datetime, date

# Check if today is a trading day (weekday, not a holiday)
today = date.today()
weekday = today.weekday()
if weekday >= 5:  # Saturday=5, Sunday=6
    print(f"非交易日（{today} 是周末），跳过选股。")
    sys.exit(0)

# Simple holiday check (major Chinese holidays)
holidays_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03",  # 元旦
    "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",  # 春节
    "2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06",
    "2026-04-06",  # 清明
    "2026-05-01", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-01",  # 端午
    "2026-09-07",  # 中秋
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",  # 国庆
}
today_str = today.strftime("%Y-%m-%d")
if today_str in holidays_2026:
    print(f"非交易日（{today_str} 是法定节假日），跳过选股。")
    sys.exit(0)

# Run the report generator
sys.path.insert(0, "/home/super-user/screening")
exec(open("/home/super-user/screening/daily_stock_report.py").read())
