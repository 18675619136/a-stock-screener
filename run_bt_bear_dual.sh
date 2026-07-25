#!/bin/bash
LOG="/tmp/bt_bear_dual_ma.log"
cd /home/super-user/screening
echo "[$(date)] Starting dual-ma golden cross bear market backtest..." >> "$LOG"
python3 backtest_bear_dual_ma.py >> "$LOG" 2>&1
EXIT=$?
echo "[$(date)] Exit code $EXIT" >> "$LOG"
echo "DONE" >> /tmp/bt_done.signal
