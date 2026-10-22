"""Tests for Phase 2: Independent RED Metrics Recording.

Verifies that RED metrics (calls_total, duration_milliseconds) are accurately recorded
for 100% of requests regardless of trace sampling rates (1.0, 0.1, 0.01, 0.0),
including exceptions, early responses, and that metric failures never break requests.
"""

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from django.conf import settings
from django.core.handlers.wsgi import WSGIHandler
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import path, clear_url_caches

import tp_trace
from tp_trace.integrations.django import DjangoIntegration
from tp_trace.metrics import (
    LATENCY_BUCKETS_MS,
    get_metrics_recorder,
)


def ok_view(request):
    return HttpResponse("OK", status=200)


def not_found_view(request):
    return HttpResponse("Not Found", status=404)


def crash_view(request):
    raise RuntimeError("Uncaught application crash")


urlpatterns = [
    path("api/test/ok/", ok_view, name="ok-view"),
    path("api/test/404/", not_found_view, name="not-found-view"),
    path("api/test/crash/", crash_view, name="crash-view"),
]


@pytest.fixture(autouse=True)
def setup_django_and_tp_trace():
    tp_trace._reset_for_testing()
    settings.ROOT_URLCONF = "tests.test_metrics"
    clear_url_caches()
    integration = DjangoIntegration()
    integration.instrument()
    yield
    integration.uninstrument()
    tp_trace._reset_for_testing()


def _get_metric_data_points(metric_reader: InMemoryMetricReader, metric_name: str):
    """Helper to extract data points for a given metric from InMemoryMetricReader."""
    data = metric_reader.get_metrics_data()
    points = []
    if not data:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == metric_name:
                    points.extend(metric.data.data_points)
    return points


@pytest.mark.parametrize("sample_rate", [1.0, 0.1, 0.01, 0.0])
def test_red_metrics_independent_of_trace_sampling(sample_rate):
    """Verify metrics record 100% of requests even when trace sampling drops spans."""
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    tp_trace.init(
        project_name="metrics-test-project",
        cluster_name="staging-cluster",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=sample_rate,
        metric_readers=[metric_reader],
    )

    handler = WSGIHandler()
    rf = RequestFactory()

    num_requests = 50
    for _ in range(num_requests):
        req = rf.get("/api/test/ok/")
        resp = handler.get_response(req)
        assert resp.status_code == 200

    # 1. Verify Trace Spans: should be constrained by sample_rate
    spans = span_exporter.get_finished_spans()
    server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
    if sample_rate == 1.0:
        assert len(server_spans) == num_requests
    elif sample_rate == 0.0:
        assert len(server_spans) == 0
    else:
        # Probabilistic TraceIdRatioBased: sampled count is significantly lower than total
        assert len(server_spans) < num_requests

    # 2. Verify RED Metrics: MUST record 100% of requests regardless of sample_rate!
    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) > 0

    total_recorded_calls = sum(pt.value for pt in calls_points)
    assert total_recorded_calls == num_requests, (
        f"Expected {num_requests} recorded calls at sample_rate={sample_rate}, but got {total_recorded_calls}"
    )

    # Verify duration histogram points
    duration_points = _get_metric_data_points(metric_reader, "duration_milliseconds")
    assert len(duration_points) > 0
    total_recorded_durations = sum(pt.count for pt in duration_points)
    assert total_recorded_durations == num_requests


def test_red_metrics_attribute_contract():
    """Verify recorded metric attributes match Grafana APM dashboard requirements."""
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="my-app",
        cluster_name="cluster-xyz",
        sample_rate=0.05,  # heavily sampled trace
        metric_readers=[metric_reader],
        export_batch=False,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    req = rf.get("/api/test/ok/")
    resp = handler.get_response(req)
    assert resp.status_code == 200

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1
    pt = calls_points[0]
    attrs = pt.attributes

    # Check contract fields
    assert attrs["project_name"] == "my-app"
    assert attrs["cluster_name"] == "cluster-xyz"
    assert attrs["normalized_service"] == "django"
    assert attrs["normalized_operation"] == "GET /api/test/ok/"
    assert attrs["http.route"] == "/api/test/ok/"
    assert attrs["http.status_code"] == 200
    assert attrs["status_code"] == "200"
    assert attrs["span_kind"] == "SPAN_KIND_SERVER"
    assert attrs["error"] == "false"


class _DummyInstitute:
    subdomain = "demo"


def test_red_metrics_carry_institute_tags():
    """Dynamic request tags (institute / institute.subdomain) must reach the SDK RED metrics.

    These become the `institute_subdomain` / `institute` labels on apm_calls_total in
    Prometheus, enabling per-tenant filtering on the full-fidelity SDK metrics.
    """
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="institute-app",
        sample_rate=0.1,
        metric_readers=[metric_reader],
        export_batch=False,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    req = rf.get("/api/test/ok/")
    req.institute = _DummyInstitute()
    resp = handler.get_response(req)
    assert resp.status_code == 200

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1
    attrs = calls_points[0].attributes
    assert attrs.get("institute.subdomain") == "demo"
    assert attrs.get("institute") == "demo"


