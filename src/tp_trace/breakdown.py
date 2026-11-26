"""Exact per-request time breakdown tracking for Django HTTP requests."""

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import SpanKind

from tp_trace.compat.attributes import get_attributes_view

logger = logging.getLogger("tp_trace.breakdown")

BreakdownKey = Tuple[str, str, str]  # (normalized_service, db_instance, db_connection_pool)
SpanInterval = Tuple[int, int, str, str, str]  # (start_ns, end_ns, service, db_instance, db_pool)

_EXCLUDED_DEPENDENCY_SERVICES = frozenset({"", "django", "celery", "pgbouncer"})
_MAX_ACTIVE_REQUESTS = 10000


class _RequestBucket:
    __slots__ = ("root_span_id", "intervals")

    def __init__(self, root_span_id: int) -> None:
        self.root_span_id = root_span_id
        self.intervals: List[SpanInterval] = []


def _get_span_context(span: Any) -> Any:
    ctx = getattr(span, "context", None)
    if ctx is None and hasattr(span, "get_span_context"):
        try:
            ctx = span.get_span_context()
        except Exception:
            ctx = None
    return ctx


def _get_span_kind(span: Any) -> Any:
    kind = getattr(span, "kind", None)
    if kind is None and hasattr(span, "_kind"):
        kind = getattr(span, "_kind", None)
    return kind


def _normalize_dependency_service(attrs: Dict[str, Any]) -> str:
    svc = (
        attrs.get("normalized.service")
        or attrs.get("peer.service")
        or attrs.get("db.system")
        or attrs.get("rpc.system")
        or attrs.get("component")
        or ""
    )
    svc_str = str(svc).strip()
    if svc_str == "postgresql":
        return "postgres"
    if svc_str == "aws-api":
        return "aws-s3"
    return svc_str


def compute_request_breakdown(
    request_start_ns: int,
    request_end_ns: int,
    intervals: List[SpanInterval],
) -> Dict[BreakdownKey, float]:
    """Compute exact per-component millisecond breakdown for one request.

    Uses a sweep-line over clamped child CLIENT intervals so concurrent or
    overlapping calls never exceed 100% of the request duration. Overlapped
    segments are split equally across the active child intervals, and the
    remaining wall-clock time is attributed to ``("django", "", "")``.
    """
    total_ns = request_end_ns - request_start_ns
    if total_ns <= 0:
        return {}

    clamped: List[SpanInterval] = []
    boundaries = set()
    for start_ns, end_ns, svc, db_inst, db_pool in intervals:
        s = max(start_ns, request_start_ns)
        e = min(end_ns, request_end_ns)
        if e > s:
            clamped.append((s, e, svc, db_inst, db_pool))
            boundaries.add(s)
            boundaries.add(e)

    if not clamped:
        return {("django", "", ""): total_ns / 1_000_000.0}

    sorted_pts = sorted(boundaries)
    ns_by_key: Dict[BreakdownKey, float] = {}
    total_dep_ns = 0

    for idx in range(len(sorted_pts) - 1):
        seg_start = sorted_pts[idx]
        seg_end = sorted_pts[idx + 1]
        seg_len = seg_end - seg_start
        if seg_len <= 0:
            continue

        active_keys: List[BreakdownKey] = [
            (svc, db_inst, db_pool)
            for s, e, svc, db_inst, db_pool in clamped
            if s <= seg_start and e >= seg_end
        ]
        if not active_keys:
            continue

        total_dep_ns += seg_len
        share = seg_len / float(len(active_keys))
        for key in active_keys:
            ns_by_key[key] = ns_by_key.get(key, 0.0) + share

    django_ns = max(total_ns - total_dep_ns, 0)
    result: Dict[BreakdownKey, float] = {}
    if django_ns > 0:
        result[("django", "", "")] = django_ns / 1_000_000.0
    for key, dep_ns in ns_by_key.items():
        if dep_ns > 0:
            result[key] = dep_ns / 1_000_000.0
    return result


