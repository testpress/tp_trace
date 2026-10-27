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


def _make_hook_wrapper(integration: Optional[Any], mw_path: str, mw_short: str, method_name: str):
    span_name = f"{mw_path}.{method_name}"

    def _wrapper(wrapped_call: Callable, inst: Any, a: Any, k: Any):
        if integration is not None and not getattr(integration, "_instrumented", False):
            return wrapped_call(*a, **k)

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
            if integration is not None and not getattr(integration, "_instrumented", False):
                return inner(request)

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


def _make_otel_middleware_factory(integration: Any, middleware_class: Any) -> Callable:
    """Keep OTel's middleware intact while scoping policy and owned-instance lifetime."""
    owns_instrumentor = integration._installed_django_instrumentor

    @functools.wraps(middleware_class)
    def factory(get_response):
        official_middleware = middleware_class(get_response)

        def middleware(request):
            if not integration._instrumented:
                if owns_instrumentor:
                    return get_response(request)
                return official_middleware(request)

            from tp_trace.propagator import django_extraction_context

            with django_extraction_context(request):
                return official_middleware(request)

        # Django registers these separately when it builds the handler chain.
        for name in ("process_view", "process_exception", "process_template_response"):
            hook = getattr(official_middleware, name, None)
            if hook is not None:
                setattr(middleware, name, hook)
        return middleware

    return factory


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
        if active_integration is not None and dotted_path == OTEL_DJANGO_MIDDLEWARE:
            return _make_otel_middleware_factory(active_integration, obj)
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
            if "opentelemetry.instrumentation.django" in middleware_path:
                integration._wrapped_middleware.add(middleware_path)
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
                                    _make_hook_wrapper(integration, middleware_path, middleware_short, method_name),
                                )
                            except Exception as exc:
                                logger.debug("Failed to wrap %s on %s: %s", method_name, middleware_path, exc)

                integration._wrapped_middleware.add(middleware_path)
            except Exception as exc:
                logger.debug("Failed to instrument middleware %s: %s", middleware_path, exc)
    except Exception as exc:
        logger.debug("Middleware instrumentation post-load failed: %s", exc)


OTEL_DJANGO_MIDDLEWARE = "opentelemetry.instrumentation.django.middleware.otel_middleware._DjangoMiddleware"


def ensure_otel_middleware_in_settings(integration: Optional[Any] = None) -> bool:
    """Ensure OpenTelemetry's _DjangoMiddleware is at index 0 of settings.MIDDLEWARE."""
    try:
        from django.conf import settings

        if not getattr(settings, "configured", False):
            return False

        mw_setting = (
            "MIDDLEWARE"
            if hasattr(settings, "MIDDLEWARE") and getattr(settings, "MIDDLEWARE") is not None
            else "MIDDLEWARE_CLASSES"
        )
        mw_list = getattr(settings, mw_setting, None)
        if mw_list is None:
            return False

        if OTEL_DJANGO_MIDDLEWARE in mw_list:
            if mw_list[0] != OTEL_DJANGO_MIDDLEWARE:
                cleaned = [m for m in mw_list if m != OTEL_DJANGO_MIDDLEWARE]
                cleaned.insert(0, OTEL_DJANGO_MIDDLEWARE)
                final_ordered: Any = tuple(cleaned) if isinstance(mw_list, tuple) else cleaned
                setattr(settings, mw_setting, final_ordered)
                logger.debug("tp_trace: Reordered %s to index 0 of settings.%s", OTEL_DJANGO_MIDDLEWARE, mw_setting)
            return True

        final_mw: Any = (
            (OTEL_DJANGO_MIDDLEWARE,) + tuple(mw_list)
            if isinstance(mw_list, tuple)
            else [OTEL_DJANGO_MIDDLEWARE] + list(mw_list)
        )
        setattr(settings, mw_setting, final_mw)
        if integration is not None:
            integration._injected_otel_middleware = True
        logger.debug("tp_trace: Inserted %s at index 0 of settings.%s", OTEL_DJANGO_MIDDLEWARE, mw_setting)
        return True
    except Exception as exc:
        logger.debug("tp_trace: Failed to ensure _DjangoMiddleware in settings: %s", exc)
        return False


