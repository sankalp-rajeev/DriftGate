#!/usr/bin/env bash
# ApplicationStart: start llama-server and wait until /health answers.
source "$(dirname "$0")/lib.sh"
if [ "$DRIFTGATE_SYSTEMD" = 1 ]; then
  systemctl start "$UNIT"
  log "started $UNIT"
fi
wait_healthy "$DRIFTGATE_HEALTH_TIMEOUT_S"
