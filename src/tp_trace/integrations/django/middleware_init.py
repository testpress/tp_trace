"""Django middleware for automatic tp_trace initialization.

Add this to your Django settings.py MIDDLEWARE list to auto-initialize tp_trace:

    MIDDLEWARE = [
        "tp_trace.integrations.django.TpTraceMiddleware",
        # ... your other middleware ...
    ]

The middleware reads configuration from environment variables:
    TP_TRACE_PROJECT_NAME / OTEL_SERVICE_NAME — the project (application) name
    TP_TRACE_ENVIRONMENT / OTEL_ENVIRONMENT — deployment environment
    TP_TRACE_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT — OTLP endpoint

Or set TP_TRACE_DISABLED=1 to disable tracing entirely.

This is an alternative to calling tp_trace.init() directly in settings.py.
"""
import logging
import os

logger = logging.getLogger("tp_trace.integrations.django")

_initialized = False


class TpTraceMiddleware:
    """Django middleware that initializes tp_trace on first request.

    This middleware is intentionally lightweight — it only runs the SDK
    initialization once (on the very first request) and then becomes a
    pass-through for all subsequent requests.

    Usage in settings.py:
        MIDDLEWARE = [
            "tp_trace.integrations.django.TpTraceMiddleware",
            # ... other middleware ...
        ]

    Environment variables:
        TP_TRACE_DISABLED / OTEL_SDK_DISABLED — set to "1" to disable
        TP_TRACE_PROJECT_NAME / OTEL_SERVICE_NAME — the project (application) name
        TP_TRACE_ENVIRONMENT / OTEL_ENVIRONMENT — deployment environment
        TP_TRACE_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT — OTLP collector endpoint
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._ensure_initialized()

    def _ensure_initialized(self):
        global _initialized
        if _initialized:
            return

        # Check if disabled
        disabled_val = (
            os.environ.get("TP_TRACE_DISABLED")
            or os.environ.get("OTEL_SDK_DISABLED", "")
        )
        if disabled_val.lower() in ("1", "true", "yes"):
            logger.debug("tp_trace disabled via environment variable")
            _initialized = True
            return

        try:
            import tp_trace
        except ImportError:
            logger.debug("tp_trace not installed; skipping initialization")
            _initialized = True
            return

        try:
            tp_trace.init(
                project_name=os.environ.get("TP_TRACE_PROJECT_NAME")
                or os.environ.get("OTEL_SERVICE_NAME"),
                environment=os.environ.get("TP_TRACE_ENVIRONMENT")
                or os.environ.get("OTEL_ENVIRONMENT"),
                endpoint=os.environ.get("TP_TRACE_ENDPOINT")
                or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
                # auto_patch=True is the default — all installed integrations
                # (Django, PostgreSQL, Redis, requests) are auto-detected
            )
            logger.info("tp_trace initialized via Django middleware")
        except Exception as exc:
            logger.warning("tp_trace initialization failed: %s", exc)

        _initialized = True

    def __call__(self, request):
        # No-op after initialization — zero overhead on subsequent requests
        return self.get_response(request)

    def process_exception(self, request, exception):
        """Ensure exception tracing is captured even if middleware init fails."""
        return None
