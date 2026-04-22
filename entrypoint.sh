#!/usr/bin/env sh
set -e

CONFIG_PATH="${CONFIG_PATH:-data/config.yaml}"
RUN_MODE="${RUN_MODE:-scheduled}"
PORT="${PORT:-8222}"
EXAMPLE_CONFIG="config/config.example.yaml"
DATA_EXAMPLE_CONFIG="data/config.example.yaml"

# Ensure directories exist
mkdir -p "$(dirname "$CONFIG_PATH")" data/logs
# Keep a copy of the example config in the runtime data directory
if [ -f "$EXAMPLE_CONFIG" ] && [ ! -f "$DATA_EXAMPLE_CONFIG" ]; then
  cp "$EXAMPLE_CONFIG" "$DATA_EXAMPLE_CONFIG"
fi
# If config missing, seed from example (without secrets)
if [ ! -f "$CONFIG_PATH" ] && [ -f "$DATA_EXAMPLE_CONFIG" ]; then
  cp "$DATA_EXAMPLE_CONFIG" "$CONFIG_PATH"
fi

# Start the poster in background (scheduled or once, depending on RUN_MODE)
python -m src.main &
POSTER_PID=$!

# Start web UI (exposes / and /api/config)
uvicorn src.web:app --host 0.0.0.0 --port "$PORT"

# Cleanup background process on exit
kill "$POSTER_PID" 2>/dev/null || true
wait "$POSTER_PID" 2>/dev/null || true
