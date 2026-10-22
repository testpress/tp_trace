"""tp_trace OpenTelemetry bootstrap for the Django POC sample app.

Initializes the tp_trace SDK with a single init() call. When auto_patch=True
(the default), all installed integrations (Django, PostgreSQL, Redis, etc.)
are automatically detected and patched.

Fail-safe: the app keeps working without the SDK installed.
"""
import logging
import os

logger = logging.getLogger("config.otel")


def setup_telemetry():
    """Initialize tp_trace with auto-patching. Idempotent, fail-safe."""
    disabled = os.environ.get("TP_TRACE_DISABLED", "").lower()
    if disabled in ("1", "true", "yes"):
        logger.info("tp_trace disabled via TP_TRACE_DISABLED")
        return False
    try:
        import tp_trace
    except ImportError:
        logger.info("tp_trace not installed; observability disabled")
        return False
    try:
        # Minimal init — auto_patch=True (default) detects all installed integrations.
        # Project (application) name, environment, endpoint resolve from env vars:
        #   TP_TRACE_PROJECT_NAME / OTEL_SERVICE_NAME
        #   TP_TRACE_ENVIRONMENT / OTEL_ENVIRONMENT
        #   TP_TRACE_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT
        # Or auto-detected from Django settings if configured there.
        tp_trace.init(
            project_name="otel-sample",
            cluster_name="Dummy",
            tags={
                "server_location": "us-east-1",
                "team": "core-backend",
                "test": "HEHE"
            },
            sample_rate=1.0,
            endpoint_sample_rules={
                "/api/s3-storage/": 1.0,
                "/api/multi-db/": 1.0
            },
        )
        logger.info("tp_trace initialized with auto-patching")
        return True
    except Exception:
        logger.exception("tp_trace initialization failed; continuing without observability")
        return False
