#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

if unit_registered; then
  as_root systemctl stop "$UNIT_NAME"
  echo "stopped systemd unit $UNIT_NAME"
  exit 0
fi

if [ ! -f "$PID_FILE" ]; then
  echo "not running (no pid file)"
  exit 0
fi
pid="$(cat "$PID_FILE")"
if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "not running (stale pid $pid)"
  exit 0
fi
kill "$pid" 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! kill -0 "$pid" 2>/dev/null; then
    break
  fi
  sleep 0.25
done
if kill -0 "$pid" 2>/dev/null; then
  kill -9 "$pid" 2>/dev/null || true
fi
rm -f "$PID_FILE"
echo "stopped pid $pid"
