#!/bin/bash
# 集合竞价高开策略回测 wrapper (防止 stdout pipe 阻塞)
LOG="/tmp/bt_auction_gap.log"
cd /home/super-user/screening
python3 backtest_auction_gap.py --universe 300 --days 240 --save >> "$LOG" 2>&1
EXIT=$?
echo "[$(date)] Exit code $EXIT" >> "$LOG"
