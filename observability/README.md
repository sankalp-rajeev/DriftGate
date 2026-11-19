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

## Gate traces (FR-24)

`gate/telemetry.py` creates the spans; `otel/gate-telemetry.yaml` configures the exporter, and `OTEL_EXPORTER_OTLP_ENDPOINT` overrides its endpoint. Root-span attributes follow the OpenTelemetry GenAI semantic conventions, **schema 1.44.0**, pinned through `opentelemetry-semantic-conventions==0.66b2`. These conventions have **Development** status (no `gen_ai.*` attribute is stable yet), so the names may change in later semconv releases. The pinned version and status are also recorded on every run span (`driftgate.semconv.genai.version` / `.status`).
