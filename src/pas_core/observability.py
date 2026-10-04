"""Metrics, tracing, structured logging and correlation IDs.

Implements the four pillars the specification asks for (Prometheus + Grafana +
Jaeger) without making any of them a hard dependency: if the OpenTelemetry SDK is
absent the module degrades to structured JSON logs plus an in-process registry.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

CORRELATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pas_correlation_id", default=""
)
TENANT_ID: contextvars.ContextVar[str] = contextvars.ContextVar("pas_tenant_id", default="")

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime", "message", "taskName",
}


class CorrelationIdFilter(logging.Filter):
    """Adds ``correlationId``/``tenantId`` to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = CORRELATION_ID.get() or "-"
        record.tenant_id = TENANT_ID.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Structured formatter that keeps non-standard fields."""

    def format(self, record: logging.LogRecord) -> str:
        import json  # noqa: PLC0415

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", "-")
        tid = getattr(record, "tenant_id", "-")
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} [{cid[:8]}/{tid}] {record.name}: {record.getMessage()}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(level: str = "INFO", *, fmt: str = "json") -> None:
    """Idempotently install the suite's logging configuration."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    handler.addFilter(CorrelationIdFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def new_correlation_id() -> str:
    """W3C-style traceable identifier."""
    return uuid.uuid4().hex


@contextmanager
def correlation_scope(correlation_id: str | None = None, tenant_id: str | None = None) -> Iterator[str]:
    """Bind a correlation id (and optionally tenant) for the enclosed block."""
    cid = correlation_id or new_correlation_id()
    token = CORRELATION_ID.set(cid)
    tenant_token = TENANT_ID.set(tenant_id) if tenant_id else None
    try:
        yield cid
    finally:
        CORRELATION_ID.reset(token)
        if tenant_token is not None:
            TENANT_ID.reset(tenant_token)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
try:  # pragma: no cover - optional dependency guard
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

    _HAS_PROM = True
except ImportError:  # pragma: no cover
    _HAS_PROM = False

if _HAS_PROM:
    REGISTRY: Any = CollectorRegistry()
    HTTP_REQUESTS = Counter(
        "pas_http_requests_total",
        "HTTP requests by plugin, method, path template and status.",
        ["plugin", "method", "path", "status"],
        registry=REGISTRY,
    )
    HTTP_DURATION = Histogram(
        "pas_http_request_duration_seconds",
        "HTTP request latency.",
        ["plugin", "method", "path"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        registry=REGISTRY,
    )
    PAS_UPSTREAM_CALLS = Counter(
        "pas_upstream_calls_total",
        "Calls to the connected Policy Administration System.",
        ["vendor", "operation", "outcome"],
        registry=REGISTRY,
    )
    PAS_UPSTREAM_DURATION = Histogram(
        "pas_upstream_call_duration_seconds",
        "Latency of upstream PAS calls (detects slow legacy monoliths).",
        ["vendor", "operation"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 15.0, 60.0),
        registry=REGISTRY,
    )
    MCP_TOOL_CALLS = Counter(
        "pas_mcp_tool_calls_total",
        "MCP tool invocations by tool name and outcome.",
        ["plugin", "tool", "outcome"],
        registry=REGISTRY,
    )
    WORKFLOW_STEPS = Histogram(
        "pas_workflow_step_duration_seconds",
        "Duration of individual workflow steps.",
        ["plugin", "workflow", "step", "outcome"],
        buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0),
        registry=REGISTRY,
    )
    ACTIVE_WORKFLOWS = Gauge(
        "pas_active_workflows",
        "Workflow executions currently in flight.",
        ["plugin"],
        registry=REGISTRY,
    )
    VALUATIONS = Counter(
        "pas_ifrs17_valuations_total",
        "IFRS 17 valuation runs by measurement model and outcome.",
        ["measurement_model", "outcome"],
        registry=REGISTRY,
    )
    MODEL_SCORE_LATENCY = Histogram(
        "pas_model_score_latency_seconds",
        "Underwriting model inference latency.",
        ["model", "version"],
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
        registry=REGISTRY,
    )
    DRIFT_SCORE = Gauge(
        "pas_feature_drift_psi",
        "Population stability index per monitored feature.",
        ["feature", "model"],
        registry=REGISTRY,
    )
else:  # pragma: no cover
    REGISTRY = None
    HTTP_REQUESTS = HTTP_DURATION = PAS_UPSTREAM_CALLS = PAS_UPSTREAM_DURATION = None
    MCP_TOOL_CALLS = WORKFLOW_STEPS = ACTIVE_WORKFLOWS = None
    VALUATIONS = MODEL_SCORE_LATENCY = DRIFT_SCORE = None


def record_http_request(plugin: str, method: str, path: str, status: int, duration: float) -> None:
    """Emit HTTP metrics when Prometheus is available (no-op otherwise)."""
    if HTTP_REQUESTS is not None:
        HTTP_REQUESTS.labels(plugin=plugin, method=method, path=path, status=str(status)).inc()
    if HTTP_DURATION is not None:
        HTTP_DURATION.labels(plugin=plugin, method=method, path=path).observe(duration)


