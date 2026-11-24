CodeDeploy lifecycle hook scripts (FR-18), run by `deploy/appspec.yml` as root on the serving
instances. Settings come from `deploy/config.env`. Values already in the environment win over
`/etc/driftgate/deploy.env` (written by the instance user data), which wins over the defaults.

| Hook | Script | Does |
| --- | --- | --- |
| ApplicationStop | `application_stop.sh` | stops the `driftgate-llama` systemd unit, if it exists |
| BeforeInstall | `before_install.sh` | creates the app, run and config directories |
| AfterInstall | `after_install.sh` | `uv sync`; builds llama.cpp at the `serving/llama_cpp.lock` commit if the binary differs; fetches the served model and checks its SHA-256 against the manifest; fetches the reference measurement from S3; renders `/etc/driftgate/serving.yaml` (instance host and port); installs the unit |
| ApplicationStart | `application_start.sh` | starts the unit and waits for `/health` |
| ValidateService | `validate_service.sh` | `python -m gate run` (D3, D5, D6) against the local llama-server; keeps the verdict and uploads it to S3; exits non-zero on gate failure (1), forced failure (`DRIFTGATE_FORCE_FAIL=1`), a gate input error (2), or an unhealthy server (3) |

The gate mode comes from `gate/config.yaml`. It is `report` (exit 0) until `gate/thresholds.json`
exists, then `enforce`. `DRIFTGATE_GATE_LEVEL` defaults to L1 (16 D3 prompts, 64 D5 items).

Local run against a llama-server on port 8081, with no systemd and no S3:

    export DRIFTGATE_APP_DIR=$PWD DRIFTGATE_SYSTEMD=0 DRIFTGATE_SERVING_PORT=8081 \
      DRIFTGATE_RUN_DIR=$PWD/runs/hooks DRIFTGATE_INSTANCE_ENV=/dev/null
    python -m gate measure --server http://127.0.0.1:8081 --level L1 --seed 0 \
      --out artifacts/reference/measurement.json     # once, on the reference config
    bash deploy/hooks/application_start.sh
    bash deploy/hooks/validate_service.sh; echo $?
