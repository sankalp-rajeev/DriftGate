# Observability (laptop research layer)

Prometheus, Grafana, an OpenTelemetry collector and Jaeger run locally in Docker (FR-22 to FR-24, ADR-009, PRD §6.8). CloudWatch configs for the Lab (FR-29) belong to T-13.

```
make obs-up      # docker compose -f observability/docker-compose.yml up -d
make obs-down
```

| Service | URL | What |
| --- | --- | --- |
| Prometheus | http://localhost:9090/targets | scrapes `llama-server` at `host.docker.internal:8082/metrics` and gate metrics from the collector (`otel-collector:8889`) |
| Grafana | http://localhost:3000 (anonymous viewer; `admin`/`admin`) | dashboard "DriftGate serving and gate" (`grafana/dashboards/driftgate.json`), provisioned automatically |
| OTel collector | OTLP gRPC `:4317`, HTTP `:4318` | receives gate spans and metrics; traces go to Jaeger and to `runs/otel/traces.jsonl` |
| Jaeger | http://localhost:16686 | trace viewer: service `driftgate-gate`, one `gate.run` span with a `gate.D1`…`gate.D6` child per detector |

llama-server runs on the host from the pinned local build, on port **8082** locally so it doesn't clash with other runs on 8080, with `--metrics`. For example:

```
$LLAMA_CPP_BIN/llama-server -m models/qwen2.5-0.5b-instruct-q8_0.gguf --port 8082 --metrics
```

## Gate traces and metrics (FR-23, FR-24)

Telemetry is **off** unless `DRIFTGATE_TELEMETRY=1`, so tests, CI and the Lab build host never try to reach a collector. `gate/telemetry.py` creates the spans; `otel/gate-telemetry.yaml` configures the exporter, and `OTEL_EXPORTER_OTLP_ENDPOINT` overrides its endpoint.

One run is one trace. D1 and D2 run in `deploy/build.sh`, wrapped in `python -m gate.telemetry span D1|D2 -- ...`; D3, D5 and D6 run in `python -m gate run`. The processes share the trace through `TRACEPARENT`. D4 is not implemented (first in the PRD §17 cut order).

```
export DRIFTGATE_TELEMETRY=1 DRIFTGATE_RUN_ID=local-1
uv run python -m gate.telemetry root --run-id local-1 --model <model file> -- bash -c '
  deploy/build.sh --commit $(git rev-parse HEAD) --run-id local-1 &&
  uv run python -m gate run --budget 16 --seed 0 --server http://127.0.0.1:8082'
```

In Jaeger (service `driftgate-gate`), the trace is `gate.run` → `gate.D1`, `gate.D2`, `gate.cli.run` → `gate.D3`, `gate.D5`, `gate.D6`. The D6 span is short because D6 only computes p95 latency from the D3 run's timings.

The gate also publishes each verdict as gauges labelled `run_id` and `mode`. These are `driftgate_d3_mean_agreement_share`, `driftgate_d3_p95_approx_kl`, `driftgate_d5_delta_accuracy`, `driftgate_d6_p95_ttft_ms`, `driftgate_d6_p95_itl_ms` and `driftgate_gate_exit_code`, sent through the collector to Prometheus. The Grafana panels "TTFT and inter-token latency p95" and "Gate score per run" show them.

Root-span attributes follow the OpenTelemetry GenAI semantic conventions, **schema 1.44.0**, pinned through `opentelemetry-semantic-conventions==0.66b2`. These conventions have **Development** status (no `gen_ai.*` attribute is stable yet), so the names may change in later semconv releases. The pinned version and status are also recorded on every run span (`driftgate.semconv.genai.version` / `.status`).
