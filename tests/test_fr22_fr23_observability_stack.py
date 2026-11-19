"""FR-22 / FR-23 / FR-24 (stack side): local Prometheus, Grafana, OTel collector and
Jaeger configs. Offline checks; the live "targets up" check is in the PR evidence."""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OBS = ROOT / "observability"
COMPOSE = yaml.safe_load((OBS / "docker-compose.yml").read_text())
PROM = yaml.safe_load((OBS / "prometheus.yml").read_text())
COLLECTOR = yaml.safe_load((OBS / "otel" / "collector.yaml").read_text())
DASHBOARD = json.loads((OBS / "grafana" / "dashboards" / "driftgate.json").read_text())
LLAMA_SERVER_LOCAL_PORT = 8082

# Metric names exported by llama-server --metrics at the pinned build (checked live).
LLAMA_METRICS = {
    "llamacpp:prompt_tokens_total",
    "llamacpp:prompt_tokens_cached_total",
    "llamacpp:prompt_seconds_total",
    "llamacpp:tokens_predicted_total",
    "llamacpp:tokens_predicted_seconds_total",
    "llamacpp:prompt_tokens_seconds",
    "llamacpp:predicted_tokens_seconds",
    "llamacpp:requests_processing",
    "llamacpp:requests_deferred",
}


def test_compose_services_and_pinned_images():
    services = COMPOSE["services"]
    assert {"prometheus", "grafana", "otel-collector", "jaeger"} <= set(services)
    for name, svc in services.items():
        image = svc["image"]
        assert re.search(r":v?\d+\.\d+\.\d+$", image), (name, image)


def test_prometheus_scrapes_llama_server_metrics():
    jobs = {j["job_name"]: j for j in PROM["scrape_configs"]}
    job = jobs["llama-server"]
    assert job["metrics_path"] == "/metrics"
    targets = [t for sc in job["static_configs"] for t in sc["targets"]]
    assert targets == [f"host.docker.internal:{LLAMA_SERVER_LOCAL_PORT}"]
    assert "host.docker.internal:host-gateway" in COMPOSE["services"]["prometheus"]["extra_hosts"]


def test_prometheus_scrapes_gate_metrics_from_collector():
    jobs = {j["job_name"]: j for j in PROM["scrape_configs"]}
    port = COLLECTOR["exporters"]["prometheus"]["endpoint"].rsplit(":", 1)[1]
    assert jobs["driftgate-gate"]["static_configs"][0]["targets"] == [f"otel-collector:{port}"]


def test_collector_sends_traces_to_jaeger_and_file():
    pipeline = COLLECTOR["service"]["pipelines"]["traces"]
    assert "otlp" in pipeline["receivers"]
    jaeger = [e for e in pipeline["exporters"] if e.endswith("/jaeger")]
    assert jaeger and COLLECTOR["exporters"][jaeger[0]]["endpoint"] == "jaeger:4317"
    assert "file" in pipeline["exporters"]
    assert "prometheus" in COLLECTOR["service"]["pipelines"]["metrics"]["exporters"]


def test_grafana_provisions_prometheus_and_dashboard():
    ds = yaml.safe_load((OBS / "grafana/provisioning/datasources/prometheus.yaml").read_text())
    (source,) = ds["datasources"]
    assert source["type"] == "prometheus" and source["url"] == "http://prometheus:9090"
    prov = yaml.safe_load((OBS / "grafana/provisioning/dashboards/driftgate.yaml").read_text())
    mount = prov["providers"][0]["options"]["path"]
    volumes = COMPOSE["services"]["grafana"]["volumes"]
    assert any(v.endswith(":" + mount + ":ro") for v in volumes)


@pytest.mark.parametrize(
    "title",
    [
        "Generation tokens/s",
        "Prompt tokens/s",
        "Queue",
        "TTFT and inter-token latency p95 (gate D6)",
        "Gate score per run",
    ],
)
def test_dashboard_has_fr23_panels(title):
    assert title in {p["title"] for p in DASHBOARD["panels"]}


def test_dashboard_queries_known_metrics():
    from gate.telemetry import VERDICT_GAUGES

    gate_metrics = {n.replace(".", "_") for n in VERDICT_GAUGES} | {"driftgate_gate_exit_code"}
    for panel in DASHBOARD["panels"]:
        for target in panel["targets"]:
            names = set(re.findall(r"llamacpp:[a-z_]+", target["expr"]))
            assert names <= LLAMA_METRICS, (panel["title"], names - LLAMA_METRICS)
            gate = set(re.findall(r"driftgate_[a-z0-9_]+", target["expr"]))
            assert gate <= gate_metrics, (panel["title"], gate - gate_metrics)
            assert target["datasource"]["uid"] == "prometheus"
