#!/usr/bin/env sh
set -e

CONFIG_PATH="${CONFIG_PATH:-data/config.yaml}"
RUN_MODE="${RUN_MODE:-scheduled}"
PORT="${PORT:-8222}"
export CONFIG_PATH RUN_MODE PORT

mkdir -p "$(dirname "$CONFIG_PATH")" data/logs

if [ ! -f "$CONFIG_PATH" ]; then
  python3 - <<'PY'
import os

from src.config import default_config_dict, save_config_dict

save_config_dict(default_config_dict(), os.environ["CONFIG_PATH"])
PY
fi

POSTER_PID=""
WEB_PID=""

cleanup() {
  for pid in "$WEB_PID" "$POSTER_PID"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done

  wait "$WEB_PID" 2>/dev/null || true
  wait "$POSTER_PID" 2>/dev/null || true
}

trap cleanup INT TERM EXIT

python3 -m src.main &
POSTER_PID=$!

python3 -m uvicorn src.web:app --host 0.0.0.0 --port "$PORT" &
WEB_PID=$!

while kill -0 "$POSTER_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null; do
  sleep 1
done

STATUS=0
if ! kill -0 "$WEB_PID" 2>/dev/null; then
  wait "$WEB_PID" || STATUS=$?
else
  wait "$POSTER_PID" || STATUS=$?
fi

exit "$STATUS"
