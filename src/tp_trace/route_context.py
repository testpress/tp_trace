"""Propagate the normalized Django ``http.route`` to child spans.

Problem: the SERVER span (``django.request``) only learns its normalized
``http.route`` *after* the wrapped handler returns, i.e. after all child
spans (PostgreSQL, Redis, outgoing HTTP, S3) have already ended. Child spans
therefore carry no ``http.route`` attribute, and the spanmetrics connector
cannot emit per-endpoint (``http_route``-labelled) series for them. Endpoint
dashboards that divide per-endpoint downstream time by per-endpoint request
time then compare global numerators against per-endpoint denominators and
report percentages far above 100%.

Fix: :func:`traced_get_response` pre-resolves the route from the URLconf
*before* invoking the handler and publishes it via :data:`current_route` /
:data:`current_method` context variables. :func:`traced_span` and
:class:`RouteEnrichingSpanProcessor` copy those values onto every span
started while the request is in flight (including spans created by upstream
OpenTelemetry instrumentors such as botocore, which never pass through
:func:`traced_span`).
"""

import contextvars
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import Span, SpanKind

from tp_trace.safety import safe_update_name

current_route: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("tp_trace.http_route", default=None)
current_method: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("tp_trace.http_method", default=None)


def get_current_route() -> Optional[str]:
    """Return the normalized route of the in-flight Django request, if any."""
    try:
        return current_route.get()
    except LookupError:
        return None


def get_current_method() -> Optional[str]:
    """Return the HTTP method of the in-flight Django request, if any."""
    try:
        return current_method.get()
    except LookupError:
        return None


def set_request_route(route: Optional[str], method: Optional[str] = None) -> Tuple[Any, Any]:
    """Publish route/method for child spans; returns tokens for :func:`reset_request_route`."""
    return current_route.set(route), current_method.set(method)


def reset_request_route(tokens: Optional[Tuple[Any, Any]]) -> None:
    """Reset route/method context vars using tokens from :func:`set_request_route`."""
    if not tokens:
        current_route.set(None)
        current_method.set(None)
        return
    try:
        route_token, method_token = tokens
    except Exception:
        current_route.set(None)
        current_method.set(None)
        return

    try:
        current_route.reset(route_token)
    except Exception:
        current_route.set(None)
    try:
        current_method.reset(method_token)
    except Exception:
        current_method.set(None)


current_view: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("tp_trace.django_view", default=None)


def get_current_view() -> Optional[str]:
    """Return the active Django view name of the in-flight request, if any."""
    try:
        return current_view.get()
    except LookupError:
        return None


def set_request_view(view: Optional[str]) -> Optional[contextvars.Token]:
    """Publish the active Django view name; returns token for reset_request_view."""
    try:
        return current_view.set(view)
    except Exception:
        return None


def reset_request_view(token: Optional[contextvars.Token] = None) -> None:
    """Reset the current view context variable using the provided token."""
    if token is not None:
        try:
            current_view.reset(token)
            return
        except Exception:
            pass
    current_view.set(None)


current_tags: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("tp_trace.tags", default=None)


def get_current_tags() -> dict:
    """Return the dynamic tags published on the in-flight request, if any."""
    try:
        val = current_tags.get()
        return dict(val) if val is not None else {}
    except LookupError:
        return {}


def set_request_tag(key: str, value: Any) -> Optional[contextvars.Token]:
    """Set a dynamic tag on the in-flight request and propagate to all child spans."""
    try:
        val = current_tags.get()
        existing = dict(val) if val is not None else {}
        existing[key] = value
        return current_tags.set(existing)
    except Exception:
        return None


def reset_request_tags(token: Optional[contextvars.Token] = None) -> None:
    """Reset dynamic request tags."""
    try:
        if token is not None:
            current_tags.reset(token)
        else:
            current_tags.set(None)
    except Exception:
        pass


@dataclass
class QueryPatternStats:
    fingerprint: str
    count: int
    total_duration_ms: float


@dataclass
class RequestDBQueryStats:
    total_queries: int = 0
    patterns: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    candidate_count: int = 0
    dominant_query: Optional[str] = None
    dominant_count: int = 0
    dominant_duration_ms: float = 0.0
    tracker_overflowed: bool = False


@dataclass
class RequestDBTracker:
    total_queries: int = 0
    fingerprints: Counter = field(default_factory=Counter)
    fingerprint_duration_ms: Dict[str, float] = field(default_factory=dict)
    dropped_fingerprints: int = 0
    max_fingerprints: int = 256


current_db_tracker: contextvars.ContextVar[Optional[RequestDBTracker]] = contextvars.ContextVar(
    "tp_trace.db_tracker", default=None
)


