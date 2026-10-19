#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

if unit_registered; then
  as_root systemctl start "$UNIT_NAME"
  if wait_for_health; then
    echo "started systemd unit $UNIT_NAME at http://${HOST}:${PORT}"
    exit 0
  fi
  echo "unit $UNIT_NAME started but health check failed" >&2
  as_root systemctl status --no-pager "$UNIT_NAME" >&2 || true
  exit 1
fi

mkdir -p "$DATA_DIR"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "already running with pid $(cat "$PID_FILE")"
  exit 0
fi

# EXEC intentionally word-splits to support "python -m gpta". The server writes
# its own rotated log to data/server.log; server.out captures pre-logging
# failures such as interpreter errors.
nohup $EXEC --data-dir "$DATA_DIR" --host "$HOST" --port "$PORT" >>"$DATA_DIR/server.out" 2>&1 &
pid=$!
echo "$pid" >"$PID_FILE"
if wait_for_health; then
  echo "started pid $pid at http://${HOST}:${PORT} (log: $DATA_DIR/server.log)"
else
  echo "server failed to become healthy; recent log:" >&2
  tail -n 40 "$DATA_DIR/server.out" >&2 || true
  exit 1
fi
