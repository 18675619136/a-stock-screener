#!/bin/bash
LOG="/tmp/bt_momentum.log"
echo "[$(date)] Starting momentum_ma backtest..." > "$LOG"
cd /home/super-user/screening
python3 -m strategies.backtest --universe 1000 --delay 0.15 --save >> "$LOG" 2>&1
echo "[$(date)] momentum_ma finished, exit=$?" >> "$LOG"
echo "DONE_momentum" >> /tmp/bt_done.signal
