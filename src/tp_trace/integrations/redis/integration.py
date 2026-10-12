"""RedisIntegration — leverages official OpenTelemetry Redis instrumentor."""

import importlib
import logging
from typing import Any, Callable, Optional

from tp_trace.config import SDKConfig
from tp_trace.config_access import get_config as _get_config
from tp_trace.config_access import set_config
from tp_trace.integrations.base import BaseIntegration
from tp_trace.integrations.django.cache import _in_cache_span

logger = logging.getLogger("tp_trace.integrations.redis")

_DEFAULT_CMD_MAX_LENGTH = 1000

# Original OTel `_format_command_args` captured at install time so
# uninstrument() can restore the stock "COMMAND ? ?" reduction.
_otel_original_format_command_args: Optional[Callable[[Any], str]] = None


def _mask_endpoint_enabled() -> bool:
    cfg = _get_config()
    return bool(getattr(cfg, "redis_mask_endpoint", False)) if cfg else False


def _mask_command_enabled() -> bool:
    cfg = _get_config()
    return bool(getattr(cfg, "redis_mask_command", False)) if cfg else False


def _cmd_max_length() -> int:
    cfg = _get_config()
    if cfg is None:
        return _DEFAULT_CMD_MAX_LENGTH
    try:
        return int(getattr(cfg, "redis_cmd_max_length", _DEFAULT_CMD_MAX_LENGTH))
    except Exception:
        return _DEFAULT_CMD_MAX_LENGTH


