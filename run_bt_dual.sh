#!/bin/bash
LOG="/tmp/bt_dual_v2.log"
echo "[$(date)] Starting dual_ma_gc backtest..." > "$LOG"
cd /home/super-user/screening
python3 -m strategies.backtest_dual_ma_gc --universe 1000 --delay 0.15 --save >> "$LOG" 2>&1
echo "[$(date)] dual_ma_gc finished, exit=$?" >> "$LOG"
echo "DONE_dual" >> /tmp/bt_done.signal
