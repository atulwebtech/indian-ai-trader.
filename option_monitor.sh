#!/usr/bin/env bash
# Starts the option signal monitor (paper signals + Telegram alerts) and stops the computer from sleeping while it runs.
# Start it before 09:15 IST on trading days. It stops itself after 15:35 when no signal is open.
cd "$(dirname "$0")"
if command -v systemd-inhibit >/dev/null 2>&1; then
  exec systemd-inhibit --what=idle:sleep --who="Indian AI Trader" --why="Option monitor running" ./run.sh option-monitor --live "$@"
else
  echo "systemd-inhibit not found: the computer may sleep. Disable sleep manually."
  exec ./run.sh option-monitor --live "$@"
fi
