"""DjangoIntegration — orchestrates request/middleware/view/template/cache/auth patches."""
import importlib
import logging
from typing import Optional

from opentelemetry.trace import SpanKind

from tp_trace.config import SDKConfig
from tp_trace.integrations.base import BaseIntegration
from tp_trace.tracing import traced_span

logger = logging.getLogger("tp_trace.integrations.django")


class DjangoIntegration(BaseIntegration):
    """Full waterfall instrumentation for Django applications (parity with ddtrace)."""

    name = "django"

    def __init__(self, config: Optional[SDKConfig] = None) -> None:
        super().__init__(config=config)
        self._wrapped_middleware: set[str] = set()
        self._populate_patched: bool = False
        self._armed: bool = False

    def is_installed(self) -> bool:
        try:
            importlib.import_module("django")
            return True
        except ImportError:
            return False

    def on_unavailable(self) -> None:
        """Re-arm once Django becomes importable."""
        self._arm()

    def _arm(self) -> None:
        if self._armed:
            return
        try:
            self.when_imported("django", self._on_django_available)
            # Only mark armed once registration succeeded, so a failed
            # when_imported registration can be retried on the next apply.
            self._armed = True
        except Exception as exc:
            logger.debug("Failed to register django when_imported hook: %s", exc)

    def _on_django_available(self, _module=None) -> None:
        self._patch_apps_populate()
        self.instrument()

    def instrument(self) -> bool:
        if self.is_installed():
            self._patch_apps_populate()
        return super().instrument()

    def _patch_apps_populate(self) -> None:
        if self._populate_patched:
            return
        try:
            from django.apps.registry import Apps

            self.wrap(Apps, "populate", self._make_traced_populate())
            self._populate_patched = True
        except Exception as exc:
            logger.debug("Failed to wrap Apps.populate: %s", exc)

    def _make_traced_populate(self):
        def traced_populate(wrapped, instance, args, kwargs):
            result = wrapped(*args, **kwargs)
            if getattr(instance, "ready", False):
                try:
                    self._retry_patch_after_ready()
                except Exception as exc:
                    logger.debug("post-populate instrumentation retry failed: %s", exc)
            return result

        return traced_populate

    def _retry_patch_after_ready(self) -> None:
        if self._instrumented:
            return
        logger.debug("Django apps ready; retrying deferred instrumentation.")
        self.instrument()

    def _apply_patch(self) -> None:

        try:
            import django  # noqa: F401
            from django.core.handlers.base import BaseHandler  # noqa: F401
            from django.template.base import Template  # noqa: F401
        except ImportError:
            logger.debug("Django is not installed, skipping patch.")
            return

        from .cache import CACHE_OPS, make_traced_cache_op
        from .middleware import make_traced_load_middleware
        from .request import traced_get_response
        from .template import traced_template_render, traced_template_response_render
        from .view import traced_get_response as traced_view, traced_view_dispatch, traced_view_setup

        from . import cache as _cache, request as _request, template as _template, view as _view, middleware as _middleware
        _template.set_config(self._config)
        _cache.set_config(self._config)
        _request.set_config(self._config)
        _view.set_config(self._config)
        _middleware.set_config(self._config)

        # 0. Middleware waterfall
        self.wrap(
            "django.core.handlers.base.BaseHandler",
            "load_middleware",
            make_traced_load_middleware(self),
        )

        # 1. Request (SERVER) + metrics
        self.wrap("django.core.handlers.base.BaseHandler", "get_response", traced_get_response)

        # 2. View (INTERNAL, View.setup, View.dispatch & DRF APIView.dispatch)
        self.wrap("django.core.handlers.base.BaseHandler", "_get_response", traced_view)
        try:
            from django.views.generic.base import View  # noqa: F401
            self.wrap("django.views.generic.base.View", "setup", traced_view_setup)
            self.wrap("django.views.generic.base.View", "dispatch", traced_view_dispatch)
        except Exception as exc:
            logger.debug("Django View instrumentation skipped: %s", exc)

        self._patch_rest_framework(traced_view_dispatch)

        # 3. Template & TemplateResponse
        self.wrap("django.template.base.Template", "render", traced_template_render)
        try:
            import django.template.backends.django  # noqa: F401
            self.wrap("django.template.backends.django.Template", "render", traced_template_render)
        except Exception as exc:
            logger.debug("Django backend Template instrumentation skipped: %s", exc)
        try:
            from django.template.response import SimpleTemplateResponse  # noqa: F401
            self.wrap("django.template.response.SimpleTemplateResponse", "render", traced_template_response_render)
        except Exception as exc:
            logger.debug("Django SimpleTemplateResponse instrumentation skipped: %s", exc)

        # 4. Django Cache Backends (BaseCache, django_redis, and Django redis backend)
        self._patch_cache_classes()

        # 5. Django Auth (login, authenticate) - defer via when_imported so auth models aren't loaded prematurely
        self._patch_auth()

        # 6. Management commands
        try:
            from django.core.management.base import BaseCommand  # noqa: F401

            def _traced_command(wrapped, instance, args, kwargs):
                cmd_name = instance.__class__.__module__.split(".")[-1]
                span_name = f"django.command.{cmd_name}"
                attrs = {"django.command.name": cmd_name, "django.command.class": instance.__class__.__name__}
                with traced_span(span_name, kind=SpanKind.SERVER, attributes=attrs, tracer_name="tp_trace.django"):
                    res = wrapped(*args, **kwargs)
                    return res

            self.wrap("django.core.management.base.BaseCommand", "execute", _traced_command)
        except Exception as exc:
            logger.debug("Django command instrumentation skipped: %s", exc)

    def _patch_cache_classes(self) -> None:
        from .cache import CACHE_OPS, make_traced_cache_op

        def _patch_cls(cls_path: str) -> None:
            try:
                mod_name, _, cls_name = cls_path.rpartition(".")
                mod = importlib.import_module(mod_name)
                if hasattr(mod, cls_name):
                    target_cls = getattr(mod, cls_name)
                    for op in CACHE_OPS:
                        if hasattr(target_cls, op):
                            self.wrap(cls_path, op, make_traced_cache_op(op))
            except Exception as exc:
                logger.debug("Django Cache patch skipped for %s: %s", cls_path, exc)

        # BaseCache is standard Django and safe to patch immediately
        _patch_cls("django.core.cache.backends.base.BaseCache")

        # Third-party or version-dependent caches: patch if already imported, else defer via when_imported
        deferred_caches = [
            ("django_redis.cache", "django_redis.cache.RedisCache"),
            ("django_redis.client.default", "django_redis.client.default.DefaultClient"),
            ("django.core.cache.backends.redis", "django.core.cache.backends.redis.RedisCache"),
        ]
        import sys
        for mod_name, cls_path in deferred_caches:
            if mod_name in sys.modules:
                _patch_cls(cls_path)
            else:
                self.when_imported(mod_name, lambda _m, cp=cls_path: _patch_cls(cp))

    def _patch_auth(self, _module=None) -> None:
        def _patch(_mod) -> None:
            try:
                from .auth import traced_authenticate, traced_login
                self.wrap("django.contrib.auth", "login", traced_login)
                self.wrap("django.contrib.auth", "authenticate", traced_authenticate)
            except Exception as exc:
                logger.debug("Django Auth instrumentation skipped: %s", exc)

        try:
            import sys
            if "django.contrib.auth" in sys.modules:
                _patch(sys.modules["django.contrib.auth"])
            else:
                self.when_imported("django.contrib.auth", _patch)
        except Exception as exc:
            logger.debug("Django Auth instrumentation hook skipped: %s", exc)

    def _patch_rest_framework(self, wrapper) -> None:
        def _patch(_module) -> None:
            self.wrap("rest_framework.views.APIView", "dispatch", wrapper)

        try:
            import sys
            if "rest_framework.views" in sys.modules:
                _patch(sys.modules["rest_framework.views"])
            else:
                from importlib.util import find_spec

                if find_spec("rest_framework") is None:
                    return
                self.when_imported("rest_framework.views", _patch)
        except Exception as exc:
            logger.debug("Django REST Framework instrumentation skipped: %s", exc)

    def _remove_patch(self) -> None:
        try:
            from django.core.handlers.base import BaseHandler

            if hasattr(BaseHandler, "_tp_middleware_instrumented"):
                try:
                    delattr(BaseHandler, "_tp_middleware_instrumented")
                except Exception:
                    pass
        except Exception:
            pass
        try:
            from .middleware import unpatch_handler_base_import_string
            unpatch_handler_base_import_string()
        except Exception:
            pass
        self._wrapped_middleware.clear()
        self._populate_patched = False
        self._armed = False
        super()._remove_patch()
