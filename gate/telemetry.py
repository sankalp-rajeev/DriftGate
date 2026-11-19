"""OpenTelemetry spans for the gate (FR-24): one root span per gate run, one child span per
detector stage (D1 to D6), exported over OTLP/HTTP to the local collector, which forwards
them to Jaeger.

GenAI semantic conventions: the attribute names come from the OpenTelemetry semantic
conventions schema pinned below. The GenAI conventions have **Development** status (none
of the gen_ai.* attributes are stable), so names may change between semconv releases; the
version is pinned through the `opentelemetry-semantic-conventions` package in pyproject.toml.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.semconv.schemas import Schemas
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from gate.manifest import load_yaml

DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[1] / "observability" / "otel" / "gate-telemetry.yaml"
)

GENAI_SEMCONV_SCHEMA = Schemas.V1_44_0
GENAI_SEMCONV_VERSION = "1.44.0"
GENAI_SEMCONV_STATUS = "Development"

DETECTORS = ("D1", "D2", "D3", "D4", "D5", "D6")


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    cfg = load_yaml(path)
    env = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if env:
        cfg["endpoint"] = env
    return cfg


class GateTelemetry:
    """Owns a TracerProvider for one gate process. Pass `exporter` in tests."""

    def __init__(self, config: dict, exporter: SpanExporter | None = None):
        self.config = config
        resource = Resource.create(
            {"service.name": config["service_name"]}, schema_url=GENAI_SEMCONV_SCHEMA.value
        )
        self.provider = TracerProvider(resource=resource)
        if exporter is None and config.get("enabled", True):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=config["endpoint"].rstrip("/") + "/v1/traces")
        if exporter is not None:
            self.provider.add_span_processor(BatchSpanProcessor(exporter))
        self.tracer: Tracer = self.provider.get_tracer(
            "driftgate.gate", schema_url=GENAI_SEMCONV_SCHEMA.value
        )

    @contextmanager
    def run(self, run_id: str, model: str, **attributes) -> Iterator[Span]:
        """Root span for one gate run."""
        attrs = {
            "driftgate.run_id": run_id,
            gen_ai.GEN_AI_REQUEST_MODEL: model,
            gen_ai.GEN_AI_PROVIDER_NAME: self.config["provider"],
            gen_ai.GEN_AI_OPERATION_NAME: self.config["operation"],
            "driftgate.semconv.genai.version": GENAI_SEMCONV_VERSION,
            "driftgate.semconv.genai.status": GENAI_SEMCONV_STATUS,
            **attributes,
        }
        with self.tracer.start_as_current_span("gate.run", attributes=attrs) as span:
            yield span

    @contextmanager
    def detector(self, detector_id: str, **attributes) -> Iterator[Span]:
        """Child span for one detector stage; nests under the current gate.run span."""
        if detector_id not in DETECTORS:
            raise ValueError(f"unknown detector {detector_id!r}")
        attrs = {"driftgate.detector": detector_id, **attributes}
        with self.tracer.start_as_current_span(f"gate.{detector_id}", attributes=attrs) as span:
            try:
                yield span
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise

    def shutdown(self) -> None:
        self.provider.shutdown()
