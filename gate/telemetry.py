"""OpenTelemetry spans and metrics for the gate (FR-24, FR-23).

One trace per gate run: a `gate.run` root span with one `gate.D<n>` child span per detector,
exported over OTLP/HTTP to the local collector, which forwards traces to Jaeger and metrics to
Prometheus. D1 and D2 run as separate processes in `deploy/build.sh`, and D3, D5 and D6 run in
`python -m gate run`. The processes join one trace through the W3C `TRACEPARENT` environment
variable:

  python -m gate.telemetry root --run-id R -- <command>     # gate.run span; exports TRACEPARENT
  python -m gate.telemetry span D1 -- <command>             # gate.D1 span around one command

Telemetry is OFF unless `DRIFTGATE_TELEMETRY=1` (or `enabled: true` in the config), so tests,
CI and the Lab build host never try to reach a collector. When off, spans are still created
in memory and dropped, and the wrappers just run their command.

GenAI semantic conventions: the gen_ai.* attribute names come from the OpenTelemetry semantic
conventions schema pinned below. The GenAI conventions have **Development** status (none of
the gen_ai.* attributes are stable), so names may change between semconv releases; the version
is pinned through the `opentelemetry-semantic-conventions` package in pyproject.toml.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.semconv.schemas import Schemas
from opentelemetry.trace import Span, Status, StatusCode, Tracer, set_span_in_context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from gate.manifest import load_yaml

DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[1] / "observability" / "otel" / "gate-telemetry.yaml"
)
ENABLE_ENV = "DRIFTGATE_TELEMETRY"
TRACEPARENT_ENV = "TRACEPARENT"

GENAI_SEMCONV_SCHEMA = Schemas.V1_44_0
GENAI_SEMCONV_VERSION = "1.44.0"
GENAI_SEMCONV_STATUS = "Development"

DETECTORS = ("D1", "D2", "D3", "D4", "D5", "D6", "D7")

# Verdict values published as gauges (FR-23 "gate score per run"), labelled by run id. No OTel
# unit is set, so Prometheus names stay exactly these with dots as underscores (the unit is in
# the name). name -> path in the verdict's "detectors" object.
VERDICT_GAUGES = {
    "driftgate.d3.mean_agreement_share": "D3.mean_agreement_share",
    "driftgate.d3.p95_approx_kl": "D3.p95_approx_kl",
    "driftgate.d5.delta_accuracy": "D5.delta_accuracy",
    "driftgate.d6.p95_ttft_ms": "D6.candidate.p95_ttft_ms",
    "driftgate.d6.p95_itl_ms": "D6.candidate.p95_itl_ms",
}


def load_config(path: Path = DEFAULT_CONFIG, env: Mapping[str, str] | None = None) -> dict:
    env = os.environ if env is None else env
    cfg = load_yaml(path)
    if env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        cfg["endpoint"] = env["OTEL_EXPORTER_OTLP_ENDPOINT"]
    if env.get(ENABLE_ENV) in ("0", "1"):
        cfg["enabled"] = env[ENABLE_ENV] == "1"
    return cfg


def _lookup(obj: dict, dotted: str):
    for key in dotted.split("."):
        obj = obj[key]
    return obj


class GateTelemetry:
    """Owns the tracer and meter providers for one process.

    Pass `exporter` / `metric_exporter` / `metric_reader` in tests; otherwise OTLP/HTTP
    exporters are created only when telemetry is enabled.
    """

    def __init__(
        self,
        config: dict,
        exporter: SpanExporter | None = None,
        metric_exporter: MetricExporter | None = None,
        metric_reader=None,
        env: Mapping[str, str] | None = None,
    ):
        self.config = config
        self.env = os.environ if env is None else env
        enabled = config.get("enabled", False)
        endpoint = config["endpoint"].rstrip("/")
        resource = Resource.create(
            {"service.name": config["service_name"]}, schema_url=GENAI_SEMCONV_SCHEMA.value
        )
        self.provider = TracerProvider(resource=resource)
        if exporter is None and enabled:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=endpoint + "/v1/traces")
        if exporter is not None:
            self.provider.add_span_processor(BatchSpanProcessor(exporter))
        self.tracer: Tracer = self.provider.get_tracer(
            "driftgate.gate", schema_url=GENAI_SEMCONV_SCHEMA.value
        )

        if metric_exporter is None and enabled:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            metric_exporter = OTLPMetricExporter(endpoint=endpoint + "/v1/metrics")
        readers = [] if metric_reader is None else [metric_reader]
        if metric_exporter is not None:
            readers.append(PeriodicExportingMetricReader(metric_exporter))
        self.meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        meter = self.meter_provider.get_meter("driftgate.gate")
        self.gauges = {name: meter.create_gauge(name) for name in VERDICT_GAUGES}
        self.gate_exit_code = meter.create_gauge("driftgate.gate.exit_code")

    def _parent(self):
        carrier = (
            {"traceparent": self.env[TRACEPARENT_ENV]} if self.env.get(TRACEPARENT_ENV) else {}
        )
        return TraceContextTextMapPropagator().extract(carrier)

    def child_env(self, span: Span) -> dict[str, str]:
        """Environment for a subprocess whose spans should nest under `span`."""
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier, context=set_span_in_context(span))
        return {**self.env, TRACEPARENT_ENV: carrier["traceparent"]}

    @contextmanager
    def run(
        self, run_id: str, model: str | None = None, name: str = "gate.run", **attributes
    ) -> Iterator[Span]:
        """Span for a gate run (or one gate command); a child of TRACEPARENT if it is set."""
        attrs = {
            "driftgate.run_id": run_id,
            gen_ai.GEN_AI_PROVIDER_NAME: self.config["provider"],
            gen_ai.GEN_AI_OPERATION_NAME: self.config["operation"],
            "driftgate.semconv.genai.version": GENAI_SEMCONV_VERSION,
            "driftgate.semconv.genai.status": GENAI_SEMCONV_STATUS,
            **attributes,
        }
        if model:
            attrs[gen_ai.GEN_AI_REQUEST_MODEL] = model
        with self.tracer.start_as_current_span(name, context=self._parent(), attributes=attrs) as s:
            yield s

    @contextmanager
    def detector(self, detector_id: str, **attributes) -> Iterator[Span]:
        """Span for one detector stage; nests under the current span or TRACEPARENT."""
        if detector_id not in DETECTORS:
            raise ValueError(f"unknown detector {detector_id!r}")
        attrs = {"driftgate.detector": detector_id, **attributes}
        from opentelemetry import trace

        ctx = None if trace.get_current_span().get_span_context().is_valid else self._parent()
        with self.tracer.start_as_current_span(
            f"gate.{detector_id}", context=ctx, attributes=attrs
        ) as span:
            try:
                yield span
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise

    def record_verdict(self, verdict: dict, run_id: str) -> None:
        """Publish verdict values as gauges labelled by run id (gate score per run)."""
        labels = {"run_id": run_id, "mode": verdict["decision"]["mode"]}
        for name, path in VERDICT_GAUGES.items():
            try:
                value = _lookup(verdict["detectors"], path)
            except (KeyError, TypeError):
                continue
            if value is not None:
                self.gauges[name].set(value, labels)
        self.gate_exit_code.set(verdict["decision"]["exit_code"], labels)

    def shutdown(self) -> None:
        self.provider.shutdown()
        self.meter_provider.shutdown()


def from_env() -> GateTelemetry:
    return GateTelemetry(load_config())


# ---------- command wrappers (spans around build.sh steps and whole runs) ----------


def _wrapped(tel: GateTelemetry, span: Span, cmd: list[str]) -> int:
    code = subprocess.call(cmd, env=tel.child_env(span))
    span.set_attribute("process.exit.code", code)
    if code != 0:
        span.set_status(Status(StatusCode.ERROR, f"exit {code}"))
    return code


def main(argv: list[str] | None = None, tel: GateTelemetry | None = None) -> int:
    p = argparse.ArgumentParser(prog="gate.telemetry", description="Run a command in a span.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("root", help="gate.run span around a whole run")
    r.add_argument("--run-id", required=True)
    r.add_argument("--model")
    s = sub.add_parser("span", help="gate.D<n> span around one detector command")
    s.add_argument("detector", choices=DETECTORS)
    for q in (r, s):
        q.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        p.error("missing command after --")
    tel = tel or from_env()
    try:
        if args.cmd == "root":
            with tel.run(args.run_id, args.model) as span:
                return _wrapped(tel, span, cmd)
        with tel.detector(args.detector) as span:
            return _wrapped(tel, span, cmd)
    finally:
        tel.shutdown()


if __name__ == "__main__":
    sys.exit(main())
