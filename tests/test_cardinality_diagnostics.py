"""Tests for bounded cardinality diagnostics, limit usage, and aggregate preservation."""

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.trace import SpanKind

import tp_trace
from tp_trace.config import SDKConfig
from tp_trace.metrics import RedMetricsRecorder, create_red_metrics_recorder
from tp_trace.red import RedSpanProcessor


class DummySpan:
    def __init__(
        self,
        name="GET /test/",
        attributes=None,
        kind=SpanKind.SERVER,
        start_time=1_000_000_000,
        end_time=1_050_000_000,
        status=None,
    ):
        self.name = name
        self.attributes = attributes or {}
        self.kind = kind
        self.start_time = start_time
        self.end_time = end_time
        self.status = status

    def is_recording(self):
        return True


def test_overflow_limits_cardinality_without_losing_aggregate_measurements():
    """Verify that exceeding cardinality limits collapses excess into __other__
    while preserving 100% of counts, duration histogram totals, and emitting
    bounded diagnostics."""
    reader = InMemoryMetricReader()
    cfg = SDKConfig(project_name="card-test", cluster_name="test-cluster")
    recorder = create_red_metrics_recorder(config=cfg, metric_readers=[reader])
    tp_trace.metrics.set_metrics_recorder(recorder)

    # Low limits: 2 operations per service, 2 routes
    processor = RedSpanProcessor(cardinality_limit_per_service=2, route_limit=2)

    total_requests = 15
    for i in range(total_requests):
        span = DummySpan(
            name=f"GET /route-{i}/",
            attributes={
                "component": "django",
                "http.method": "GET",
                "http.route_template": f"/route-{i}/",
                "http.status_code": 200,
            },
            kind=SpanKind.SERVER,
            start_time=1_000_000_000,
            end_time=1_025_000_000,  # 25ms
        )
        processor.on_end(span)

    reader.collect()
    data = reader.get_metrics_data()
    metrics = {m.name: m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}

    # 1. Total call count MUST equal exactly total_requests
    calls_metric = metrics["calls_total"]
    total_calls = sum(dp.value for dp in calls_metric.data.data_points)
    assert total_calls == total_requests

    # 2. Total duration histogram count MUST equal exactly total_requests
    duration_metric = metrics["duration_milliseconds"]
    total_duration_count = sum(dp.count for dp in duration_metric.data.data_points)
    assert total_duration_count == total_requests

    # 3. Cardinality is limited: distinct routes emitted in calls_total <= 3 (2 admitted + 1 __other__)
    emitted_routes = {dp.attributes.get("http.route") for dp in calls_metric.data.data_points}
    assert "__other__" in emitted_routes
    assert len(emitted_routes) <= 3

    # 4. Diagnostic grouping counter emitted with bounded labels
    assert "apm_cardinality_grouping_total" in metrics
    grouping_metric = metrics["apm_cardinality_grouping_total"]
    grouping_points = grouping_metric.data.data_points
    assert len(grouping_points) > 0

    # Total grouped calls should be total_requests - 2 (since 2 routes admitted)
    total_route_grouped = sum(
        dp.value for dp in grouping_points if dp.attributes.get("scope") == "route"
    )
    assert total_route_grouped == total_requests - 2

    # Check label schema on diagnostic points
    for dp in grouping_points:
        assert dp.attributes.get("project_name") == "card-test"
        assert dp.attributes.get("cluster_name") == "test-cluster"
        if dp.attributes.get("scope") in ("route", "service"):
            assert dp.attributes.get("normalized_service") == "all"
        else:
            assert dp.attributes.get("normalized_service") == "django"
        assert dp.attributes.get("metric_source") == "tp_trace_sdk"
        assert dp.attributes.get("scope") in ("route", "operation")
        assert dp.attributes.get("cause") in ("route_limit", "operation_limit")
        assert "worker_id" in dp.attributes


def test_metric_series_cardinality_budget_emits_diagnostics_and_preserves_totals():
    """Verify that RED metric recorder series budget collapses excess combinations
    into __other__ without losing total call counts, and records diagnostics."""
    reader = InMemoryMetricReader()
    cfg = SDKConfig(project_name="series-test", cluster_name="prod", metric_cardinality_limit=3)
    recorder = RedMetricsRecorder(
        meter_provider=create_red_metrics_recorder(config=cfg, metric_readers=[reader]).meter_provider,
        config=cfg,
        readers=[reader],
    )

    # Record 10 different label combinations (exceeding budget of 3)
    for i in range(10):
        recorder.record_request(
            method="GET",
            route=f"/endpoint-{i}/",
            status_code=200,
            error=False,
            duration_ms=10.0,
            service="django",
            operation=f"GET /endpoint-{i}/",
        )

    reader.collect()
    data = reader.get_metrics_data()
    metrics = {m.name: m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}

    calls_metric = metrics["calls_total"]
    total_calls = sum(dp.value for dp in calls_metric.data.data_points)
    assert total_calls == 10

    # Excess was grouped into __other__
    assert any(dp.attributes.get("normalized_service") == "__other__" for dp in calls_metric.data.data_points)

    # Diagnostic grouping metric captures metric_series_limit overflow
    grouping_metric = metrics["apm_cardinality_grouping_total"]
    grouping_dps = [
        dp for dp in grouping_metric.data.data_points
        if dp.attributes.get("scope") == "metric_series"
    ]
    assert len(grouping_dps) > 0
    assert sum(dp.value for dp in grouping_dps) == 7  # 10 - 3 admitted = 7 grouped
    assert grouping_dps[0].attributes.get("cause") == "metric_series_limit"


