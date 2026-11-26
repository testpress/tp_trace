"""Exact per-request time breakdown tracking for Django HTTP requests."""

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import SpanKind, get_current_span

from tp_trace.compat.attributes import get_attributes_view

logger = logging.getLogger("tp_trace.breakdown")

BreakdownKey = Tuple[str, str, str]  # (normalized_service, db_instance, db_connection_pool)
SpanInterval = Tuple[int, int, str, str, str]  # (start_ns, end_ns, service, db_instance, db_pool)

_EXCLUDED_DEPENDENCY_SERVICES = frozenset({"", "django", "celery", "pgbouncer"})
_MAX_ACTIVE_REQUESTS = 10000
_MAX_INTERVALS_PER_REQUEST = 2000
_MAX_SPAN_PARENTS = 50000
_MAX_ANCESTOR_HOPS = 64


class _RequestBucket:
    __slots__ = ("trace_id", "intervals")

    def __init__(self, trace_id: int) -> None:
        self.trace_id = trace_id
        self.intervals: List[SpanInterval] = []


def _get_span_context(span: Any) -> Any:
    ctx = getattr(span, "context", None)
    if ctx is None and hasattr(span, "get_span_context"):
        try:
            ctx = span.get_span_context()
        except Exception:
            ctx = None
    return ctx


def _get_parent_span_id(span: Any, parent_context: Optional[Any] = None) -> int:
    parent = getattr(span, "parent", None)
    if parent is not None:
        pid = getattr(parent, "span_id", 0)
        if pid:
            return int(pid)
    if parent_context is not None:
        try:
            p_ctx = get_current_span(parent_context).get_span_context()
            pid = getattr(p_ctx, "span_id", 0)
            if pid:
                return int(pid)
        except Exception:
            pass
    return 0


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

    Uses an O(N log N) event sweep over clamped child CLIENT intervals so
    concurrent or overlapping calls never exceed 100% of the request duration.
    Overlapped segments are split equally across the active child intervals,
    and the remaining wall-clock time is attributed to ``("django", "", "")``.
    """
    total_ns = request_end_ns - request_start_ns
    if total_ns <= 0:
        return {}

    events: List[Tuple[int, int, BreakdownKey]] = []
    for start_ns, end_ns, svc, db_inst, db_pool in intervals:
        s = max(start_ns, request_start_ns)
        e = min(end_ns, request_end_ns)
        if e > s:
            key: BreakdownKey = (svc, db_inst, db_pool)
            events.append((s, 1, key))
            events.append((e, -1, key))

    if not events:
        return {("django", "", ""): total_ns / 1_000_000.0}

    events.sort(key=lambda ev: ev[0])

    active_counts: Dict[BreakdownKey, int] = {}
    total_active = 0
    ns_by_key: Dict[BreakdownKey, float] = {}
    total_dep_ns = 0

    n_events = len(events)
    idx = 0
    prev_t: Optional[int] = None

    while idx < n_events:
        curr_t = events[idx][0]
        if prev_t is not None and curr_t > prev_t and total_active > 0:
            seg_len = curr_t - prev_t
            total_dep_ns += seg_len
            share = seg_len / float(total_active)
            for key, cnt in active_counts.items():
                ns_by_key[key] = ns_by_key.get(key, 0.0) + share * cnt

        while idx < n_events and events[idx][0] == curr_t:
            _, delta, key = events[idx]
            new_cnt = active_counts.get(key, 0) + delta
            if new_cnt > 0:
                active_counts[key] = new_cnt
            else:
                active_counts.pop(key, None)
            total_active += delta
            idx += 1

        prev_t = curr_t

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

    def __init__(
        self,
        max_active_requests: int = _MAX_ACTIVE_REQUESTS,
        max_intervals_per_request: int = _MAX_INTERVALS_PER_REQUEST,
    ) -> None:
        self._lock = threading.Lock()
        self._server_buckets: Dict[int, _RequestBucket] = {}
        self._active_trace_counts: Dict[int, int] = {}
        self._span_parents: Dict[int, int] = {}
        self._max_active_requests = max(1, int(max_active_requests))
        self._max_intervals_per_request = max(1, int(max_intervals_per_request))

    def _evict_oldest_bucket_locked(self) -> None:
        oldest_sid = next(iter(self._server_buckets))
        old_bucket = self._server_buckets.pop(oldest_sid)
        tid = old_bucket.trace_id
        rem = self._active_trace_counts.get(tid, 0) - 1
        if rem > 0:
            self._active_trace_counts[tid] = rem
        else:
            self._active_trace_counts.pop(tid, None)

    def on_start(self, span: Span, parent_context: Optional[Any] = None) -> None:
        try:
            ctx = _get_span_context(span)
            trace_id = getattr(ctx, "trace_id", 0) if ctx is not None else 0
            span_id = getattr(ctx, "span_id", 0) if ctx is not None else 0
            if not trace_id or not span_id:
                return

            kind = _get_span_kind(span)
            parent_span_id = _get_parent_span_id(span, parent_context)

            with self._lock:
                if kind == SpanKind.SERVER:
                    if span_id not in self._server_buckets:
                        if len(self._server_buckets) >= self._max_active_requests:
                            self._evict_oldest_bucket_locked()
                        self._server_buckets[span_id] = _RequestBucket(trace_id=trace_id)
                        self._active_trace_counts[trace_id] = self._active_trace_counts.get(trace_id, 0) + 1
                    if parent_span_id:
                        if len(self._span_parents) >= _MAX_SPAN_PARENTS:
                            self._span_parents.pop(next(iter(self._span_parents)))
                        self._span_parents[span_id] = parent_span_id
                elif trace_id in self._active_trace_counts and parent_span_id:
                    if len(self._span_parents) >= _MAX_SPAN_PARENTS:
                        self._span_parents.pop(next(iter(self._span_parents)))
                    self._span_parents[span_id] = parent_span_id
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
                self._record_client_span(trace_id, span_id, span)
            elif kind == SpanKind.SERVER:
                self._finalize_server_span(trace_id, span_id, span)
        except Exception:
            logger.debug("tp_trace: RequestBreakdownSpanProcessor.on_end failed", exc_info=True)

    def _find_nearest_server_bucket_locked(self, span_id: int, direct_parent_id: int) -> Optional[_RequestBucket]:
        curr = direct_parent_id or self._span_parents.get(span_id, 0)
        for _ in range(_MAX_ANCESTOR_HOPS):
            if not curr:
                return None
            bucket = self._server_buckets.get(curr)
            if bucket is not None:
                return bucket
            curr = self._span_parents.get(curr, 0)
        return None

    def _record_client_span(self, trace_id: int, span_id: int, span: ReadableSpan) -> None:
        direct_parent_id = _get_parent_span_id(span)
        with self._lock:
            if trace_id not in self._active_trace_counts:
                return
            if self._find_nearest_server_bucket_locked(span_id, direct_parent_id) is None:
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
            bucket = self._find_nearest_server_bucket_locked(span_id, direct_parent_id)
            if bucket is not None and len(bucket.intervals) < self._max_intervals_per_request:
                bucket.intervals.append(interval)

    def _finalize_server_span(self, trace_id: int, span_id: int, span: ReadableSpan) -> None:
        with self._lock:
            bucket = self._server_buckets.pop(span_id, None)
            if bucket is None:
                return
            rem = self._active_trace_counts.get(trace_id, 0) - 1
            if rem > 0:
                self._active_trace_counts[trace_id] = rem
            else:
                self._active_trace_counts.pop(trace_id, None)

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
            self._server_buckets.clear()
            self._active_trace_counts.clear()
            self._span_parents.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
