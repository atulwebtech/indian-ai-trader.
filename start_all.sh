#!/usr/bin/env bash
# One command for everything: dashboard + Telegram bot + option monitor (paper signals only, no orders).
# Ctrl+C stops all three. Output of each goes to logs/<name>.out
cd "$(dirname "$0")"
mkdir -p logs

if pgrep -f "main.py (telegram|dashboard|option-monitor)" >/dev/null; then
  echo "Kuch programs pehle se chal rahe (ya Ctrl+Z se ruke) hain:"
  pgrep -af "main.py (telegram|dashboard|option-monitor)"
  echo "Pehle ./stop_all.sh chalao, phir dobara ./start_all.sh"
  exit 1
fi

pids=()
start() {   # start <name> <args...>
  local name=$1; shift
  setsid .venv/bin/python main.py "$@" >> "logs/$name.out" 2>&1 < /dev/null &   # own session: Ctrl+Z in this terminal cannot pause it
  pids+=($!)
  echo "  started $name (pid $!)  -> logs/$name.out"
}

stop_children() {
  echo; echo "Stopping all..."
  kill "${pids[@]}" 2>/dev/null
  wait 2>/dev/null
  echo "Sab band."
  exit 0
}
trap stop_children INT TERM HUP
trap 'echo " (Ctrl+Z band hai - rokne ke liye Ctrl+C dabao)"' TSTP

echo "Indian AI Trader - sab ek saath (PAPER ONLY, orders disabled)"
start dashboard dashboard
start telegram telegram
start monitor option-monitor --live
echo
echo "Dashboard: http://127.0.0.1:8050"
echo "Option monitor 15:35 ke baad khud band hota hai; dashboard aur Telegram bot chalte rehte hain."
echo "Sab band karne ke liye: Ctrl+C"
echo

# keep the computer awake while we wait
if command -v systemd-inhibit >/dev/null 2>&1; then
  setsid systemd-inhibit --what=idle:sleep --who="Indian AI Trader" --why="Trader running" bash -c 'while sleep 3600; do :; done' &
  pids+=($!)
fi
setsid tail -n 0 -F logs/monitor.out logs/telegram.out 2>/dev/null &
pids+=($!)
while true; do wait; sleep 1; done      # wait returns early after a trapped Ctrl+Z, so keep waiting