def test_cardinality_tracked_and_limit_gauges():
    """Verify that apm_cardinality_tracked and apm_cardinality_limit gauges accurately report counts."""
    reader = InMemoryMetricReader()
    cfg = SDKConfig(project_name="gauge-test", cluster_name="prod")
    recorder = create_red_metrics_recorder(config=cfg, metric_readers=[reader])
    tp_trace.metrics.set_metrics_recorder(recorder)

    processor = RedSpanProcessor(cardinality_limit_per_service=64, route_limit=128, service_limit=32)

    # Record 3 distinct operations for postgres
    for i in range(3):
        span = DummySpan(
            name=f"SELECT {i}",
            attributes={
                "db.system": "postgres",
                "normalized.service": "postgres",
                "normalized.operation": f"SELECT table_{i}",
            },
            kind=SpanKind.CLIENT,
        )
        processor.on_end(span)

    reader.collect()
    data = reader.get_metrics_data()
    metrics = {m.name: m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}

    assert "apm_cardinality_tracked" in metrics
    assert "apm_cardinality_limit" in metrics

    tracked_metric = metrics["apm_cardinality_tracked"]
    limit_metric = metrics["apm_cardinality_limit"]

    # Verify postgres operation tracking
    pg_tracked = next(
        dp for dp in tracked_metric.data.data_points
        if dp.attributes.get("normalized_service") == "postgres" and dp.attributes.get("scope") == "operation"
    )
    assert pg_tracked.value == 3

    pg_limit = next(
        dp for dp in limit_metric.data.data_points
        if dp.attributes.get("normalized_service") == "postgres" and dp.attributes.get("scope") == "operation"
    )
    assert pg_limit.value == 64

    # Verify metric_series limit
    series_limit = next(
        dp for dp in limit_metric.data.data_points
        if dp.attributes.get("scope") == "metric_series"
    )
    assert series_limit.value == 5000


def test_diagnostic_labels_are_strictly_bounded():
    """Verify that arbitrary service names and scopes are strictly bounded to prevent leaks."""
    reader = InMemoryMetricReader()
    cfg = SDKConfig(project_name="bound-test", cluster_name="prod")
    recorder = create_red_metrics_recorder(config=cfg, metric_readers=[reader])

    # Unsanitized scope and cause should fall back to "unknown"
    recorder.record_cardinality_grouping(
        service="django",
        scope="malicious_unbounded_scope_name_123",
        cause="untrusted_cause_string",
    )

    # Record 80 distinct service names; only first 64 should be admitted, rest collapse to __other__
    for i in range(80):
        recorder.record_cardinality_grouping(
            service=f"custom_service_{i}",
            scope="operation",
            cause="operation_limit",
        )

    reader.collect()
    data = reader.get_metrics_data()
    metrics = {m.name: m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    grouping_metric = metrics["apm_cardinality_grouping_total"]

    scopes = {dp.attributes.get("scope") for dp in grouping_metric.data.data_points}
    causes = {dp.attributes.get("cause") for dp in grouping_metric.data.data_points}
    services = {dp.attributes.get("normalized_service") for dp in grouping_metric.data.data_points}

    assert "unknown" in scopes
    assert "unknown" in causes
    assert "__other__" in services
    # Total distinct services must be <= 65 (64 admitted + __other__)
    assert len(services) <= 66


def test_telemetry_safety_in_cardinality_diagnostics():
    """Verify that exceptions in diagnostics recording or gauges never raise or crash."""
    recorder = create_red_metrics_recorder(config=None, metric_readers=[InMemoryMetricReader()])
    # Corrupt internal state deliberately
    recorder._cached_identity = "invalid_type"  # type: ignore

    # Should not raise
    recorder.record_cardinality_grouping(service="django", scope="operation", cause="operation_limit")
    obs = list(recorder._observe_cardinality_tracked())
    assert isinstance(obs, list)


def test_service_count_excludes_overflow_sentinel_and_never_exceeds_limit():
    """Verify that service_count in stats never exceeds service_limit when __other__ is added."""
    processor = RedSpanProcessor(service_limit=2, cardinality_limit_per_service=10)
    for i in range(5):
        span = DummySpan(
            name=f"call_{i}",
            attributes={"normalized.service": f"service_{i}", "normalized.operation": "op"},
            kind=SpanKind.CLIENT,
        )
        processor.on_end(span)

    stats = processor.get_cardinality_stats()
    assert stats["service_count"] == 2
    assert stats["service_count"] <= stats["service_limit"]
    processor.shutdown()


def test_span_processor_shutdown_clears_active_pointer():
    """Verify that RedSpanProcessor.shutdown() clears _ACTIVE_SPAN_PROCESSOR."""
    from tp_trace.red import get_active_span_processor

    proc = RedSpanProcessor()
    assert get_active_span_processor() is proc
    proc.shutdown()
    assert get_active_span_processor() is None


def test_gauges_omit_scope_when_no_active_span_processor():
    """Verify that _observe_cardinality_tracked/limit omit processor scopes when none is active."""
    from tp_trace.red import get_active_span_processor

    assert get_active_span_processor() is None

    recorder = create_red_metrics_recorder(config=None, metric_readers=[InMemoryMetricReader()])
    tracked_obs = list(recorder._observe_cardinality_tracked())
    limit_obs = list(recorder._observe_cardinality_limit())

    tracked_scopes = [o.attributes["scope"] for o in tracked_obs]
    limit_scopes = [o.attributes["scope"] for o in limit_obs]

    # Only metric_series belongs to metrics recorder; route/service/operation must not be emitted
    assert tracked_scopes == ["metric_series"]
    assert limit_scopes == ["metric_series"]

