#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

if unit_registered; then
  as_root systemctl restart "$UNIT_NAME"
else
  "$SCRIPT_DIR/stop.sh"
  "$SCRIPT_DIR/start.sh"
fi

if wait_for_health; then
  echo "restarted at $(server_url)"
else
  echo "restart completed but health check failed" >&2
  exit 1
fi
