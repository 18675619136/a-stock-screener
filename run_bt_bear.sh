#!/bin/bash
# Wrapper for bear market PICK3 backtest
LOG="/tmp/bt_bear_pick3.log"
cd /home/super-user/screening
echo "[$(date)] Starting bear market PICK3 backtest..." >> "$LOG"
python3 backtest_bear_pick3.py >> "$LOG" 2>&1
EXIT=$?
echo "[$(date)] Exit code $EXIT" >> "$LOG"
echo "DONE_BEAR" >> /tmp/bt_done.signal
