"""OpenTelemetry tracing and Prometheus metrics for the gateway.

The module deliberately records operational metadata only. Prompts, responses,
authorization headers, and upstream API keys must never be attached to spans.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from prometheus_client import Counter, Gauge, Histogram

if TYPE_CHECKING:
    from fastapi import FastAPI


GATEWAY_REQUESTS = Counter(
    "tokencoin_gateway_requests_total",
    "Completed gateway requests.",
    ("model", "outcome", "stream"),
)
UPSTREAM_ATTEMPTS = Counter(
    "tokencoin_upstream_attempts_total",
    "Upstream attempts made by the router.",
    ("source_id", "model", "outcome"),
)
FAILOVERS = Counter(
    "tokencoin_failovers_total",
    "Requests that moved to another source before response streaming began.",
    ("model", "reason"),
)
TOKENS = Counter(
    "tokencoin_tokens_total",
    "Tokens reported by the upstream provider.",
    ("source_id", "model", "direction"),
)
UPSTREAM_LATENCY = Histogram(
    "tokencoin_upstream_latency_seconds",
    "End-to-end upstream request latency.",
    ("source_id", "model"),
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
TIME_TO_FIRST_TOKEN = Histogram(
    "tokencoin_time_to_first_token_seconds",
    "Time until the first upstream SSE payload is available.",
    ("source_id", "model"),
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
CIRCUIT_STATE = Gauge(
    "tokencoin_source_circuit_state",
    "Circuit state: healthy=0, degraded=1, half_open=2, open=3.",
    ("source_id",),
)


@lru_cache(maxsize=1)
def tracer() -> trace.Tracer:
    return trace.get_tracer("tokencoin.gateway", "0.2.0")


def configure_telemetry(app: "FastAPI") -> None:
    """Configure tracing once and instrument the FastAPI application."""

    current = trace.get_tracer_provider()
    if not isinstance(current, TracerProvider):
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": "tokencoin-gateway",
                    "service.version": "0.2.0",
                    "deployment.environment": os.getenv(
                        "TOKENCOIN_ENVIRONMENT", "local"
                    ),
                }
            )
        )

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
        elif os.getenv("TOKENCOIN_OTEL_CONSOLE", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="/healthz,/metrics",
    )


def set_circuit_state(source_id: str, state: str) -> None:
    values = {"healthy": 0, "degraded": 1, "half_open": 2, "open": 3}
    normalized = state.replace("-", "_")
    CIRCUIT_STATE.labels(source_id=source_id).set(values.get(normalized, 1))


def record_tokens(
    source_id: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    if prompt_tokens > 0:
        TOKENS.labels(
            source_id=source_id, model=model, direction="input"
        ).inc(prompt_tokens)
    if completion_tokens > 0:
        TOKENS.labels(
            source_id=source_id, model=model, direction="output"
        ).inc(completion_tokens)
