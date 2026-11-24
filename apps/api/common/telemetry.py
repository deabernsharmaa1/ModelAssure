"""OpenTelemetry tracing (spec §5.1 Observability).

Each test case runs in its own span; the W3C `traceparent` header is sent to the decision system
so a failure can be correlated with the customer's own traces. If an OTLP exporter is installed
and OTEL_EXPORTER_OTLP_ENDPOINT is set, spans are exported; otherwise ids are still generated.
"""
from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

_initialised = False


def init_tracing(service: str = "modelassure-worker") -> None:
    global _initialised
    if _initialised:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except ImportError:  # exporter package not installed
            pass
    trace.set_tracer_provider(provider)
    _initialised = True


def tracer():
    init_tracing()
    return trace.get_tracer("modelassure")


def current_traceparent() -> tuple[str | None, str | None]:
    carrier: dict[str, str] = {}
    inject(carrier)
    tp = carrier.get("traceparent")
    trace_id = tp.split("-")[1] if tp else None
    return tp, trace_id
