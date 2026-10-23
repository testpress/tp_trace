"""Shared pytest fixtures and Django configuration for tp_trace SDK tests."""

import django
from django.conf import settings
import pytest

import tp_trace
from tp_trace.integrations import get_integration_manager

urlpatterns = []

# Configure minimal Django settings once for all test suites
if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="test-secret-key-tp-trace-global",
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["*"],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            },
            "slave1": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            },
            "slave2": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            },
        },
        MIDDLEWARE=[
            "django.middleware.security.SecurityMiddleware",
            "django.middleware.common.CommonMiddleware",
        ],
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": False,
            }
        ],
    )
    django.setup()


from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics.export import MetricExportResult
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Tests that call init() without an explicit exporter still build real OTLP
# exporters for the default local endpoint. Periodic and shutdown flushes would
# then open connections to localhost:4318, so the suite must not depend on a
# collector. Stub the network send for the whole session; the atexit flush runs
# after per-test fixtures are torn down, so this cannot be a monkeypatch fixture.
OTLPSpanExporter.export = lambda self, spans: SpanExportResult.SUCCESS
OTLPMetricExporter.export = lambda self, metrics_data, *args, **kwargs: MetricExportResult.SUCCESS

_orig_init = tp_trace.init


def _safe_test_init(*args, **kwargs):
    if "exporter" not in kwargs or kwargs["exporter"] is None:
        kwargs["exporter"] = InMemorySpanExporter()
    if "metric_readers" not in kwargs:
        # Prevent unit tests from creating background OTLP metric export threads to host
        kwargs["metric_readers"] = []
    return _orig_init(*args, **kwargs)


tp_trace.init = _safe_test_init


@pytest.fixture(autouse=True)
def clean_sdk_state():
    """Reset SDK and integration state before and after every test."""
    tp_trace._reset_for_testing()
    mgr = get_integration_manager()
    mgr.apply_integrations()
    yield
    try:
        mgr.uninstrument_all()
    except Exception:
        pass
    tp_trace._reset_for_testing()
