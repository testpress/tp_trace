"""Shared span attribute normalization for tp_trace.

Consolidates normalization that was duplicated between RouteEnrichingSpanProcessor
and SafeSpanExporter. Uses public API when possible, falls back to compat shim
only for ended spans with third-party instrumentors.
"""

from typing import Any, Dict, Optional

from opentelemetry.trace import SpanKind

from tp_trace.compat.attributes import (
    delete_attribute_safe,
    get_attributes_view,
    get_mutable_attributes,
    set_attribute_safe,
)


def _set_attr(span: Any, key: str, value: Any) -> None:
    try:
        set_attribute_safe(span, key, value)
    except Exception:
        pass


def _infer_operation(span: Any, attrs: Dict[str, Any], svc: Optional[str]) -> Optional[str]:
    if svc == "redis" or attrs.get("db.system") == "redis":
        if attrs.get("db.redis.pipeline_length") is not None and attrs.get("db.redis.pipeline_length") != 0:
            return "redis.pipeline"
        if attrs.get("db.operation"):
            return f"redis.{str(attrs.get('db.operation')).lower()}"
        name = getattr(span, "name", "") or ""
        raw = str(name).replace("🔸", "").replace("REDIS:", "").replace("redis:", "").strip().lower()
        return f"redis.{raw}" if raw else "redis.command"
    elif svc in ("postgres", "pgbouncer") or attrs.get("db.system") in ("postgres", "postgresql"):
        return attrs.get("db.statement") or "postgres.query"
    elif svc == "django":
        route_val = attrs.get("http.route_template") or attrs.get("http.route")
        method = attrs.get("http.method")
        if method and route_val:
            return f"{method} {route_val}"
        return getattr(span, "name", "")
    elif attrs.get("rpc.service") and attrs.get("rpc.method"):
        return f"{attrs.get('rpc.service')}.{attrs.get('rpc.method')}"
    elif attrs.get("db.statement"):
        return attrs.get("db.statement")
    elif attrs.get("http.method") and (attrs.get("http.route_template") or attrs.get("http.route")):
        route_val = attrs.get("http.route_template") or attrs.get("http.route")
        return f"{attrs.get('http.method')} {route_val}"
    else:
        return getattr(span, "name", "")


def preserve_sampled_attribute(span: Any) -> None:
    """Protect tp_trace.sampled through export against BoundedAttributes eviction."""
    try:
        ctx = getattr(span, "context", None)
        if ctx is None and hasattr(span, "get_span_context"):
            try:
                ctx = span.get_span_context()
            except Exception:
                ctx = None
        trace_flags = getattr(ctx, "trace_flags", None)
        if trace_flags is not None:
            sampled_decision = bool(getattr(trace_flags, "sampled", False))
            target = get_mutable_attributes(span)
            if target is not None:
                target["tp_trace.sampled"] = sampled_decision
            else:
                _set_attr(span, "tp_trace.sampled", sampled_decision)
    except Exception:
        pass


def normalize_span_attributes(span: Any) -> None:
    """Normalize span attributes for tp_trace RED metrics/export."""
    try:
        preserve_sampled_attribute(span)

        target = get_mutable_attributes(span)
        attrs = get_attributes_view(span)

        # Owned Celery
        is_owned_celery = bool(attrs.get("tp_trace.celery.owned")) or bool(
            target and target.get("tp_trace.celery.owned")
        )
        if is_owned_celery:
            for bad_key in (
                "http.method",
                "http.request.method",
                "http.route",
                "http.route_template",
                "django.route",
            ):
                delete_attribute_safe(span, bad_key)
            if not attrs.get("normalized.service"):
                _set_attr(span, "normalized.service", "celery")
            if not attrs.get("normalized.operation"):
                kind = getattr(span, "kind", None)
                action = "apply_async" if kind == SpanKind.PRODUCER else "run"
                _set_attr(span, "normalized.operation", f"celery.{action}.unknown")
            return

        # Strip http attrs from DB/cache
        svc_cand = attrs.get("normalized.service") or attrs.get("db.system") or ""
        if attrs.get("db.system") or svc_cand in ("redis", "postgres", "pgbouncer"):
            for bad_key in ("http.method", "http.request.method", "http.route"):
                delete_attribute_safe(span, bad_key)

        # Normalize service
        if not attrs.get("normalized.service"):
            svc = (
                attrs.get("peer.service") or attrs.get("db.system") or attrs.get("rpc.system") or attrs.get("component")
            )
            if svc == "postgresql":
                svc = "postgres"
            elif svc == "aws-api":
                svc = "aws-s3"
            _set_attr(span, "normalized.service", str(svc or "django"))

        svc = attrs.get("normalized.service") or (target.get("normalized.service") if target else None)

        # Normalize operation
        if not attrs.get("normalized.operation"):
            op = _infer_operation(span, attrs, svc)
            if op:
                _set_attr(span, "normalized.operation", str(op))
    except Exception:
        pass
