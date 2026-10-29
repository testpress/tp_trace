"""RED Metrics and Sampling Gate Span Processors for tp_trace.

Provides two specialized SpanProcessors:
1. RedSpanProcessor: Computes sampling-independent RED metrics for 100% of finished
   spans (HTTP Server requests and CLIENT/PRODUCER/CONSUMER dependencies) before they
   are discarded or exported.
2. SampledOnlySpanProcessor: Wraps the export pipeline, ensuring only sampled traces
   (trace_flags.sampled=True) are serialized and transmitted over the network.
"""

import logging
import threading
from typing import Dict, Optional, Set

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import SpanKind, StatusCode

import tp_trace.metrics as metrics
from tp_trace.route_context import get_current_tags
from tp_trace.safety import attempt

logger = logging.getLogger("tp_trace.red")

_DEFAULT_OPS_LIMIT_PER_SERVICE = 128
_DEFAULT_ROUTE_LIMIT = 512
_DEFAULT_SERVICE_LIMIT = 64
_OVERFLOW_SENTINEL = "__other__"


class RedSpanProcessor(SpanProcessor):
    """Computes RED metrics on span completion.

    Evaluates 100% of finished spans (Django HTTP requests and dependencies like
    PostgreSQL, Redis, Requests, Celery) and records rate, error, and duration metrics
    into Prometheus natively, bypassing trace sampling decisions.
    """

    def __init__(
        self,
        cardinality_limit_per_service: int = _DEFAULT_OPS_LIMIT_PER_SERVICE,
        route_limit: int = _DEFAULT_ROUTE_LIMIT,
        service_limit: int = _DEFAULT_SERVICE_LIMIT,
    ) -> None:
        self.cardinality_limit_per_service = cardinality_limit_per_service
        self.route_limit = route_limit
        self.service_limit = service_limit
        self._lock = threading.Lock()
        self._seen_operations: Dict[str, Set[str]] = {}
        self._seen_routes: Set[str] = set()

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        if span.is_recording():
            kind = getattr(span, "kind", None)
            if kind in (SpanKind.CLIENT, SpanKind.PRODUCER, SpanKind.CONSUMER, SpanKind.SERVER):
                try:
                    span.set_attribute("tp_trace.red", 1)
                except Exception:
                    pass

    def on_end(self, span: ReadableSpan) -> None:
        attempt(self._process_span, span, _label="red_span_processor")

    def _process_span(self, span: ReadableSpan) -> None:
        kind = getattr(span, "kind", None)
        # Skip internal framework spans
        if kind == SpanKind.INTERNAL:
            return

        attrs = getattr(span, "attributes", {}) or {}

        # Determine normalized service
        service = (
            attrs.get("normalized.service")
            or attrs.get("service.name")
            or attrs.get("service")
            or attrs.get("messaging.system")
            or attrs.get("db.system")
            or attrs.get("rpc.system")
            or attrs.get("peer.service")
            or attrs.get("component")
        )
        if not service:
            if kind in (SpanKind.PRODUCER, SpanKind.CONSUMER):
                if (
                    attrs.get("component") == "celery"
                    or attrs.get("celery.task_name")
                    or attrs.get("celery.action")
                ):
                    service = "celery"
                else:
                    service = "messaging"
            elif kind == SpanKind.CLIENT:
                service = "client"
            elif kind == SpanKind.SERVER:
                if attrs.get("component") == "django" or attrs.get("django.view") or attrs.get("django.route"):
                    service = "django"
                else:
                    service = "server"
            else:
                return

        if service == "postgresql":
            service = "postgres"

        # Error check: 4xx responses are not server errors (error=False)
        status = getattr(span, "status", None)
        is_error = bool(
            attrs.get("error") is True
            or attrs.get("error") == "true"
            or (
                attrs.get("error") is not False
                and attrs.get("error") != "false"
                and status
                and getattr(status, "status_code", None) == StatusCode.ERROR
            )
        )

        status_code = attrs.get("http.status_code") or attrs.get("http.response.status_code")
        if status_code is None and kind == SpanKind.SERVER:
            if is_error:
                status_code = 500
            else:
                # SERVER span without status code and without error (e.g. unhandled bypass): skip recording
                return

        # Determine route template for low-cardinality routing
        route = (
            attrs.get("http.route_template")
            or attrs.get("django.route")
            or (attrs.get("http.route") if kind != SpanKind.SERVER else None)
            or ""
        )
        if not route and kind == SpanKind.SERVER:
            if status_code is not None:
                route = str(status_code)

        # Determine normalized operation safely without unbound dynamic string fallback
        operation = (
            attrs.get("normalized.operation")
            or attrs.get("db.operation")
            or (attrs.get("http.method") if kind == SpanKind.CLIENT else None)
        )
        if not operation:
            if kind == SpanKind.SERVER:
                method = attrs.get("http.method") or attrs.get("http.request.method") or "GET"
                operation = f"{method} {route}" if route else str(method)
            elif service in ("postgres", "pgbouncer"):
                operation = "query"
            elif service == "redis":
                operation = "command"
            elif service == "celery":
                operation = "task"
            elif kind in (SpanKind.PRODUCER, SpanKind.CONSUMER):
                operation = attrs.get("messaging.destination") or attrs.get("messaging.operation") or "task"
            else:
                raw_name = str(getattr(span, "name", "") or "")
                if raw_name and "/" not in raw_name and len(raw_name) <= 64:
                    operation = raw_name
                else:
                    operation = "unknown"

        # Thread-safe cardinality checks
        with self._lock:
            if len(self._seen_operations) >= self.service_limit and service not in self._seen_operations:
                service = _OVERFLOW_SENTINEL
            service_ops = self._seen_operations.setdefault(service, set())
            if len(service_ops) >= self.cardinality_limit_per_service:
                if operation not in service_ops:
                    operation = _OVERFLOW_SENTINEL
            else:
                service_ops.add(operation)

            if route:
                if len(self._seen_routes) >= self.route_limit:
                    if route not in self._seen_routes:
                        route = _OVERFLOW_SENTINEL
                else:
                    self._seen_routes.add(route)

        # Duration
        start_time = getattr(span, "start_time", None) or 0
        end_time = getattr(span, "end_time", None) or 0
        duration_ms = max(0.0, (end_time - start_time) / 1_000_000.0) if end_time >= start_time else 0.0

        span_kind_str = f"SPAN_KIND_{kind.name}" if kind is not None and hasattr(kind, "name") else "SPAN_KIND_CLIENT"

        # Direct extraction of subdomain without full attribute dict allocation
        subdomain = attrs.get("institute.subdomain") or attrs.get("institute_subdomain") or attrs.get("institute")

        http_method = str(attrs.get("http.method") or attrs.get("http.request.method") or "") or None

        if kind == SpanKind.SERVER:
            extra_attrs = dict(get_current_tags() or {})
            if subdomain:
                extra_attrs["institute.subdomain"] = str(subdomain)
                extra_attrs["institute"] = str(subdomain)
                extra_attrs["institute_subdomain"] = str(subdomain)
            metrics.record_request(
                method=http_method or "GET",
                route=route or str(status_code or 200),
                status_code=int(status_code or 200),
                error=is_error,
                duration_ms=duration_ms,
                service=str(service or "django"),
                operation=str(operation),
                extra_attributes=extra_attrs or None,
            )
        else:
            metrics.record_dependency_span(
                service=str(service),
                operation=str(operation),
                duration_ms=duration_ms,
                error=is_error,
                span_kind=span_kind_str,
                route=route or None,
                db_instance=str(attrs.get("db.instance") or attrs.get("db.connection_alias") or "") or None,
                db_system=str(attrs.get("db.system") or "") or None,
                http_method=http_method,
                status_code=status_code,
                subdomain=str(subdomain) if subdomain else None,
            )

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class SampledOnlySpanProcessor(SpanProcessor):
    """Wraps an export SpanProcessor, passing only sampled spans (trace_flags.sampled=True).

    Unsampled spans (Decision.RECORD_ONLY) are dropped immediately without
    ever being queued in BatchSpanProcessor or serialized over the network.
    """

    def __init__(self, delegate: SpanProcessor) -> None:
        self._delegate = delegate

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        if not self._delegate:
            return
        context = span.get_span_context()
        if context and context.trace_flags.sampled:
            self._delegate.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if not self._delegate:
            return
        context = span.get_span_context()
        if context and context.trace_flags.sampled:
            self._delegate.on_end(span)

    def shutdown(self) -> None:
        if self._delegate:
            self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        if self._delegate:
            return self._delegate.force_flush(timeout_millis)
        return True
