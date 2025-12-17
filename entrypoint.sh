#!/usr/bin/env sh
set -e

CONFIG_PATH="${CONFIG_PATH:-config/config.yaml}"
RUN_MODE="${RUN_MODE:-scheduled}"
PORT="${PORT:-8006}"

# Ensure directories exist
mkdir -p "$(dirname "$CONFIG_PATH")" data logs
# If config missing, seed from example (without secrets)
if [ ! -f "$CONFIG_PATH" ] && [ -f "config/config.example.yaml" ]; then
  cp config/config.example.yaml "$CONFIG_PATH"
fi

# Start the poster in background (scheduled or once, depending on RUN_MODE)
python -m src.main &
POSTER_PID=$!

# Start web UI (exposes / and /api/config)
uvicorn src.web:app --host 0.0.0.0 --port "$PORT"

# Cleanup background process on exit
kill "$POSTER_PID" 2>/dev/null || true
wait "$POSTER_PID" 2>/dev/null || true
