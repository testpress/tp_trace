"""Cache spans — wraps django.core.cache.backends.base.BaseCache operations."""
import logging
from typing import Any, Callable, Optional

from contextvars import ContextVar
from opentelemetry.trace import SpanKind

from tp_trace.config_access import get_config as _get_config, set_config
from tp_trace.safety import attempt, safe_set_attribute
from tp_trace.tracing import suppress_instrumentation, traced_span

logger = logging.getLogger("tp_trace.integrations.django.cache")

_in_cache_span: ContextVar[bool] = ContextVar("in_cache_span", default=False)

CACHE_OPS = ["get", "set", "delete", "get_many", "set_many", "delete_many", "add", "touch", "incr", "decr", "clear"]


_REDIS_BACKEND_CLASSES = {
    "django_redis.cache.RedisCache",
    "django.core.cache.backends.redis.RedisCache",
}


def _is_redis_backend(instance: Any) -> bool:
    if instance is None:
        return False
    cls = getattr(instance, "__class__", type(instance))
    for c in getattr(cls, "__mro__", (cls,)):
        full_name = f"{getattr(c, '__module__', '')}.{getattr(c, '__name__', '')}"
        if full_name in _REDIS_BACKEND_CLASSES:
            return True
        mod = getattr(c, "__module__", "")
        if mod == "django_redis.cache" or mod == "django.core.cache.backends.redis":
            return True
    return False


def _extract_redis_server_info(instance: Any) -> tuple:
    try:
        server = getattr(instance, "_server", None) or getattr(instance, "_servers", None)
        if isinstance(server, (list, tuple)) and server:
            server = server[0]
        if isinstance(server, str) and "://" in server:
            from urllib.parse import urlparse
            parsed = urlparse(server)
            return parsed.hostname, parsed.port
    except Exception:
        pass
    return None, None


def make_traced_cache_op(op_name: str):
    def _traced_op(wrapped: Callable, instance: Any, args: Any, kwargs: Any) -> Any:
        # Pre-application telemetry must never prevent the cache operation from
        # running; every step below degrades independently.
        cfg = attempt(_get_config, default=None, _label="cache_config")
        if cfg and not cfg.cache_enabled:
            return wrapped(*args, **kwargs)

        # Avoid nested duplicate spans if both RedisCache and DefaultClient are wrapped
        if _in_cache_span.get():
            return wrapped(*args, **kwargs)

        backend_cls = attempt(getattr, instance.__class__, "__name__", default="cache", _label="cache_backend_cls")

        is_redis = _is_redis_backend(instance)
        if is_redis:
            span_name = f"🔸 django_redis.cache.{op_name}"
            span_kind = SpanKind.CLIENT
        else:
            span_name = f"django.cache.{op_name}"
            span_kind = SpanKind.INTERNAL

        key = None
        if args:
            key = args[0]
        elif "key" in kwargs:
            key = kwargs["key"]
        elif "keys" in kwargs:
            key = kwargs["keys"]

        span_attrs = {
            "django.cache.operation": op_name,
            "django.cache.backend": backend_cls,
        }
        if is_redis:
            span_attrs["db.system"] = "redis"
            span_attrs["db.operation"] = op_name
            span_attrs["db.name"] = "redis"
            span_attrs["normalized.service"] = "redis"
            span_attrs["normalized.operation"] = f"redis.{op_name}"
            server_addr, server_port = _extract_redis_server_info(instance)
            if server_addr:
                span_attrs["server.address"] = server_addr
            if server_port:
                span_attrs["server.port"] = server_port

            try:
                cmd_parts = [op_name.upper()]
                if op_name in ("get", "delete", "has_key", "incr", "decr"):
                    if key is not None:
                        cmd_parts.append(str(key))
                    if op_name in ("incr", "decr") and len(args) > 1:
                        cmd_parts.append(str(args[1]))
                    elif op_name in ("incr", "decr") and "delta" in kwargs:
                        cmd_parts.append(str(kwargs["delta"]))
                elif op_name in ("set", "add"):
                    if key is not None:
                        cmd_parts.append(str(key))
                    val = args[1] if len(args) > 1 else kwargs.get("value")
                    if val is not None:
                        cmd_parts.append(str(val))
                elif op_name in ("get_many", "delete_many"):
                    if key is not None:
                        if isinstance(key, (list, tuple, set)):
                            cmd_parts.extend(str(k) for k in key)
                        else:
                            cmd_parts.append(str(key))
                elif op_name == "set_many":
                    data = args[0] if args else kwargs.get("data", {})
                    if isinstance(data, dict):
                        for k, v in data.items():
                            cmd_parts.extend([str(k), str(v)])
                    elif key is not None:
                        cmd_parts.append(str(key))
                else:
                    if key is not None:
                        cmd_parts.append(str(key))
                    for a in args[1:]:
                        cmd_parts.append(str(a))

                from tp_trace.integrations.redis.integration import _tp_trace_format_command_args
                stmt = _tp_trace_format_command_args(cmd_parts)
                if stmt:
                    span_attrs["db.statement"] = stmt
                    span_attrs["redis.raw_command"] = stmt
                    span_attrs["redis.command"] = stmt
            except Exception:
                pass

        if key is not None:
            if isinstance(key, (list, tuple, set)):
                span_attrs["django.cache.key"] = ", ".join(str(k) for k in key)
            else:
                span_attrs["django.cache.key"] = str(key)

        token = _in_cache_span.set(True)
        try:
            with traced_span(span_name, kind=span_kind, attributes=span_attrs, tracer_name="tp_trace.django") as span:
                with suppress_instrumentation():
                    res = wrapped(*args, **kwargs)
                if op_name == "get":
                    safe_set_attribute(span, "django.cache.hit", res is not None)
                elif op_name == "get_many":
                    safe_set_attribute(span, "django.cache.hit", bool(res) if res is not None else False)
                return res
        finally:
            _in_cache_span.reset(token)

    return _traced_op
