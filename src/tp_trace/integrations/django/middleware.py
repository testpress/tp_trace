"""Middleware spans — traces each class-based middleware's __call__ and process_* hooks.

Produces exact Datadog APM waterfall hierarchy:
- <module>.<MiddlewareClass>.__call__
  - <module>.<MiddlewareClass>.process_request
  - (downstream middleware chain / view)
  - <module>.<MiddlewareClass>.process_response
"""

from contextvars import ContextVar
import functools
import logging
from typing import Any, Callable, Optional

from opentelemetry.trace import SpanKind

from tp_trace.config_access import get_config as _get_config, set_config as set_config
from tp_trace.safety import attempt
from tp_trace.tracing import traced_span

logger = logging.getLogger("tp_trace.integrations.django.middleware")


def _inspect_and_propagate_request_tags(request: Any) -> None:
    """Auto-detect tenant / institute context on the request and propagate to all child spans."""
    try:
        institute = getattr(request, "institute", None)
        if institute:
            subdomain = getattr(institute, "subdomain", None)
            if subdomain:
                from tp_trace.route_context import set_request_tag

                set_request_tag("institute.subdomain", str(subdomain))
                set_request_tag("institute", str(subdomain))
    except Exception:
        pass


_HOOK_METHODS = (
    "__call__",
    "process_request",
    "process_view",
    "process_response",
    "process_exception",
    "process_template_response",
)


def _make_hook_wrapper(mw_path: str, mw_short: str, method_name: str):
    span_name = f"{mw_path}.{method_name}"

    def _wrapper(wrapped_call: Callable, inst: Any, a: Any, k: Any):
        attrs = {
            "span.type": "web",
            "component": "django",
            "django.middleware": mw_path,
            "django.middleware.name": mw_short,
            "django.middleware.method": method_name,
            "resource.name": span_name,
        }
        cfg = attempt(_get_config, default=None, _label="middleware_config")
        if cfg and cfg.tags:
            attempt(lambda: attrs.update(cfg.tags), _label="merge_tags")

        with traced_span(span_name, kind=SpanKind.INTERNAL, attributes=attrs, tracer_name="tp_trace.django"):
            res = wrapped_call(*a, **k)
            for arg in a:
                if hasattr(arg, "META") or hasattr(arg, "path"):
                    _inspect_and_propagate_request_tags(arg)
                    break
            return res

    return _wrapper


def _make_traced_middleware_factory(integration, mw_path, factory):
    """Wrap a function-based middleware factory so its returned callable is traced."""
    mw_short = mw_path.rsplit(".", 1)[-1]
    span_name = f"{mw_path}.__call__"

    @functools.wraps(factory)
    def traced_factory(get_response):
        inner = factory(get_response)
        if not callable(inner):
            return inner

        @functools.wraps(inner)
        def traced_get_response(request):
            attrs = {
                "span.type": "web",
                "component": "django",
                "django.middleware": mw_path,
                "django.middleware.name": mw_short,
                "django.middleware.method": "__call__",
                "resource.name": span_name,
            }
            cfg = attempt(_get_config, default=None, _label="middleware_config")
            if cfg and cfg.tags:
                try:
                    attrs.update(cfg.tags)
                except Exception:
                    pass
            with traced_span(span_name, kind=SpanKind.INTERNAL, attributes=attrs, tracer_name="tp_trace.django"):
                res = inner(request)
                _inspect_and_propagate_request_tags(request)
                return res

        return traced_get_response

    return traced_factory


_active_middleware_integration: ContextVar[Optional[Any]] = ContextVar("active_middleware_integration", default=None)
_orig_handler_base_import_string: Optional[Callable] = None


def _patch_handler_base_import_string() -> None:
    """Safely patch django.core.handlers.base.import_string once with ContextVar guard."""
    global _orig_handler_base_import_string
    try:
        import django.core.handlers.base as handler_base
    except Exception:
        return

    if getattr(handler_base, "_tp_import_string_patched", False):
        return

    orig = getattr(handler_base, "import_string", None)
    if orig is None:
        return

    _orig_handler_base_import_string = orig

    def patched_import_string(dotted_path: str) -> Any:
        obj = orig(dotted_path)
        active_integration = _active_middleware_integration.get()
        if active_integration is not None and not isinstance(obj, type) and callable(obj):
            return _make_traced_middleware_factory(active_integration, dotted_path, obj)
        return obj

    handler_base.import_string = patched_import_string
    handler_base._tp_import_string_patched = True


def unpatch_handler_base_import_string() -> None:
    """Restore original django.core.handlers.base.import_string."""
    global _orig_handler_base_import_string
    try:
        import django.core.handlers.base as handler_base

        if getattr(handler_base, "_tp_import_string_patched", False):
            if _orig_handler_base_import_string is not None:
                handler_base.import_string = _orig_handler_base_import_string
            delattr(handler_base, "_tp_import_string_patched")
            _orig_handler_base_import_string = None
    except Exception:
        pass


def _instrument_class_middleware(integration, middleware_list):
    try:
        from django.conf import settings
        from django.utils.module_loading import import_string

        if middleware_list is None:
            middleware_list = getattr(settings, "MIDDLEWARE", None) or getattr(settings, "MIDDLEWARE_CLASSES", [])

        for middleware_path in middleware_list:
            if middleware_path in integration._wrapped_middleware:
                continue
            try:
                middleware = import_string(middleware_path)
                if not isinstance(middleware, type):
                    integration._wrapped_middleware.add(middleware_path)
                    continue

                middleware_short = middleware_path.rsplit(".", 1)[-1]

                for method_name in _HOOK_METHODS:
                    if hasattr(middleware, method_name):
                        attr = getattr(middleware, method_name, None)
                        if callable(attr):
                            try:
                                integration.wrap(
                                    middleware,
                                    method_name,
                                    _make_hook_wrapper(middleware_path, middleware_short, method_name),
                                )
                            except Exception as exc:
                                logger.debug("Failed to wrap %s on %s: %s", method_name, middleware_path, exc)

                integration._wrapped_middleware.add(middleware_path)
            except Exception as exc:
                logger.debug("Failed to instrument middleware %s: %s", middleware_path, exc)
    except Exception as exc:
        logger.debug("Middleware instrumentation post-load failed: %s", exc)


def make_traced_load_middleware(integration):
    """Factory that captures the DjangoIntegration instance so we can track wrapped middlewares."""
    _patch_handler_base_import_string()

    def traced_load_middleware(wrapped: Callable, instance: Any, args: Any, kwargs: Any):
        if getattr(instance, "_tp_middleware_instrumented", False):
            return wrapped(*args, **kwargs)

        try:
            from django.conf import settings

            middleware_list = getattr(settings, "MIDDLEWARE", None) or getattr(settings, "MIDDLEWARE_CLASSES", [])
        except Exception:
            middleware_list = []

        token = _active_middleware_integration.set(integration)
        try:
            result = wrapped(*args, **kwargs)
        finally:
            _active_middleware_integration.reset(token)

        try:
            _instrument_class_middleware(integration, middleware_list)
        except Exception as exc:
            logger.debug("Middleware instrumentation post-load failed: %s", exc)

        instance._tp_middleware_instrumented = True
        return result

    return traced_load_middleware
