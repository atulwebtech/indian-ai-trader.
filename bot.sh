#!/usr/bin/env bash
# Starts the Telegram bot and stops the computer from sleeping while it runs.
cd "$(dirname "$0")"
if command -v systemd-inhibit >/dev/null 2>&1; then
  exec systemd-inhibit --what=idle:sleep --who="Indian AI Trader" --why="Telegram bot running" ./run.sh telegram "$@"
else
  echo "systemd-inhibit not found: the computer may sleep. Disable sleep manually."
  exec ./run.sh telegram "$@"
fi
