#!/usr/bin/env bash
# ApplicationStop: stop llama-server from the previous revision (no-op on the first deploy).
source "$(dirname "$0")/lib.sh"
if [ "$DRIFTGATE_SYSTEMD" = 1 ] && systemctl list-unit-files "$UNIT.service" >/dev/null 2>&1; then
  systemctl stop "$UNIT" || true
  log "stopped $UNIT"
else
  log "nothing to stop"
fi
