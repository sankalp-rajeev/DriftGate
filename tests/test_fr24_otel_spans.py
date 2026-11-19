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


# ---------- one trace across processes, CLI spans, verdict metrics ----------

from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402

from gate import cli  # noqa: E402


def _tel(exporter, env=None, **kw):
    return telemetry.GateTelemetry(telemetry.load_config(env={}), exporter=exporter, env=env, **kw)


def test_disabled_by_default_and_env_enables():
    assert telemetry.load_config(env={})["enabled"] is False
    assert telemetry.load_config(env={"DRIFTGATE_TELEMETRY": "1"})["enabled"] is True


def test_traceparent_joins_child_process_spans_to_one_trace():
    parent_exp, child_exp = InMemorySpanExporter(), InMemorySpanExporter()
    parent = _tel(parent_exp)
    with parent.run("run-x", "m") as root:
        env = parent.child_env(root)
        child = _tel(child_exp, env=env)  # stands in for a separate process
        with child.detector("D1"):
            pass
        child.shutdown()
    parent.shutdown()
    spans = {s.name: s for s in parent_exp.get_finished_spans() + child_exp.get_finished_spans()}
    assert spans["gate.D1"].context.trace_id == spans["gate.run"].context.trace_id
    assert spans["gate.D1"].parent.span_id == spans["gate.run"].context.span_id


def test_span_wrapper_runs_command_and_keeps_exit_code():
    exporter = InMemorySpanExporter()
    assert telemetry.main(["span", "D2", "--", "false"], tel=_tel(exporter)) == 1
    (span,) = exporter.get_finished_spans()
    assert span.name == "gate.D2"
    assert span.attributes["process.exit.code"] == 1
    assert not span.status.is_ok


def test_gate_cli_emits_d6_span_and_verdict_gauges(tmp_path, monkeypatch):
    from test_fr06_gate_cli import _files

    monkeypatch.delenv(cli.FORCE_FAIL_ENV, raising=False)
    monkeypatch.setenv(cli.RUN_ID_ENV, "run-cli")
    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tel = _tel(exporter, metric_reader=reader)
    files = _files(tmp_path)
    data_before_shutdown = []
    real_shutdown = tel.shutdown
    tel.shutdown = lambda: (data_before_shutdown.append(reader.get_metrics_data()), real_shutdown())
    assert cli.main(files, tel=tel) == 0
    names = [s.name for s in exporter.get_finished_spans()]
    assert names == ["gate.D6", "gate.cli.compare"]
    (data,) = data_before_shutdown
    points = {
        m.name: m.data.data_points[0]
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert "driftgate.d3.mean_agreement_share" in points
    assert points["driftgate.gate.exit_code"].attributes["run_id"] == "run-cli"