def remove_otel_middleware_from_settings() -> None:
    """Remove OpenTelemetry's _DjangoMiddleware from settings on uninstrument."""
    try:
        from django.conf import settings

        if not getattr(settings, "configured", False):
            return

        for mw_setting in ("MIDDLEWARE", "MIDDLEWARE_CLASSES"):
            mw_list = getattr(settings, mw_setting, None)
            if mw_list and OTEL_DJANGO_MIDDLEWARE in mw_list:
                cleaned_mw: Any = (
                    tuple(m for m in mw_list if m != OTEL_DJANGO_MIDDLEWARE)
                    if isinstance(mw_list, tuple)
                    else [m for m in mw_list if m != OTEL_DJANGO_MIDDLEWARE]
                )
                setattr(settings, mw_setting, cleaned_mw)
    except Exception:
        pass


def _compose_django_hooks(integration: Optional[Any] = None) -> None:
    """Compose tp_trace request and response hooks with any existing host hooks."""
    try:
        from opentelemetry.instrumentation.django.middleware.otel_middleware import _DjangoMiddleware
        from .request import _tp_trace_django_request_hook, _tp_trace_django_response_hook

        curr_req = getattr(_DjangoMiddleware, "_otel_request_hook", None)
        if curr_req != _tp_trace_django_request_hook and not getattr(curr_req, "_tp_composed", False):
            if integration is not None:
                integration._orig_host_request_hook = curr_req

            def composed_req_hook(span: Any, req: Any) -> None:
                if curr_req is not None:
                    try:
                        curr_req(span, req)
                    except Exception:
                        logger.debug("Host Django request_hook failed", exc_info=True)
                _tp_trace_django_request_hook(span, req)

            composed_req_hook._tp_composed = True  # type: ignore[attr-defined]
            _DjangoMiddleware._otel_request_hook = composed_req_hook

        curr_resp = getattr(_DjangoMiddleware, "_otel_response_hook", None)
        if curr_resp != _tp_trace_django_response_hook and not getattr(curr_resp, "_tp_composed", False):
            if integration is not None:
                integration._orig_host_response_hook = curr_resp

            def composed_resp_hook(span: Any, req: Any, resp: Any) -> None:
                _tp_trace_django_response_hook(span, req, resp)
                if curr_resp is not None:
                    try:
                        curr_resp(span, req, resp)
                    except Exception:
                        logger.debug("Host Django response_hook failed", exc_info=True)

            composed_resp_hook._tp_composed = True  # type: ignore[attr-defined]
            _DjangoMiddleware._otel_response_hook = composed_resp_hook
    except Exception as exc:
        logger.debug("tp_trace: Failed to compose Django hooks: %s", exc)


def ensure_django_instrumented(integration: Optional[Any] = None) -> bool:
    """Ensure official OpenTelemetry DjangoInstrumentor is instrumented and its middleware is in settings.

    Defers instrumentation when Django settings are not yet configured.
    """
    try:
        from django.conf import settings

        if not getattr(settings, "configured", False):
            logger.debug("tp_trace: Django settings not configured yet; deferring official instrumentation.")
            return False
    except Exception:
        return False

    try:
        from opentelemetry.instrumentation.django import DjangoInstrumentor
        from .request import (
            _tp_trace_django_request_hook,
            _tp_trace_django_response_hook,
        )

        instrumentor = DjangoInstrumentor()
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument(
                request_hook=_tp_trace_django_request_hook,
                response_hook=_tp_trace_django_response_hook,
            )
            if integration is not None:
                integration._installed_django_instrumentor = True
        else:
            _compose_django_hooks(integration)
    except Exception as exc:
        logger.debug("tp_trace: Failed to instrument official DjangoInstrumentor: %s", exc)

    return ensure_otel_middleware_in_settings(integration)


def make_traced_load_middleware(integration):
    """Factory that captures the DjangoIntegration instance so we can track wrapped middlewares."""
    _patch_handler_base_import_string()

    def traced_load_middleware(wrapped: Callable, instance: Any, args: Any, kwargs: Any):
        if not getattr(integration, "_instrumented", False):
            return wrapped(*args, **kwargs)

        ensure_django_instrumented(integration)

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
