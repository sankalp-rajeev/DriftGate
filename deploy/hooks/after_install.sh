#!/usr/bin/env bash
# AfterInstall: Python env, llama.cpp at the locked commit, model (FR-02 hash check), baseline
# reference, instance serving config and the systemd unit.
source "$(dirname "$0")/lib.sh"
cd "$DRIFTGATE_APP_DIR"

uv sync --frozen --no-dev
log "python environment synced"

# llama.cpp at the serving/llama_cpp.lock commit; rebuilt only when the lock changes (F8).
lock_commit="$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["commit"])' serving/llama_cpp.lock)"
built="$("$LLAMA_CPP_BIN/llama-server" --version 2>&1 | sed -n 's/.*commit \([0-9a-f]*\).*/\1/p' | head -n1 || true)"
if [ -z "$built" ] || [ "${lock_commit#"$built"}" = "$lock_commit" ]; then
  log "building llama.cpp $lock_commit (installed: ${built:-none})"
  repo="$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["repo"])' serving/llama_cpp.lock)"
  [ -d "$DRIFTGATE_LLAMA_CPP_DIR/.git" ] || git clone --quiet "$repo" "$DRIFTGATE_LLAMA_CPP_DIR"
  git -C "$DRIFTGATE_LLAMA_CPP_DIR" fetch --quiet origin
  git -C "$DRIFTGATE_LLAMA_CPP_DIR" checkout --quiet --detach "$lock_commit"
  cmake -S "$DRIFTGATE_LLAMA_CPP_DIR" -B "$DRIFTGATE_LLAMA_CPP_DIR/build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF
  cmake --build "$DRIFTGATE_LLAMA_CPP_DIR/build" -j "$(nproc)" --target llama-server
fi

# The model named by the serving config, downloaded if missing and checked against the manifest.
role="$("$PY" - "$DRIFTGATE_SERVING_CONFIG" <<'PYEOF'
import sys
import yaml
cfg = yaml.safe_load(open(sys.argv[1]))
models = yaml.safe_load(open(cfg["models"]))["models"]
print(next(m["role"] for m in models if m["file"] == cfg["model"]))
PYEOF
)"
"$PY" -m gate.models --role "$role"
log "model for role $role present and matches the manifest"

# Reference measurement and D2 reference logits for the gate, from the storage bucket. The key is
# always the baseline's (gate/baseline.yaml) on this instance type (IMDSv2), never the candidate's.
if [ -n "$DRIFTGATE_BUCKET" ]; then
  eval "$("$PY" -m gate reference --print-key | "$PY" -c \
    'import json, shlex, sys; d = json.load(sys.stdin); print("ref_key=%s ref_dir=%s" % (shlex.quote(d["key"]), shlex.quote(d["local_dir"])))')"
  mkdir -p "$ref_dir"
  aws s3 cp --only-show-errors --recursive "s3://$DRIFTGATE_BUCKET/$ref_key" "$ref_dir"
  log "baseline reference fetched from $ref_key to $ref_dir"
fi

# Instance serving config: the revision's config with the instance listen address.
"$PY" - "$DRIFTGATE_SERVING_CONFIG" "$DRIFTGATE_INSTANCE_SERVING_CONFIG" <<PYEOF
import sys
import yaml
cfg = yaml.safe_load(open(sys.argv[1]))
cfg["server"]["host"] = "$DRIFTGATE_SERVING_HOST"
cfg["server"]["port"] = int("$DRIFTGATE_SERVING_PORT")
with open(sys.argv[2], "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PYEOF
log "serving config written to $DRIFTGATE_INSTANCE_SERVING_CONFIG"

if [ "$DRIFTGATE_SYSTEMD" = 1 ]; then
  cat >"/etc/systemd/system/$UNIT.service" <<UNITEOF
[Unit]
Description=DriftGate llama-server (serving config $DRIFTGATE_INSTANCE_SERVING_CONFIG)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DRIFTGATE_APP_DIR
Environment=LLAMA_CPP_BIN=$LLAMA_CPP_BIN
ExecStart=$PY -m gate.serve --config $DRIFTGATE_INSTANCE_SERVING_CONFIG
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
  systemctl daemon-reload
  systemctl enable "$UNIT" >/dev/null
  log "systemd unit $UNIT installed"
fi
