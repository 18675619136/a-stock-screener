#!/bin/bash
cd /home/super-user/screening
echo "[$(date)] Starting backtest: universe=1000, delay=0.1" > /tmp/backtest_progress.txt
python3 -m strategies.backtest --universe 1000 --delay 0.1 --save >> /tmp/backtest_progress.txt 2>&1
echo "[$(date)] Backtest finished, exit_code=$?" >> /tmp/backtest_progress.txt
