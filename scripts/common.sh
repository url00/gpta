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

# Host and port come from data/config.json (server.host / server.port), the
# same place the server reads them. EXEC intentionally word-splits.
server_url() {
  # shellcheck disable=SC2086
  $EXEC --data-dir "$DATA_DIR" --print-url
}

wait_for_health() {
  local i url
  url="$(server_url)"
  for i in $(seq 1 20); do
    if curl -fsS "${url}/api/state" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}
