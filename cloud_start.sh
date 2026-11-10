#!/usr/bin/env bash
# Start command for a cloud server (Render etc.): dashboard + Telegram bot + option monitor in ONE service,
# so the monitor and the dashboard share the same data/ folder. PAPER ONLY, orders stay disabled.
#   Render "Start Command":  bash cloud_start.sh
# Needs these environment variables on the server (same names as .env): DATA_MODE=LIVE, GROWW_API_KEY,
# GROWW_TOTP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DASHBOARD_PASSWORD. Optional: RUN_TELEGRAM=false.
# Do NOT run ./start_all.sh on the laptop at the same time: two Telegram bots conflict and alerts arrive twice.
cd "$(dirname "$0")"
mkdir -p data logs reports
PY=python; [ -x .venv/bin/python ] && PY=.venv/bin/python

# Option monitor: start on weekdays between 09:05 and 15:35 IST; restart if it crashes; it exits itself after 15:35.
monitor_loop() {
  while true; do
    hm=$(TZ=Asia/Kolkata date +%H%M); dow=$(TZ=Asia/Kolkata date +%u)
    if [ "$dow" -le 5 ] && [ "$hm" -ge 0905 ] && [ "$hm" -lt 1535 ]; then
      echo "[$(TZ=Asia/Kolkata date '+%F %T')] starting option monitor"
      $PY main.py option-monitor --live
      echo "[$(TZ=Asia/Kolkata date '+%F %T')] option monitor exited"
    fi
    sleep 60
  done
}
monitor_loop &

# Telegram bot: restart if it stops.
if [ "${RUN_TELEGRAM:-true}" != "false" ]; then
  ( while true; do $PY main.py telegram; echo "telegram bot exited, restarting in 30s"; sleep 30; done ) &
fi

# Dashboard in the foreground on the port the platform gives us.
export DASHBOARD_HOST=0.0.0.0
exec $PY main.py dashboard
