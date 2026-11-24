#!/usr/bin/env bash
# Prepare the checkout created by build-host user data.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/lib.sh

rm -f .bootstrap-done
load_env deploy/config.env
unset PYTHONPATH

uv sync --frozen
PY=.venv/bin/python
ensure_llama_cpp llama-server llama-perplexity
"$PY" -m gate.models --role reference --role ci --role F1 --role F2

touch .bootstrap-done
echo "DriftGate build host ready"
