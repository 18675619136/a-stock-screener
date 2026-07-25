#!/bin/bash
# Wrapper for PICK3 backtest — runs in background, logs to file
LOG="/tmp/bt_pick3_score.log"
cd /home/super-user/screening
echo "[$(date)] Starting PICK3 score-mode backtest..." >> "$LOG"
python3 -m strategies.backtest_pick3 --universe 500 --top-n 15 --pick 3 --days 240 --save --pick-mode score >> "$LOG" 2>&1
EXIT=$?
echo "[$(date)] Exit code $EXIT" >> "$LOG"
echo "DONE_SCORE" >> /tmp/bt_done.signal
