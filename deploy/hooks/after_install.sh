#!/usr/bin/env bash
# AfterInstall: Python env, llama.cpp at the locked commit, model (FR-02 hash check), baseline
# reference, instance serving config and the systemd unit.
source "$(dirname "$0")/lib.sh"
cd "$DRIFTGATE_APP_DIR"

uv sync --frozen --no-dev
log "python environment synced"

ensure_llama_cpp llama-server

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

ensure_reference

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
