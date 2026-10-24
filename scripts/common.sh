#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${GPTA_DATA_DIR:-$ROOT/data}"

# Pillow is GPTA's one dependency. Unless GPTA_EXEC or GPTA_PYTHON is set, run
# this checkout with the flake's Python (python3 + Pillow). It is linked at
# .nix-python, which keeps it from being garbage collected and gives the
# systemd unit a stable path. Without Nix, any python3 with Pillow works.
default_python() {
  local link="$ROOT/.nix-python"
  if command -v nix >/dev/null 2>&1 &&
    nix --extra-experimental-features "nix-command flakes" \
      build "$ROOT#python" --out-link "$link" >/dev/null 2>&1; then
    echo "$link/bin/python3"
  elif [ -x "$link/bin/python3" ]; then
    echo "$link/bin/python3"
  elif python3 -c "import PIL" >/dev/null 2>&1; then
    echo python3
  else
    echo "no Python with Pillow: install Nix, or set GPTA_PYTHON" >&2
    return 1
  fi
}

if [ -z "${GPTA_EXEC:-}" ] && [ -z "${GPTA_PYTHON:-}" ]; then
  GPTA_PYTHON="$(default_python)" || exit 1
  # Exported so install-systemd.sh's sudo re-run uses the same interpreter.
  export GPTA_PYTHON
fi
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