class RequestBreakdownSpanProcessor(SpanProcessor):
    """Track child CLIENT span intervals per Django SERVER request and emit exact breakdown metrics."""

    def __init__(self, max_active_requests: int = _MAX_ACTIVE_REQUESTS) -> None:
        self._lock = threading.Lock()
        self._active_requests: Dict[int, _RequestBucket] = {}
        self._max_active_requests = max(1, int(max_active_requests))

    def on_start(self, span: Span, parent_context: Optional[Any] = None) -> None:
        try:
            if _get_span_kind(span) != SpanKind.SERVER:
                return
            ctx = _get_span_context(span)
            trace_id = getattr(ctx, "trace_id", 0) if ctx is not None else 0
            span_id = getattr(ctx, "span_id", 0) if ctx is not None else 0
            if not trace_id or not span_id:
                return

            with self._lock:
                if trace_id not in self._active_requests:
                    if len(self._active_requests) >= self._max_active_requests:
                        oldest_key = next(iter(self._active_requests))
                        del self._active_requests[oldest_key]
                    self._active_requests[trace_id] = _RequestBucket(root_span_id=span_id)
        except Exception:
            logger.debug("tp_trace: RequestBreakdownSpanProcessor.on_start failed", exc_info=True)

    def on_end(self, span: ReadableSpan) -> None:
        try:
            ctx = _get_span_context(span)
            trace_id = getattr(ctx, "trace_id", 0) if ctx is not None else 0
            span_id = getattr(ctx, "span_id", 0) if ctx is not None else 0
            if not trace_id or not span_id:
                return

            kind = _get_span_kind(span)
            if kind == SpanKind.CLIENT:
                self._record_client_span(trace_id, span)
            elif kind == SpanKind.SERVER:
                self._finalize_server_span(trace_id, span_id, span)
        except Exception:
            logger.debug("tp_trace: RequestBreakdownSpanProcessor.on_end failed", exc_info=True)

    def _record_client_span(self, trace_id: int, span: ReadableSpan) -> None:
        with self._lock:
            if trace_id not in self._active_requests:
                return

        start_ns = getattr(span, "start_time", None)
        end_ns = getattr(span, "end_time", None)
        if start_ns is None or end_ns is None or end_ns <= start_ns:
            return

        attrs = get_attributes_view(span)
        dep_service = _normalize_dependency_service(attrs)
        if dep_service in _EXCLUDED_DEPENDENCY_SERVICES:
            return

        db_instance = str(attrs.get("db.instance") or "")
        db_pool = str(attrs.get("db.connection.pool") or "")
        interval: SpanInterval = (int(start_ns), int(end_ns), dep_service, db_instance, db_pool)

        with self._lock:
            bucket = self._active_requests.get(trace_id)
            if bucket is not None:
                bucket.intervals.append(interval)

    def _finalize_server_span(self, trace_id: int, span_id: int, span: ReadableSpan) -> None:
        with self._lock:
            bucket = self._active_requests.get(trace_id)
            if bucket is None or bucket.root_span_id != span_id:
                return
            del self._active_requests[trace_id]

        attrs = get_attributes_view(span)
        norm_svc = str(attrs.get("normalized.service") or "")
        norm_op = str(attrs.get("normalized.operation") or "")
        if norm_svc != "django" or norm_op.startswith("django.command."):
            return

        start_ns = getattr(span, "start_time", None)
        end_ns = getattr(span, "end_time", None)
        if start_ns is None or end_ns is None or end_ns <= start_ns:
            return

        breakdown = compute_request_breakdown(int(start_ns), int(end_ns), bucket.intervals)
        if not breakdown:
            return

        from tp_trace.metrics import record_request_time_breakdown

        record_request_time_breakdown(
            breakdown=breakdown,
            project_name=attrs.get("project_name"),
            cluster_name=attrs.get("cluster_name"),
        )

    def shutdown(self) -> None:
        with self._lock:
            self._active_requests.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