def test_red_metrics_exception_handling():
    """Verify unhandled exceptions record 500 error metric with error='true'."""
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-crash",
        sample_rate=0.0,  # traces completely dropped
        metric_readers=[metric_reader],
        export_batch=False,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    req = rf.get("/api/test/crash/")

    resp = handler.get_response(req)
    assert resp.status_code == 500

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1
    pt = calls_points[0]
    attrs = pt.attributes

    assert pt.value == 1
    assert attrs["error"] == "true"
    assert attrs["http.status_code"] == 500
    assert attrs["status_code"] == "500"
    assert attrs["normalized_operation"] == "GET /api/test/crash/"


def test_red_metrics_early_response_404():
    """Verify 404 responses record calls_total with error='false' (non-5xx)."""
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-404",
        sample_rate=0.1,
        metric_readers=[metric_reader],
        export_batch=False,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    req = rf.get("/api/test/404/")
    resp = handler.get_response(req)
    assert resp.status_code == 404

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1
    pt = calls_points[0]
    attrs = pt.attributes

    assert pt.value == 1
    assert attrs["error"] == "false"
    assert attrs["http.status_code"] == 404
    assert attrs["status_code"] == "404"


def test_red_metrics_recording_failure_never_disrupts_request(monkeypatch):
    """Verify that if metric recording crashes internally, Django request succeeds normally."""
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="resilience-test",
        sample_rate=1.0,
        metric_readers=[metric_reader],
        export_batch=False,
    )

    recorder = get_metrics_recorder()
    assert recorder is not None

    def exploding_add(*args, **kwargs):
        raise RuntimeError("Metric collection failure: connection timeout / memory full")

    monkeypatch.setattr(recorder.calls_total, "add", exploding_add)

    handler = WSGIHandler()
    rf = RequestFactory()
    req = rf.get("/api/test/ok/")

    # The request MUST return 200 OK without raising the RuntimeError!
    resp = handler.get_response(req)
    assert resp.status_code == 200
    assert resp.content == b"OK"


def test_histogram_explicit_bucket_boundaries():
    """Verify duration histogram matches exact APM buckets."""
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="bucket-test",
        metric_readers=[metric_reader],
        export_batch=False,
    )

    recorder = get_metrics_recorder()
    assert recorder is not None

    recorder.record_request(
        method="GET",
        route="/test/",
        status_code=200,
        error=False,
        duration_ms=45.0,
    )

    duration_points = _get_metric_data_points(metric_reader, "duration_milliseconds")
    assert len(duration_points) == 1
    pt = duration_points[0]

    assert list(pt.explicit_bounds) == LATENCY_BUCKETS_MS
    assert pt.count == 1
    assert pt.sum == 45.0


def test_default_init_creates_otlp_metric_reader():
    """Verify default init() creates PeriodicExportingMetricReader targeting /v1/metrics."""
    tp_trace.init(
        project_name="otlp-export-test",
        endpoint="http://collector.local:4318",
        export_batch=False,
    )
    recorder = get_metrics_recorder()
    assert recorder is not None
    # Verify meter_provider has 1 PeriodicExportingMetricReader
    assert len(recorder.readers) == 1
    reader = recorder.readers[0]

    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from tp_trace.exporter import SafeMetricExporter

    assert isinstance(reader, PeriodicExportingMetricReader)
    assert isinstance(reader._exporter, SafeMetricExporter)
    assert reader._exporter._endpoint == "http://collector.local:4318/v1/metrics"
    assert reader._export_interval_millis == 60000


def test_metrics_disabled_flag():
    """Verify metrics_enabled=False prevents creating metric readers."""
    tp_trace.init(
        project_name="metrics-disabled-test",
        endpoint="http://collector.local:4318",
        metrics_enabled=False,
        export_batch=False,
    )
    recorder = get_metrics_recorder()
    assert recorder is not None
    assert len(recorder.readers) == 0


def test_safe_metric_exporter_absorbs_errors():
    """Verify SafeMetricExporter catches exceptions and returns FAILURE safely."""
    from opentelemetry.sdk.metrics.export import MetricExportResult
    from tp_trace.exporter import SafeMetricExporter

    class FaultyExporter:
        def __init__(self):
            self.calls = 0

        def export(self, metrics_data, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionRefusedError("Connection refused by remote collector")
            return MetricExportResult.SUCCESS

        def shutdown(self, **kwargs):
            pass

        def force_flush(self, **kwargs):
            return True

    faulty = FaultyExporter()
    safe = SafeMetricExporter(faulty, endpoint="http://test-collector:4318")

    # 1. First export fails with exception, but SafeMetricExporter catches it
    result1 = safe.export(None)
    assert result1 == MetricExportResult.FAILURE
    assert safe._error_count == 1

    # 2. Second export succeeds, error count resets
    result2 = safe.export(None)
    assert result2 == MetricExportResult.SUCCESS
    assert safe._error_count == 0

    # 3. Flush & Shutdown don't raise
    assert safe.force_flush() is True
    safe.shutdown()
