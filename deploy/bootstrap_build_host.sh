#!/usr/bin/env bash
# Prepare the checkout created by build-host user data.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/lib.sh

rm -f .bootstrap-done
build_env="${DRIFTGATE_BUILD_ENV:-/etc/driftgate/build.env}"
if [[ -f "$build_env" ]]; then load_env "$build_env"; fi
load_env deploy/config.env
unset PYTHONPATH

uv sync --frozen
PY=.venv/bin/python
ensure_llama_cpp llama-server llama-perplexity
"$PY" -m gate.models --role reference --role ci --role F1 --role F2

touch .bootstrap-done
echo "DriftGate build host READY"
