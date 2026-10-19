#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${GPTA_DATA_DIR:-$ROOT/data}"
PYTHON="${GPTA_PYTHON:-python3}"
if ! PYTHON_ABS="$(command -v "$PYTHON")"; then
  echo "python interpreter not found: $PYTHON" >&2
  exit 1
fi
EXEC="${GPTA_EXEC:-$PYTHON_ABS -m gpta}"
UNIT_NAME="${GPTA_UNIT_NAME:-gpta}"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"
HOST="${GPTA_HOST:-127.0.0.1}"
PORT="${GPTA_PORT:-8787}"
PID_FILE="$DATA_DIR/server.pid"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

unit_registered() {
  systemctl cat --no-pager "$UNIT_NAME" >/dev/null 2>&1
}

wait_for_health() {
  local i
  for i in $(seq 1 20); do
    if [ -n "${GPTA_AUTH_TOKEN:-}" ]; then
      if curl -fsS -H "Authorization: Bearer ${GPTA_AUTH_TOKEN}" \
        "http://${HOST}:${PORT}/api/state" >/dev/null 2>&1; then
        return 0
      fi
    elif curl -fsS "http://${HOST}:${PORT}/api/state" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}