def record_pas_call(vendor: str, operation: str, outcome: str, duration: float) -> None:
    if PAS_UPSTREAM_CALLS is not None:
        PAS_UPSTREAM_CALLS.labels(vendor=vendor, operation=operation, outcome=outcome).inc()
    if PAS_UPSTREAM_DURATION is not None:
        PAS_UPSTREAM_DURATION.labels(vendor=vendor, operation=operation).observe(duration)


def prometheus_payload() -> bytes:
    """Render the registry in the Prometheus text exposition format."""
    if not _HAS_PROM:
        return b"# prometheus_client not installed\n"
    return generate_latest(REGISTRY)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# OpenTelemetry tracing (optional)
# ---------------------------------------------------------------------------
class Span:
    """Minimal span that works with or without the OpenTelemetry SDK."""

    def __init__(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.name = name
        self.attributes: dict[str, Any] = dict(attributes or {})
        self.started_at = time.perf_counter()
        self.ended_at: float | None = None
        self.status = "unset"
        self._otel_span: Any = None

    def set_attributes(self, **attributes: Any) -> None:
        self.attributes.update(attributes)

    def end(self, status: str = "ok") -> float:
        self.ended_at = time.perf_counter()
        self.status = status
        if self._otel_span is not None:
            self._otel_span.end()
        return self.duration

    @property
    def duration(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return end - self.started_at

    def __enter__(self) -> Span:
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        self.end("error" if exc_type else "ok")


@contextmanager
def trace_span(name: str, **attributes: Any) -> Iterator[Span]:
    """Trace a unit of work; integrates with Jaeger when the SDK is installed."""
    span = Span(name, attributes)
    try:  # pragma: no cover - depends on optional SDK
        from opentelemetry import trace  # noqa: PLC0415

        tracer = trace.get_tracer("pas-plugins")
        with tracer.start_as_current_span(name) as otel_span:
            for key, value in attributes.items():
                otel_span.set_attribute(f"pas.{key}", value)
            span._otel_span = otel_span  # noqa: SLF001
            yield span
            span.end()
            return
    except ImportError:
        pass
    yield span


class Timer:
    """Small RAII timer for latency measurement without tracing infrastructure."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.elapsed = 0.0

    def __enter__(self) -> Timer:
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed = time.perf_counter() - self.started

    def __call__(self) -> float:
        return self.elapsed if self.elapsed else time.perf_counter() - self.started


def setup_tracing(service_name: str, *, endpoint: str | None = None) -> bool:
    """Install a Jaeger/OTLP exporter when configured. Returns True when active."""
    if not endpoint:
        return False
    try:  # pragma: no cover - optional dependency guard
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name, "service.namespace": "pas-plugins"})
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        from opentelemetry import trace  # noqa: PLC0415

        trace.set_tracer_provider(provider)
        return True
    except Exception:  # noqa: BLE001
        logging.getLogger("pas_core.observability").warning("tracing setup failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# In-process metric snapshots used by the Svelte management UIs when the
# Prometheus endpoint is not reachable (or when running single-container).
# ---------------------------------------------------------------------------
class MetricsRegistry:
    """Counters/histograms kept in-process and rendered as JSON for the UI."""

    def __init__(self) -> None:
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._timings: dict[str, list[float]] = {}

    def increment(self, name: str, value: float = 1.0, **labels: Any) -> None:
        key = _label_key(name, labels)
        self._counters[key] = self._counters.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        self._gauges[_label_key(name, labels)] = value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        self._timings.setdefault(_label_key(name, labels), []).append(value)

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "timings": {
                key: {
                    "count": len(values),
                    "sum": round(sum(values), 6),
                    "avg": round(sum(values) / len(values), 6) if values else 0.0,
                    "max": round(max(values), 6) if values else 0.0,
                }
                for key, values in self._timings.items()
            },
        }

    def reset(self) -> None:
        self._counters.clear()
        self._gauges.clear()
        self._timings.clear()


def _label_key(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return name
    rendered = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{rendered}}}"


GLOBAL_METRICS = MetricsRegistry()


def sla_timer(threshold_ms: float, name: str, **labels: Any) -> Any:
    """Context manager that flags (but never raises on) an SLA breach.

    Embedded-distribution quote/bind calls have contractual SLAs, so a breach is
    recorded and alerted rather than turned into a client error.
    """

    @contextmanager
    def _timer() -> Iterator[Timer]:
        timer = Timer()
        with timer:
            yield timer
        elapsed_ms = timer.elapsed * 1000
        GLOBAL_METRICS.observe(f"{name}_duration_ms", elapsed_ms, **labels)
        if elapsed_ms > threshold_ms:
            GLOBAL_METRICS.increment(f"{name}_sla_breaches_total", **labels)

    return _timer()
