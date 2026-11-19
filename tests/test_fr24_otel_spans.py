"""FR-24: one span per gate stage (one per detector) under a gate.run span; GenAI
semconv version pinned and marked Development. Offline: spans go to an in-memory exporter."""

import tomllib
from pathlib import Path

import pytest
import yaml
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gate import telemetry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def traced():
    exporter = InMemorySpanExporter()
    tel = telemetry.GateTelemetry(telemetry.load_config(), exporter=exporter)
    yield tel, exporter
    tel.shutdown()


def test_one_child_span_per_detector(traced):
    tel, exporter = traced
    with tel.run("run-1", "qwen2.5-0.5b-instruct-q8_0.gguf"):
        for d in telemetry.DETECTORS:
            with tel.detector(d):
                pass
    tel.provider.force_flush()
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "gate.run")
    children = [s for s in spans if s.parent is not None]
    assert [s.name for s in children] == [f"gate.{d}" for d in telemetry.DETECTORS]
    for s in children:
        assert s.parent.span_id == root.context.span_id
        assert s.end_time >= s.start_time
        assert s.attributes["driftgate.detector"] == s.name.removeprefix("gate.")


def test_root_span_genai_attributes(traced):
    tel, exporter = traced
    with tel.run("run-2", "m.gguf"):
        pass
    tel.provider.force_flush()
    (root,) = exporter.get_finished_spans()
    assert root.attributes["gen_ai.request.model"] == "m.gguf"
    assert root.attributes["gen_ai.provider.name"] == "llama.cpp"
    assert root.attributes["gen_ai.operation.name"] == "text_completion"
    assert root.attributes["driftgate.semconv.genai.status"] == "Development"
    assert root.resource.schema_url == telemetry.GENAI_SEMCONV_SCHEMA.value


def test_detector_error_marks_span(traced):
    tel, exporter = traced
    with pytest.raises(RuntimeError), tel.run("run-3", "m"), tel.detector("D3"):
        raise RuntimeError("boom")
    tel.provider.force_flush()
    d3 = next(s for s in exporter.get_finished_spans() if s.name == "gate.D3")
    assert not d3.status.is_ok


def test_unknown_detector_rejected(traced):
    tel, _ = traced
    with pytest.raises(ValueError):
        with tel.detector("D9"):
            pass


def test_semconv_version_pinned_and_development():
    assert telemetry.GENAI_SEMCONV_STATUS == "Development"
    assert telemetry.GENAI_SEMCONV_SCHEMA.value.endswith("/" + telemetry.GENAI_SEMCONV_VERSION)
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    pinned = [d for d in deps if d.startswith("opentelemetry-semantic-conventions")]
    assert pinned and "==" in pinned[0]
    assert "Development" in (ROOT / "gate" / "telemetry.py").read_text()


def test_endpoint_from_config_and_env(monkeypatch):
    cfg = yaml.safe_load((ROOT / "observability/otel/gate-telemetry.yaml").read_text())
    assert telemetry.load_config()["endpoint"] == cfg["endpoint"]
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    assert telemetry.load_config()["endpoint"] == "http://collector:4318"
