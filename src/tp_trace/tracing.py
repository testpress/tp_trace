"""Shared tracing primitives that remove boilerplate from integration wrappers."""

import contextlib
import contextvars
import logging
import sys
from typing import Any, Dict, FrozenSet, Iterator, Optional, Tuple

from opentelemetry.trace import (
    INVALID_SPAN,
    Context,
    Span,
    SpanKind,
    StatusCode,
    get_tracer,
)

from tp_trace.route_context import get_current_route
from tp_trace.safety import (
    attempt,
    find_exception,
    safe_record_exception,
    safe_set_attribute,
    safe_set_status,
)

logger = logging.getLogger("tp_trace.tracing")

_active_reentrant_guards: contextvars.ContextVar[FrozenSet[Tuple[int, str]]] = contextvars.ContextVar(
    "_tp_trace_active_guards", default=frozenset()
)


def _with_request_route(
    attributes: Optional[Dict[str, Any]],
    kind: Optional[SpanKind] = None,
) -> Optional[Dict[str, Any]]:
    """Merge the in-flight Django request route into span attributes.

    Lets per-endpoint spanmetrics series (``http_route`` label) be emitted
    for child spans even though the SERVER span only learns its normalized
    route after the handler returns. Never overwrites explicit attributes.
    """
    route = get_current_route()
    if not route or route == "__unmatched__":
        return attributes
    attrs = dict(attributes) if attributes else {}
    if (
        kind in (SpanKind.CLIENT, SpanKind.INTERNAL)
        or "db.system" in attrs
        or "db.name" in attrs
        or attrs.get("normalized.service") in ("redis", "postgres", "pgbouncer")
    ):
        return attributes
    if "http.route" not in attrs:
        attrs["http.route"] = route
    return attrs


@contextlib.contextmanager
def _span_lifecycle(
    name: str,
    kind: SpanKind,
    attributes: Optional[Dict[str, Any]],
    tracer_name: str,
    context: Optional[Context],
) -> Iterator[Any]:
    """Own the OpenTelemetry span lifecycle, degrading to a no-op span on failure.

    This is the only place that touches the context-manager protocol by hand.
    It is hand-driven because setup failure and body failure must stay
    distinguishable: a span that cannot be *created* is a telemetry fault and
    degrades silently, whereas an exception from the *body* is the
    application's own and must propagate untouched.
    """
    try:
        span_cm = get_tracer(tracer_name).start_as_current_span(
            name,
            kind=kind,
            attributes=_with_request_route(attributes, kind=kind),
            context=context,
            record_exception=False,
            set_status_on_exception=False,
        )
    except Exception:
        logger.debug("tp_trace: could not create span %r; running untraced", name, exc_info=True)
        yield INVALID_SPAN
        return

    try:
        span = span_cm.__enter__()
    except Exception:
        logger.debug("tp_trace: span %r failed to start; running untraced", name, exc_info=True)
        yield INVALID_SPAN
        return

    try:
        yield span
    finally:
        try:
            span_cm.__exit__(*sys.exc_info())
        except Exception:
            logger.debug("tp_trace: span %r failed to end cleanly", name, exc_info=True)


@contextlib.contextmanager
def traced_span(
    name: str,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Optional[Dict[str, Any]] = None,
    tracer_name: str = "tp_trace",
    context: Optional[Context] = None,
) -> Iterator[Span]:
    """
    Start a span and collapse the standard try/success/error/raise wrapper.

    On success the span is marked OK unless the caller already set a status.
    On failure the exception is recorded, error attributes are set, the span
    is marked ERROR, and the exception is re-raised unchanged.

    Yields the active Span so callers can enrich it after the wrapped call.

    Telemetry failures are absorbed here and in :mod:`tp_trace.safety`, never
    by re-running the caller. That is what makes the invariant in
    :mod:`tp_trace.safety` hold: an exception escaping the ``with`` body is
    always the application's own.
    """
    with _span_lifecycle(name, kind, attributes, tracer_name, context) as span:
        try:
            yield span
            if span.is_recording() and hasattr(span, "status") and span.status.status_code == StatusCode.UNSET:
                span.set_status(StatusCode.OK)
        except BaseException as exc:
            # This block sits between the application's exception and the
            # caller, so every statement in it is guarded: a telemetry fault
            # here would otherwise *replace* the app's exception with its own,
            # corrupting error handling on the one path where it matters most.
            real_exc = find_exception(exc)
            if attempt(span.is_recording, default=False, _label="is_recording"):
                safe_record_exception(span, real_exc)
                safe_set_attribute(span, "error", True)
                safe_set_attribute(span, "error.type", real_exc.__class__.__name__)
                safe_set_status(span, StatusCode.ERROR, description=str(real_exc))
            raise


@contextlib.contextmanager
def reentrant_guard(instance: Any, attr: str) -> Iterator[bool]:
    """
    Guard a traced wrapper against recursive invocation on the same instance / context.

    Thread-safe and async-safe via contextvars, preventing cross-thread race
    conditions on shared database connections or cursors.

    Yields True for the outermost call (trace) and False when the guard is
    already held (call through without tracing).
    """
    guard_key = (id(instance), attr)
    active = _active_reentrant_guards.get()
    if guard_key in active:
        yield False
        return

    token = _active_reentrant_guards.set(active | {guard_key})
    try:
        yield True
    finally:
        try:
            _active_reentrant_guards.reset(token)
        except Exception:
            pass


@contextlib.contextmanager
def suppress_instrumentation() -> Iterator[None]:
    """Context manager to suppress downstream OpenTelemetry driver instrumentations.

    Used inside high-level wrappers (like django_redis cache and Django DB execute wrappers)
    to prevent lower-level raw driver instrumentors (redis-py, psycopg2) from emitting
    redundant nested duplicate child spans.
    """
    keys = [
        "suppress_instrumentation",
        "suppress_instrumentation-b7e0b9cc-f3b6-4ca5-8d52-37b2d54b7203",
        "tp_trace_suppress_db",
        "tp_trace_suppress_db",
    ]
    try:
        from opentelemetry.instrumentation.utils import (
            _SUPPRESS_INSTRUMENTATION_KEY,
            _SUPPRESS_INSTRUMENTATION_KEY_PLAIN,
        )

        keys.extend([_SUPPRESS_INSTRUMENTATION_KEY, _SUPPRESS_INSTRUMENTATION_KEY_PLAIN])
    except Exception:
        pass

    from opentelemetry import context as otel_context

    ctx = otel_context.get_current()
    for k in set(keys):
        ctx = otel_context.set_value(k, True, context=ctx)
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        try:
            otel_context.detach(token)
        except Exception:
            pass
