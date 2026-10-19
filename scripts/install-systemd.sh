#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "usage: $0 [--dry-run]"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

RUN_USER="${GPTA_RUN_USER:-${SUDO_USER:-$(id -un)}}"

render_unit() {
  local supplementary="# SupplementaryGroups=lp (group absent)"
  if getent group lp >/dev/null 2>&1; then
    supplementary="SupplementaryGroups=lp"
  fi
  sed     -e "s|@RUN_USER@|${RUN_USER}|g"     -e "s|@ROOT@|${ROOT}|g"     -e "s|@DATA_DIR@|${DATA_DIR}|g"     -e "s|@EXEC@|${EXEC}|g"     -e "s|@SUPPLEMENTARY_GROUPS@|${supplementary}|g"     "$ROOT/deploy/gpta.service.in"
}

if [ "$DRY_RUN" -eq 1 ]; then
  render_unit
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  exec sudo --preserve-env=GPTA_DATA_DIR,GPTA_EXEC,GPTA_RUN_USER,GPTA_UNIT_NAME,GPTA_PYTHON,GPTA_PORT,GPTA_HOST     env "GPTA_RUN_USER=${RUN_USER}" "$ROOT/scripts/install-systemd.sh" "$@"
fi

if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  echo "run user does not exist: $RUN_USER" >&2
  exit 1
fi

if [ -f "$UNIT_PATH" ]; then
  echo "already registered at $UNIT_PATH"
  systemctl daemon-reload
  systemctl enable --now "$UNIT_NAME"
  exit 0
fi

# Remove a manually started instance so the unit can bind the same port.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
render_unit >"$tmp"
install -m 0644 "$tmp" "$UNIT_PATH"
install -d -m 0755 -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" "$DATA_DIR"
systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"
echo "registered $UNIT_PATH and started $UNIT_NAME"
systemctl status --no-pager "$UNIT_NAME" || true
