"""RED Metrics and Sampling Gate Span Processors for tp_trace.

Provides two specialized SpanProcessors:
1. RedSpanProcessor: Computes sampling-independent RED metrics for 100% of finished
   spans (CLIENT, PRODUCER, CONSUMER) before they are discarded or exported.
2. SampledOnlySpanProcessor: Wraps the export pipeline, ensuring only sampled traces
   (trace_flags.sampled=True) are serialized and transmitted over the network.
"""

import logging
from typing import Optional, Set

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import SpanKind, StatusCode

from tp_trace.metrics import record_dependency_span
from tp_trace.route_context import is_request_ignored
from tp_trace.safety import attempt

logger = logging.getLogger("tp_trace.red")

_DEFAULT_CARDINALITY_LIMIT = 1024
_OVERFLOW_SENTINEL = "__other__"


class RedSpanProcessor(SpanProcessor):
    """Computes RED metrics on span completion.

    Evaluates 100% of finished dependency spans (PostgreSQL, Redis, Requests, Celery)
    and records rate, error, and duration metrics into Prometheus natively,
    bypassing trace sampling decisions.
    """

    def __init__(self, cardinality_limit: int = _DEFAULT_CARDINALITY_LIMIT) -> None:
        self.cardinality_limit = cardinality_limit
        self._seen_operations: Set[str] = set()
        self._seen_routes: Set[str] = set()

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        attempt(self._process_span, span, _label="red_span_processor")

    def _process_span(self, span: ReadableSpan) -> None:
        if is_request_ignored():
            return

        kind = getattr(span, "kind", None)
        # Skip internal framework spans
        if kind == SpanKind.INTERNAL:
            return

        attrs = getattr(span, "attributes", {}) or {}

        # Skip Django SERVER spans: request.py already records rich HTTP RED metrics for them
        if kind == SpanKind.SERVER and (
            attrs.get("normalized.service") == "django" or attrs.get("django.route") is not None
        ):
            return

        # Determine normalized service
        service = (
            attrs.get("normalized.service")
            or attrs.get("db.system")
            or attrs.get("rpc.system")
            or attrs.get("peer.service")
            or attrs.get("component")
        )
        if not service:
            if kind in (SpanKind.PRODUCER, SpanKind.CONSUMER):
                service = "celery"
            else:
                return

        if service == "postgresql":
            service = "postgres"

        # Determine normalized operation
        operation = (
            attrs.get("normalized.operation") or attrs.get("db.operation") or getattr(span, "name", "") or "unknown"
        )

        # Cardinality check on operation
        if len(self._seen_operations) >= self.cardinality_limit:
            if operation not in self._seen_operations:
                operation = _OVERFLOW_SENTINEL
        else:
            self._seen_operations.add(operation)

        # Route extraction and cardinality check
        route = attrs.get("http.route") or attrs.get("http.route_template") or attrs.get("django.route") or ""
        if route:
            if len(self._seen_routes) >= self.cardinality_limit:
                if route not in self._seen_routes:
                    route = _OVERFLOW_SENTINEL
            else:
                self._seen_routes.add(route)

        # Error check
        status = getattr(span, "status", None)
        is_error = bool(
            (status and getattr(status, "status_code", None) == StatusCode.ERROR)
            or attrs.get("error") is True
            or attrs.get("error") == "true"
        )

        # Duration
        start_time = getattr(span, "start_time", None) or 0
        end_time = getattr(span, "end_time", None) or 0
        duration_ms = max(0.0, (end_time - start_time) / 1_000_000.0) if end_time >= start_time else 0.0

        span_kind_str = f"SPAN_KIND_{kind.name}" if kind is not None and hasattr(kind, "name") else "SPAN_KIND_CLIENT"

        # Tag span so collector can know this span was counted by SDK
        target_dict = getattr(attrs, "_dict", None)
        if target_dict is None and hasattr(attrs, "__setitem__"):
            target_dict = attrs
        if target_dict is not None and hasattr(target_dict, "__setitem__"):
            try:
                target_dict["tp_trace.red"] = 1
            except Exception:
                pass

        record_dependency_span(
            service=str(service),
            operation=str(operation),
            duration_ms=duration_ms,
            error=is_error,
            span_kind=span_kind_str,
            route=route or None,
            db_instance=str(attrs.get("db.instance") or attrs.get("db.connection_alias") or "") or None,
            db_system=str(attrs.get("db.system") or "") or None,
            http_method=str(attrs.get("http.method") or "") or None,
            status_code=attrs.get("http.status_code"),
            extra_attributes=dict(attrs),
        )

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class SampledOnlySpanProcessor(SpanProcessor):
    """Wraps an export SpanProcessor, passing only sampled spans (trace_flags.sampled=True).

    Unsampled spans (Decision.RECORD_ONLY) are dropped immediately at on_end() without
    ever being queued in BatchSpanProcessor or serialized over the network.
    """

    def __init__(self, delegate: SpanProcessor) -> None:
        self._delegate = delegate

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        if self._delegate:
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