def init_request_db_tracker(max_fingerprints: int = 256) -> Optional[contextvars.Token]:
    """Initialize request-scoped database query tracker; returns token for reset."""
    try:
        limit = max_fingerprints if isinstance(max_fingerprints, int) and max_fingerprints >= 1 else 256
        tracker = RequestDBTracker(max_fingerprints=limit)
        return current_db_tracker.set(tracker)
    except Exception:
        return None


def record_request_db_query(metric_sql: str, duration_ms: float = 0.0) -> None:
    """Record one successful database execute call for the active request.

    Non-raising fail-safe: any failure is absorbed without interrupting application execution.
    """
    try:
        tracker = current_db_tracker.get()
        if tracker is None or not metric_sql:
            return
        duration = max(0.0, float(duration_ms))
        tracker.total_queries += 1
        if metric_sql in tracker.fingerprints:
            tracker.fingerprints[metric_sql] += 1
            tracker.fingerprint_duration_ms[metric_sql] += duration
        else:
            if len(tracker.fingerprints) < tracker.max_fingerprints:
                tracker.fingerprints[metric_sql] = 1
                tracker.fingerprint_duration_ms[metric_sql] = duration
            else:
                tracker.dropped_fingerprints += 1
    except Exception:
        pass


def get_request_db_query_stats(threshold: int = 15) -> RequestDBQueryStats:
    """Retrieve statistics across ALL query fingerprints from the current request DB tracker."""
    try:
        tracker = current_db_tracker.get()
        if tracker is None or not tracker.fingerprints:
            return RequestDBQueryStats()

        patterns = []
        for fp, count in tracker.fingerprints.most_common():
            dur = float(tracker.fingerprint_duration_ms.get(fp, 0.0))
            patterns.append(QueryPatternStats(fingerprint=fp, count=count, total_duration_ms=dur))

        candidates = [p for p in patterns if p.count >= threshold]

        # When candidates exist, report an actual candidate rather than an
        # unrelated frequent query. Duration breaks count ties deterministically.
        representative = max(
            candidates or patterns,
            key=lambda pattern: (pattern.count, pattern.total_duration_ms),
        )
        dominant_query = representative.fingerprint
        dominant_count = representative.count
        dominant_duration_ms = representative.total_duration_ms

        return RequestDBQueryStats(
            total_queries=tracker.total_queries,
            patterns=patterns,
            candidates=candidates,
            candidate_count=len(candidates),
            dominant_query=dominant_query,
            dominant_count=dominant_count,
            dominant_duration_ms=dominant_duration_ms,
            tracker_overflowed=tracker.dropped_fingerprints > 0,
        )
    except Exception:
        return RequestDBQueryStats()


def reset_request_db_tracker(token: Optional[contextvars.Token] = None) -> None:
    """Reset the request-scoped database query tracker."""
    try:
        if token is not None:
            current_db_tracker.reset(token)
        else:
            current_db_tracker.set(None)
    except Exception:
        pass


