#!/usr/bin/env bash
# Stops every running or Ctrl+Z-paused copy of the dashboard, Telegram bot and option monitor.
P=$(pgrep -f "main.py (telegram|dashboard|option-monitor)|run.sh (telegram|dashboard|option-monitor)|Indian AI Trader")
if [ -z "$P" ]; then echo "Kuch nahi chal raha."; exit 0; fi
echo "Band kar rahe: $(echo $P | wc -w) process"
kill -TERM $P 2>/dev/null
kill -CONT $P 2>/dev/null          # Ctrl+Z wale ruke process ko jagana padta hai tabhi wo band hote hain
sleep 3
R=$(pgrep -f "main.py (telegram|dashboard|option-monitor)")
[ -n "$R" ] && kill -KILL $R 2>/dev/null
pgrep -af "main.py (telegram|dashboard|option-monitor)" || echo "Sab band. Ab ./start_all.sh chala sakte ho."
