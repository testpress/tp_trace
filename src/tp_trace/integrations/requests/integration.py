"""RequestsIntegration — leverages OpenTelemetry requests instrumentor for HTTP client tracing."""

import importlib
import logging

from tp_trace.integrations.base import BaseIntegration

logger = logging.getLogger("tp_trace.integrations.requests")


class RequestsIntegration(BaseIntegration):
    """Outgoing HTTP request tracing and distributed context propagation for requests."""

    name = "requests"

    def is_installed(self) -> bool:
        try:
            importlib.import_module("requests")
            return True
        except ImportError:
            return False

    def _apply_patch(self) -> None:
        from .client import set_config, tp_trace_request_hook, tp_trace_response_hook

        set_config(self._config)

        try:
            from opentelemetry.instrumentation.requests import RequestsInstrumentor

            instrumentor = RequestsInstrumentor()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(
                    request_hook=tp_trace_request_hook,
                    response_hook=tp_trace_response_hook,
                )
        except Exception as exc:
            logger.debug("RequestsInstrumentor patch skipped: %s", exc)

    def uninstrument(self) -> bool:
        try:
            from opentelemetry.instrumentation.requests import RequestsInstrumentor

            instrumentor = RequestsInstrumentor()
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
        except Exception as exc:
            logger.debug("Failed to uninstrument requests: %s", exc)

        return super().uninstrument()