def _stringify_cmd_arg(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return repr(value)
    return str(value)


def _tp_trace_format_command_args(args: Any) -> str:
    """db.statement formatter replacing the OTel instrumentor's arg reducer.

    Exposes the full command text (keys and values) so spans read like
    dd-trace's ``redis.raw_command``. Reads the mask toggle and max length
    from SDKConfig at call time; masking reproduces OTel's "COMMAND ? ?"
    reduction. Truncation appends "..." beyond ``redis_cmd_max_length``.
    """
    try:
        if not args:
            return ""
        if _mask_command_enabled():
            parts = [str(args[0])] + ["?"] * (len(args) - 1)
        else:
            parts = [_stringify_cmd_arg(a) for a in args]
        out_str = " ".join(parts)
        max_len = _cmd_max_length()
        if max_len > 0 and len(out_str) > max_len:
            out_str = out_str[: max(0, max_len - 3)] + "..."
        return out_str
    except Exception:
        # Never break the instrumented command over span formatting;
        # degrade to the bare command name instead of blanking the statement.
        try:
            return str(args[0])
        except Exception:
            return ""


def _install_command_formatter() -> None:
    """Swap the instrumentor's `_format_command_args` for the full-command one.

    The instrumentor binds `_format_command_args` into both its package
    namespace (execute + async paths) and its util namespace (pipeline path),
    so both module globals must be replaced for complete coverage.
    """
    global _otel_original_format_command_args
    try:
        otel_redis_pkg = importlib.import_module("opentelemetry.instrumentation.redis")
        otel_redis_util = importlib.import_module("opentelemetry.instrumentation.redis.util")
        if _otel_original_format_command_args is None:
            _otel_original_format_command_args = getattr(
                otel_redis_util, "_format_command_args", None
            )
        otel_redis_pkg._format_command_args = _tp_trace_format_command_args
        otel_redis_util._format_command_args = _tp_trace_format_command_args
    except Exception as exc:
        logger.debug("Failed to install full redis command formatter: %s", exc)


def _restore_command_formatter() -> None:
    """Restore the stock OTel `_format_command_args` in both module namespaces."""
    global _otel_original_format_command_args
    original = _otel_original_format_command_args
    if original is None:
        return
    try:
        otel_redis_pkg = importlib.import_module("opentelemetry.instrumentation.redis")
        otel_redis_util = importlib.import_module("opentelemetry.instrumentation.redis.util")
        otel_redis_pkg._format_command_args = original
        otel_redis_util._format_command_args = original
    except Exception as exc:
        logger.debug("Failed to restore redis command formatter: %s", exc)
    _otel_original_format_command_args = None


def _tp_trace_redis_request_hook(span: Any, instance: Any, args: Any, kwargs: Any) -> None:
    try:
        if span is not None and getattr(span, "is_recording", lambda: True)():
            name = getattr(span, "name", "")
            raw_op = name.replace("🔸", "").replace("REDIS:", "").replace("redis:", "").strip() if name else "command"
            display_name = f"🔸 {raw_op.upper()}" if raw_op else "🔸 COMMAND"
            if hasattr(span, "update_name"):
                span.update_name(display_name)
            if hasattr(span, "set_attribute"):
                span.set_attribute("db.system", "redis")
                span.set_attribute("db.name", "redis")
                span.set_attribute("normalized.service", "redis")
                span.set_attribute("normalized.operation", f"redis.{raw_op.lower()}" if raw_op else "redis.command")
                if args:
                    try:
                        cmd_str = _tp_trace_format_command_args(args)
                        if cmd_str:
                            span.set_attribute("db.statement", cmd_str)
                    except Exception:
                        pass
                # Default exposes the real connection endpoint (team owns the
                # infra); TP_TRACE_REDIS_MASK_ENDPOINT restores the old masking.
                if _mask_endpoint_enabled():
                    span.set_attribute("net.peer.name", "?")
                    span.set_attribute("net.peer.port", "?")
    except Exception as exc:
        logger.debug("redis hook/guard error", exc_info=True)


def _tp_trace_redis_response_hook(span: Any, instance: Any, response: Any) -> None:
    try:
        if span is not None and getattr(span, "is_recording", lambda: True)():
            attrs = getattr(span, "attributes", None) or {}
            if not attrs.get("normalized.service") and hasattr(span, "set_attribute"):
                span.set_attribute("normalized.service", "redis")
                span.set_attribute("db.system", "redis")
                span.set_attribute("db.name", "redis")
            if not attrs.get("normalized.operation") and hasattr(span, "set_attribute"):
                if "db.redis.pipeline_length" in attrs or "pipeline" in getattr(span, "name", "").lower():
                    span.set_attribute("normalized.operation", "redis.pipeline")
                    if hasattr(span, "update_name"):
                        span.update_name("🔸 PIPELINE")
                elif attrs.get("db.operation"):
                    span.set_attribute("normalized.operation", f"redis.{attrs.get('db.operation').lower()}")
                else:
                    name = getattr(span, "name", "")
                    raw_op = name.replace("🔸", "").replace("REDIS:", "").replace("redis:", "").strip().lower() if name else "command"
                    span.set_attribute("normalized.operation", f"redis.{raw_op}")
    except Exception as exc:
        logger.debug("redis response hook error: %s", exc)


# Backwards compatibility aliases
_tp_dog_redis_request_hook = _tp_trace_redis_request_hook
_tp_dog_redis_response_hook = _tp_trace_redis_response_hook


def _redis_suppress_guard(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    try:
        from opentelemetry.instrumentation.utils import is_instrumentation_enabled

        if not is_instrumentation_enabled() or _in_cache_span.get():
            orig = getattr(wrapped, "__wrapped__", wrapped)
            while hasattr(orig, "__wrapped__"):
                orig = orig.__wrapped__
            return orig(*args, **kwargs)
    except Exception as exc:
        logger.debug("redis hook/guard error", exc_info=True)
    return wrapped(*args, **kwargs)


class RedisIntegration(BaseIntegration):
    """Official OpenTelemetry Redis instrumentation for redis-py and asyncio clients."""

    name = "redis"

    def __init__(self, config: Optional[SDKConfig] = None) -> None:
        super().__init__(config=config)

    def is_installed(self) -> bool:
        try:
            importlib.import_module("redis")
            return True
        except ImportError:
            return False

    def _apply_patch(self) -> None:
        set_config(self._config)
        # Install the full-command formatter BEFORE instrumenting so every
        # path (execute, pipeline, async) formats db.statement the same way.
        _install_command_formatter()
        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            instrumentor = RedisInstrumentor()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(
                    request_hook=_tp_trace_redis_request_hook,
                    response_hook=_tp_trace_redis_response_hook,
                )
            logger.debug("tp_trace: Official RedisInstrumentor applied successfully.")
        except Exception as exc:
            logger.debug("RedisInstrumentor patch skipped: %s", exc)

        # Wrap execute_command and pipeline execution with suppression guard to prevent duplicate child spans
        # when called from high-level cache wrappers (compatible across all OTel versions)
        targets = [
            ("redis.client.Redis", "execute_command"),
            ("redis.client.Redis", "pipeline"),
            ("redis.client.StrictRedis", "execute_command"),
            ("redis.client.StrictRedis", "pipeline"),
            ("redis.Redis", "execute_command"),
            ("redis.Redis", "pipeline"),
            ("redis.client.Pipeline", "execute"),
            ("redis.client.Pipeline", "immediate_execute_command"),
            ("redis.cluster.ClusterPipeline", "execute"),
            ("redis.asyncio.client.Pipeline", "execute"),
            ("redis.asyncio.client.Pipeline", "immediate_execute_command"),
        ]
        for target_cls, target_method in targets:
            try:
                self.wrap(target_cls, target_method, _redis_suppress_guard)
            except Exception:
                pass

    def uninstrument(self) -> bool:
        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            instrumentor = RedisInstrumentor()
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
        except Exception as exc:
            logger.debug("Failed to uninstrument redis: %s", exc)

        _restore_command_formatter()

        return super().uninstrument()


