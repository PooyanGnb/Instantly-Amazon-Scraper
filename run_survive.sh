#!/usr/bin/env bash
# Run a pipeline step in the background so it keeps running after you disconnect.
# Usage: ./run_survive.sh main.py   (or gpt.py / phone_clean.py)
# Then: tail -f main.log   (or gpt.log / phone_clean.log) to see output when you reconnect.

set -e
SCRIPT="${1:-main.py}"
NAME="${SCRIPT%.py}"
LOG="${NAME}.log"

if [[ "$SCRIPT" != "main.py" && "$SCRIPT" != "gpt.py" && "$SCRIPT" != "phone_clean.py" ]]; then
  echo "Usage: $0 main.py | gpt.py | phone_clean.py"
  exit 1
fi

cd "$(dirname "$0")"
echo "Starting $SCRIPT in background (log: $LOG). You can disconnect from SSH."
nohup docker compose run --rm app python "$SCRIPT" >> "$LOG" 2>&1 &
echo ""
echo "To watch the log while connected:  tail -f $LOG"
echo "After reconnect, to see output:     tail -f $LOG   (or  cat $LOG  when finished)"
echo ""
