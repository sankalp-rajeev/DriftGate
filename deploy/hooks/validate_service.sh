#!/usr/bin/env bash
# ValidateService (FR-18): run D3, D5 and D6 through the gate CLI against this instance's
# llama-server, keep the verdict, and exit non-zero on gate failure. The gate mode comes from
# the gate config (report until gate/thresholds.json exists, then enforce; PRD 6.8).
# DRIFTGATE_FORCE_FAIL=1 makes this hook fail in either mode (T-13 rollback drills).
# With DRIFTGATE_PUBLISH_METRICS=1 the verdict goes to CloudWatch (FR-20): GateScore only in
# enforce mode, the raw per-detector values always (publish_metrics.py). The rollback alarm on
# GateScore > 1 is attached to the deployment group (deploy/cloudformation/codedeploy.yaml).
# Exit codes: 0 pass, 1 gate failure or forced failure, 2 gate input/usage error, 3 not healthy.
source "$(dirname "$0")/lib.sh"
cd "$DRIFTGATE_APP_DIR"

wait_healthy "$DRIFTGATE_HEALTH_TIMEOUT_S" || exit 3

out="$DRIFTGATE_RUN_DIR/${DEPLOYMENT_ID:-local}"
mkdir -p "$out"
set +e
DRIFTGATE_FORCE_FAIL="$DRIFTGATE_FORCE_FAIL" "$PY" -m gate run \
  --gate-config "$DRIFTGATE_GATE_CONFIG" \
  --config "$DRIFTGATE_SERVING_CONFIG" \
  --server "$SERVER_URL" \
  --budget "$DRIFTGATE_GATE_BUDGET" \
  --seed "$DRIFTGATE_GATE_SEED" \
  --measurement-out "$out/measurement.json" \
  --out "$out/verdict.json"
rc=$?
set -e
log "gate exit $rc; verdict at $out/verdict.json"

if [ -n "$DRIFTGATE_BUCKET" ] && [ -f "$out/verdict.json" ]; then
  dest="s3://$DRIFTGATE_BUCKET/deployments/${DEPLOYMENT_ID:-local}/$(instance_id)/"
  aws s3 cp --only-show-errors "$out/verdict.json" "$dest" || log "verdict upload to $dest failed"
fi

if [ "$DRIFTGATE_PUBLISH_METRICS" = 1 ] && [ -f "$out/verdict.json" ]; then
  "$PY" "$HOOK_DIR/publish_metrics.py" --verdict "$out/verdict.json" \
    --deployment-group "${DEPLOYMENT_GROUP_NAME:-$DRIFTGATE_DEPLOYMENT_GROUP}" \
    --region "$DRIFTGATE_REGION" || log "CloudWatch metric publish failed"
fi

if [ "$DRIFTGATE_FORCE_FAIL" = 1 ] && [ "$rc" -eq 0 ]; then
  log "DRIFTGATE_FORCE_FAIL=1: failing ValidateService"
  rc=1
fi
exit "$rc"
