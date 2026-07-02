#!/bin/bash
# Wrapper script for dual_ma_gc backtest
# Writes all output to a log file to avoid pipe blocking
LOG="/tmp/backtest_dual_ma_gc.log"
RESULT_DIR="/home/super-user/screening/backtest_results"
mkdir -p "$RESULT_DIR"

echo "[$(date)] Starting dual_ma_gc backtest..." >> "$LOG"
cd /home/super-user/screening
python3 -m strategies.backtest_dual_ma_gc \
    --universe 1000 \
    --delay 0.08 \
    --track-k 10 \
    --save >> "$LOG" 2>&1

EXIT=$?
echo "[$(date)] Backtest finished with exit code $EXIT" >> "$LOG"
echo "DONE" >> /tmp/backtest_done.signal
