#!/usr/bin/env bash
# Submit the Lab A/A job once and return its SSM command ID.
set -euo pipefail
if [[ $# != 3 || ! "$1" =~ ^[a-z0-9.-]+$ || ! "$2" =~ ^i-[a-f0-9]+$ ||
      ! "$3" =~ ^[a-zA-Z0-9-]+$ ]]; then
  echo "usage: $0 BUCKET INSTANCE_ID RUN_ID" >&2
  exit 2
fi
cd "$(dirname "$0")/.."
bucket="$1"
instance="$2"
run_id="$3"
region="${AWS_REGION:-us-east-1}"
prefix="runs/$run_id"
commit="$(git rev-parse HEAD)"
scratch="$(mktemp -d)"
trap 'rm -f "$scratch/source.tar.gz" "$scratch/parameters.json"; rmdir "$scratch"' EXIT
git archive --format=tar.gz --output="$scratch/source.tar.gz" "$commit" \
  analysis faults gate serving docs/prereg/2026-11-26-d7.md pyproject.toml uv.lock README.md
aws s3 cp --region "$region" --only-show-errors --sse AES256 "$scratch/source.tar.gz" \
  "s3://$bucket/$prefix/source.tar.gz"

uv run python - "$bucket" "$prefix" "$run_id" "$region" "$commit" > "$scratch/parameters.json" <<'PY'
import json
import shlex
import sys

bucket, prefix, run_id, region, commit = map(shlex.quote, sys.argv[1:])
script = f"""
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
export LLAMA_CPP_BIN=/opt/llama.cpp/build/bin
export AWS_DEFAULT_REGION={region}
mkdir -p /var/lib/driftgate
cd /var/lib/driftgate
mkdir {run_id}
cd {run_id}
aws s3 cp --only-show-errors s3://{bucket}/{prefix}/source.tar.gz source.tar.gz
tar xzf source.tar.gz
mkdir -p models results
ln -s /opt/driftgate/models/qwen2.5-1.5b-instruct-q8_0.gguf models/
ln -s /opt/driftgate/.venv .venv
unset PYTHONPATH
key="$(uv run --no-sync python -m gate reference --instance-type t3.large --print-key | \\
  uv run --no-sync python -c 'import json, sys; print(json.load(sys.stdin)["key"])')"
aws s3 cp --only-show-errors "s3://{bucket}/${{key}}measurement.json" baseline.json
trap 'aws s3 cp --only-show-errors --recursive --sse AES256 results/ s3://{bucket}/{prefix}/results/' EXIT
uv run --no-sync python -m analysis.d7_runs lab-aa --out results \\
  --reference baseline.json --s3-uri s3://{bucket} --revision {commit}
"""
json.dump({"commands": ["#!/usr/bin/env bash", script], "executionTimeout": ["10800"]}, sys.stdout)
PY

aws ssm send-command --region "$region" --instance-ids "$instance" \
  --document-name AWS-RunShellScript --parameters "file://$scratch/parameters.json" \
  --comment "$run_id" --output-s3-bucket-name "$bucket" --output-s3-key-prefix "$prefix/ssm" \
  --query 'Command.CommandId' --output text
