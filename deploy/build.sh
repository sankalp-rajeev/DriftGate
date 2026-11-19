#!/usr/bin/env bash
# Build stage checks (FR-16 local part, T-11): unit tests, FR-02 hash check, D1, D2.
#
#   deploy/build.sh --commit <commit> --run-id <id> [--config serving/config.yaml] [--out DIR]
#
# Runs from the repository root (PRD 6.8: the build host checks out <commit> in /opt/driftgate
# first). Steps run in order and the first failure stops the build with a non-zero exit, so a
# failed build never reaches approval or deploy. Every step writes its JSON or log under
# --out (default runs/build-<id>/), and build.json records each step's status and timing.
# After the checks, the `package` step writes the CodeDeploy bundle to --out as
# bundles/<commit>.zip (repository tree at <commit> plus deploy/appspec.yml at the root), the
# key the orchestrator expects in S3.
#
# Upload is OFF by default and never runs locally or in tests: only with --upload (or
# DRIFTGATE_UPLOAD=1) AND DRIFTGATE_BUNDLE_BUCKET set does the `upload` step run
# `aws s3 cp` to s3://$DRIFTGATE_BUNDLE_BUCKET/bundles/<commit>.zip (Lab build host, T-28).
#
# DRIFTGATE_BUILD_STEPS (default "tests manifest d1 d2 package") selects steps, for local use
# and tests.
# DRIFTGATE_MANIFEST (default serving/manifest.json) is the manifest the FR-02 step checks.
set -uo pipefail

usage() {
  echo "usage: deploy/build.sh --commit <commit> --run-id <id> [--config PATH] [--out DIR] [--upload]" >&2
  exit 2
}

COMMIT="" RUN_ID="" CONFIG="serving/config.yaml" OUT="" UPLOAD="${DRIFTGATE_UPLOAD:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) COMMIT="${2:-}"; shift 2 ;;
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --config) CONFIG="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --upload) UPLOAD=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
[[ -n "$COMMIT" && -n "$RUN_ID" ]] || usage
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "invalid --run-id" >&2; exit 2; }

cd "$(dirname "$0")/.." || exit 2
unset PYTHONPATH
OUT="${OUT:-runs/build-$RUN_ID}"
STEPS="${DRIFTGATE_BUILD_STEPS-tests manifest d1 d2 package}"
MANIFEST="${DRIFTGATE_MANIFEST:-serving/manifest.json}"
mkdir -p "$OUT"

STEP_JSON=()
STATUS="passed"
FAILED_STEP=""

finish() {
  local steps
  steps=$(IFS=,; echo "${STEP_JSON[*]}")
  printf '{"schema": "driftgate.build/1", "commit": "%s", "head": "%s", "run_id": "%s", "config": "%s", "status": "%s", "failed_step": %s, "steps": [%s]}\n' \
    "$COMMIT" "$HEAD" "$RUN_ID" "$CONFIG" "$STATUS" \
    "$( [[ -n "$FAILED_STEP" ]] && echo "\"$FAILED_STEP\"" || echo null )" "$steps" \
    > "$OUT/build.json"
  cat "$OUT/build.json"
  [[ "$STATUS" == "passed" ]] && exit 0 || exit 1
}

record() {  # name exit_code seconds output
  local state="passed"
  [[ "$2" -eq 0 ]] || state="failed"
  STEP_JSON+=("{\"name\": \"$1\", \"status\": \"$state\", \"exit_code\": $2, \"seconds\": $3, \"output\": \"$4\"}")
  if [[ "$2" -ne 0 ]]; then
    STATUS="failed"
    FAILED_STEP="$1"
    finish
  fi
}

run_step() {  # name output-file command...
  local name="$1" output="$2"
  shift 2
  echo "== $name" >&2
  local start=$SECONDS
  "$@" > "$output" 2> "$OUT/$name.stderr.log"
  record "$name" "$?" "$((SECONDS - start))" "$output"
}

# The checkout must be the commit the orchestrator asked for.
HEAD="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
if [[ "$HEAD" != "$COMMIT"* ]]; then
  echo "checked-out commit $HEAD is not $COMMIT" >&2
  record commit 1 0 ""
fi
record commit 0 0 ""

for step in $STEPS; do
  case "$step" in
    tests) run_step tests "$OUT/tests.log" uv run pytest -q ;;
    manifest) run_step manifest "$OUT/manifest.json" \
      uv run python -m gate.manifest verify --config "$CONFIG" --manifest "$MANIFEST" ;;
    d1) run_step d1 "$OUT/d1.json" uv run python -m gate.d1 --config "$CONFIG" --report-dir "$OUT" ;;
    d2) run_step d2 "$OUT/d2.json" uv run python -m gate.d2 run --config "$CONFIG" ;;
    package) run_step package "$OUT/package.json" \
      uv run python deploy/package_bundle.py --commit "$COMMIT" --out "$OUT" ;;
    *) echo "unknown step $step" >&2; record "$step" 2 0 "" ;;
  esac
done

if [[ "$UPLOAD" == "1" ]]; then
  if [[ -z "${DRIFTGATE_BUNDLE_BUCKET:-}" ]]; then
    echo "--upload needs DRIFTGATE_BUNDLE_BUCKET" >&2
    record upload 2 0 ""
  fi
  run_step upload "$OUT/upload.log" \
    aws s3 cp "$OUT/bundles/$COMMIT.zip" "s3://$DRIFTGATE_BUNDLE_BUCKET/bundles/$COMMIT.zip"
fi
finish
