# Shared by the CodeDeploy hooks (FR-18). Sourced, not executed.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Root of the revision being deployed (CodeDeploy runs hooks from its deployment archive).
BUNDLE_DIR="$(cd "$HOOK_DIR/../.." && pwd)"

# Export KEY=VALUE lines from a file, keeping any variable that is already set.
load_env() {
  local line key
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | \#*) continue ;; esac
    key="${line%%=*}"
    if [ -z "${!key+x}" ]; then export "$key=${line#*=}"; fi
  done <"$1"
}

INSTANCE_ENV="${DRIFTGATE_INSTANCE_ENV:-/etc/driftgate/deploy.env}"
if [ -f "$INSTANCE_ENV" ]; then load_env "$INSTANCE_ENV"; fi
load_env "$BUNDLE_DIR/deploy/config.env"

PY="$DRIFTGATE_APP_DIR/.venv/bin/python"
SERVER_URL="http://127.0.0.1:$DRIFTGATE_SERVING_PORT"
UNIT=driftgate-llama

log() {
  printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${LIFECYCLE_EVENT:-local}" "$*" >&2
}

# wait_healthy <seconds>: poll /health until it returns 200.
wait_healthy() {
  local deadline=$(($(date +%s) + $1))
  until curl -fs -o /dev/null "$SERVER_URL/health"; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "llama-server at $SERVER_URL not healthy after $1 s"
      return 1
    fi
    sleep 2
  done
  log "llama-server at $SERVER_URL is healthy"
}

# Instance id via IMDSv2 (NFR-04), or the hostname off EC2.
instance_id() {
  local token
  token="$(curl -fsS -m 2 -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null)" &&
    curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $token" \
      http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null ||
    hostname
}
