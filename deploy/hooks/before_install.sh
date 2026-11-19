#!/usr/bin/env bash
# BeforeInstall: make sure the install, config and run directories exist.
source "$(dirname "$0")/lib.sh"
mkdir -p "$DRIFTGATE_APP_DIR" "$DRIFTGATE_RUN_DIR" "$(dirname "$DRIFTGATE_INSTANCE_SERVING_CONFIG")"
log "directories ready under $DRIFTGATE_APP_DIR"