class RouteEnrichingSpanProcessor(SpanProcessor):
    """Copy the in-flight request route and global tags onto spans.

    Covers spans created by upstream OpenTelemetry instrumentors (botocore,
    requests) that never pass through :func:`tp_trace.tracing.traced_span`.
    Runs on span start so attributes are present no matter which export
    processor ends the span.
    """

    def __init__(self, static_attributes: Optional[dict] = None) -> None:
        self.static_attributes = dict(static_attributes or {})

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        try:
            for k, v in self.static_attributes.items():
                if v is not None:
                    span.set_attribute(k, v)

            # Propagate in-flight dynamic tags (e.g. institute, institute.subdomain) to all child spans
            for k, v in get_current_tags().items():
                if v is not None:
                    span.set_attribute(k, v)

            # Check for owned Celery span creation
            try:
                from tp_trace.integrations.celery.context import get_celery_creation_meta

                celery_meta = get_celery_creation_meta()
            except Exception:
                celery_meta = None

            if celery_meta is not None:
                scope = getattr(span, "instrumentation_scope", None) or getattr(span, "instrumentation_info", None)
                scope_name = getattr(scope, "name", None)
                span_kind = getattr(span, "kind", None)
                if span_kind is None and hasattr(span, "_kind"):
                    span_kind = getattr(span, "_kind", None)
                expected_kind = SpanKind.PRODUCER if celery_meta.action == "apply_async" else SpanKind.CONSUMER
                if scope_name == "opentelemetry.instrumentation.celery" and span_kind == expected_kind:
                    span.set_attribute("tp_trace.celery.owned", True)
                    span.set_attribute("normalized.service", "celery")
                    span.set_attribute("normalized.operation", celery_meta.bounded_operation)
                    span.set_attribute("error", False)
                    safe_update_name(span, celery_meta.span_name)
                    view = get_current_view()
                    if view and celery_meta.action == "apply_async":
                        span.set_attribute("celery.trigger.view", view)
                    return

            existing = getattr(span, "attributes", None) or {}
            span_kind = getattr(span, "kind", None)
            if span_kind is None and hasattr(span, "_kind"):
                span_kind = getattr(span, "_kind", None)
            is_client_or_internal = span_kind in (SpanKind.CLIENT, SpanKind.INTERNAL)
            is_db_or_cache = bool(existing.get("db.system") or existing.get("db.name"))
            route = get_current_route()
            if route and route != "__unmatched__":
                if not existing.get("django.route"):
                    span.set_attribute("django.route", route)
                if not is_client_or_internal and not is_db_or_cache and not existing.get("http.route"):
                    span.set_attribute("http.route", route)
            method = get_current_method()
            if method and not is_client_or_internal and not is_db_or_cache:
                if not existing.get("http.request.method"):
                    span.set_attribute("http.request.method", method)
                if not existing.get("http.method"):
                    span.set_attribute("http.method", method)
        except Exception:
            pass

    def on_end(self, span: ReadableSpan) -> None:
        try:
            # ReadableSpan does not implement set_attribute and is_recording() is False after end().
            # Mutate the underlying _attributes dict directly so the fallback values survive into export.
            attrs = getattr(span, "_attributes", None)
            if attrs is None:
                attrs = getattr(span, "attributes", None) or {}
            target_dict = getattr(attrs, "_dict", None)
            if target_dict is None and hasattr(attrs, "__setitem__"):
                target_dict = attrs

            if target_dict is not None:
                # Check for owned Celery span: defensive normalization and suppression of HTTP attributes
                is_owned_celery = bool(target_dict.get("tp_trace.celery.owned") or attrs.get("tp_trace.celery.owned"))
                if is_owned_celery:
                    for bad_key in (
                        "http.method",
                        "http.request.method",
                        "http.route",
                        "http.route_template",
                        "django.route",
                    ):
                        if bad_key in target_dict:
                            try:
                                del target_dict[bad_key]
                            except Exception:
                                pass
                    if not target_dict.get("normalized.service"):
                        target_dict["normalized.service"] = "celery"
                    if not target_dict.get("normalized.operation"):
                        action = "apply_async" if getattr(span, "kind", None) == SpanKind.PRODUCER else "run"
                        target_dict["normalized.operation"] = f"celery.{action}.unknown"
                    return

                # Remove accidental http.method / http.route from database/cache spans
                if (
                    target_dict.get("db.system")
                    or attrs.get("db.system")
                    or target_dict.get("normalized.service") in ("redis", "postgres", "pgbouncer")
                ):
                    for bad_key in ("http.method", "http.request.method", "http.route"):
                        if bad_key in target_dict:
                            try:
                                del target_dict[bad_key]
                            except Exception:
                                pass

                if not attrs.get("normalized.service"):
                    svc = (
                        attrs.get("peer.service")
                        or attrs.get("db.system")
                        or attrs.get("rpc.system")
                        or attrs.get("component")
                    )
                    if svc == "postgresql":
                        svc = "postgres"
                    elif svc == "aws-api":
                        svc = "aws-s3"
                    target_dict["normalized.service"] = str(svc or "django")

                svc = target_dict.get("normalized.service") or attrs.get("normalized.service")

                if not attrs.get("normalized.operation"):
                    op = None
                    if svc == "redis" or attrs.get("db.system") == "redis":
                        if "db.redis.pipeline_length" in attrs:
                            op = "redis.pipeline"
                        elif attrs.get("db.operation"):
                            op = f"redis.{str(attrs.get('db.operation')).lower()}"
                        elif getattr(span, "name", ""):
                            raw = (
                                getattr(span, "name", "")
                                .replace("🔸", "")
                                .replace("REDIS:", "")
                                .replace("redis:", "")
                                .strip()
                                .lower()
                            )
                            op = f"redis.{raw}" if raw else "redis.command"
                        else:
                            op = "redis.command"
                    elif svc in ("postgres", "pgbouncer") or attrs.get("db.system") in ("postgres", "postgresql"):
                        op = attrs.get("db.statement") or "postgres.query"
                    elif svc == "django":
                        route_val = attrs.get("http.route_template") or attrs.get("http.route")
                        if attrs.get("http.method") and route_val:
                            op = f"{attrs.get('http.method')} {route_val}"
                        else:
                            op = getattr(span, "name", "")
                    elif attrs.get("rpc.service") and attrs.get("rpc.method"):
                        op = f"{attrs.get('rpc.service')}.{attrs.get('rpc.method')}"
                    elif attrs.get("db.statement"):
                        op = attrs.get("db.statement")
                    elif attrs.get("http.method") and (attrs.get("http.route_template") or attrs.get("http.route")):
                        route_val = attrs.get("http.route_template") or attrs.get("http.route")
                        op = f"{attrs.get('http.method')} {route_val}"
                    else:
                        op = getattr(span, "name", "")
                    if op:
                        target_dict["normalized.operation"] = str(op)
        except Exception:
            pass

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
