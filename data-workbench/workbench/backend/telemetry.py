"""Backend OpenTelemetry wiring — OPT-IN, endpoint-gated, fail-safe.

Everything here is a no-op unless an OTLP endpoint is configured (see
``config.telemetry_enabled()``, driven by the standard
``OTEL_EXPORTER_OTLP_ENDPOINT``). No ``opentelemetry`` import happens at module
import time, so a deployment that never installs the ``opentelemetry-*``
packages (a Phase-1/2 "CLI-telemetry-only" deploy) still boots and runs — the
imports are attempted lazily inside :func:`init_telemetry` and degrade to a
warning + disabled telemetry.

What this adds on top of the CLI's own export (which ``config.py`` enables via
env):
  * FastAPI request spans (latency / errors) under service
    ``data-workbench-backend``.
  * A live parent span around each Claude Agent SDK stage run
    (:func:`stage_span`) so the SDK injects W3C trace context into the spawned
    Claude Code CLI subprocess and the CLI's spans parent under the backend
    trace — one end-to-end trace across the process boundary.
  * The internal ``llm_usage`` token/cost ledger bridged into OTel metrics
    (:func:`record_usage_metrics`) so it lands in the same collector.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any

from . import config

_log = logging.getLogger("workbench.telemetry")

_initialized = False
_tracer_provider = None
_meter_provider = None
_token_counter = None
_cost_counter = None


def init_telemetry(app: Any) -> None:
    """Initialise the backend TracerProvider + MeterProvider and instrument the
    FastAPI app. No-op when telemetry is disabled or the ``opentelemetry-*``
    packages aren't installed. Safe to call exactly once at startup; never
    raises (a telemetry problem must not break the app)."""
    global _initialized, _tracer_provider, _meter_provider, _token_counter, _cost_counter
    if _initialized:
        return
    if not config.telemetry_enabled():
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception as e:  # noqa: BLE001 — packages absent / import problem
        _log.warning(
            "OTEL endpoint configured (%s) but opentelemetry packages are "
            "unavailable — backend self-instrumentation disabled (the Claude "
            "Code CLI still exports via env). Install opentelemetry-sdk / "
            "-exporter-otlp-proto-http / -instrumentation-fastapi to enable. %s",
            config.otel_endpoint(), e,
        )
        return
    try:
        # service.name/namespace are set in CODE (not env) so we don't rename the
        # CLI's own service. The OTLP exporters read OTEL_EXPORTER_OTLP_* from the
        # env that config.py already normalised.
        resource = Resource.create(
            {
                "service.name": "data-workbench-backend",
                "service.namespace": "data-workbench",
            }
        )
        _tracer_provider = TracerProvider(resource=resource)
        _tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(_tracer_provider)

        _meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
        metrics.set_meter_provider(_meter_provider)

        meter = metrics.get_meter("workbench.llm_usage")
        _token_counter = meter.create_counter(
            "wb.llm.tokens",
            unit="{token}",
            description="Working tokens (uncached input + output) per LLM call.",
        )
        _cost_counter = meter.create_counter(
            "wb.llm.cost_usd",
            unit="USD",
            description="LLM cost in USD per call, when the SDK reports it.",
        )

        FastAPIInstrumentor().instrument_app(app)
        _initialized = True
        _log.info("OpenTelemetry initialised (OTLP → %s)", config.otel_endpoint())
    except Exception as e:  # noqa: BLE001 — never break startup
        _log.warning("OpenTelemetry init failed — telemetry disabled: %s", e)


def shutdown_telemetry() -> None:
    """Flush + shut down the providers on app exit. Best-effort."""
    global _initialized
    for provider in (_tracer_provider, _meter_provider):
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:  # noqa: BLE001
                pass
    _initialized = False


def stage_span(name: str, attributes: dict | None = None):
    """Return a context manager that starts a *current* span when telemetry is
    active, else a :func:`contextlib.nullcontext`. Wrapping the Claude Agent SDK
    ``query()`` loop in this parents the spawned CLI subprocess's spans under the
    backend trace (the SDK injects W3C trace context into the subprocess env when
    a span is active). No-op — and zero cost — when telemetry is off."""
    if not _initialized:
        return nullcontext()
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("workbench.sdk_runner")
        return tracer.start_as_current_span(name, attributes=attributes or {})
    except Exception:  # noqa: BLE001
        return nullcontext()


def record_usage_metrics(
    *, source: str, usage: dict | None, project_code: str | None = None
) -> None:
    """Bridge the ``llm_usage`` ledger into OTel counters. Best-effort no-op when
    telemetry is off or a counter isn't available."""
    if not _initialized or not usage:
        return
    try:
        attrs: dict[str, str] = {"source": source}
        if project_code:
            attrs["project_code"] = project_code
        model = usage.get("model")
        if model:
            attrs["model"] = str(model)
        total = int(usage.get("total_tokens") or 0)
        if total and _token_counter is not None:
            _token_counter.add(total, attrs)
        cost = usage.get("cost_usd")
        if isinstance(cost, (int, float)) and cost and _cost_counter is not None:
            _cost_counter.add(float(cost), attrs)
    except Exception:  # noqa: BLE001
        pass
