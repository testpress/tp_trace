"""Tests for Repeated Database Query (N+1) Anomaly Detection.

Validates:
1. Request-local tracker mechanics, bounded fingerprint storage, and reset behavior.
2. Metrics recorder allowlist guarantees (no raw SQL, no trace ID, no worker ID).
3. Config parsing, environment variable fallbacks, and non-raising defaults.
4. Django request-level detection, root span tagging, and route cardinality guards.
"""

import pytest
from django.conf import settings
from django.core.handlers.wsgi import WSGIHandler
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import clear_url_caches, path
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import tp_trace
from tp_trace.config import SDKConfig
from tp_trace.integrations.django import DjangoIntegration
from tp_trace.metrics import (
    init_metrics,
    record_repeated_query_affected_request,
)
from tp_trace.route_context import (
    get_request_db_query_stats,
    init_request_db_tracker,
    record_request_db_query,
    reset_request_db_tracker,
)
from tp_trace.integrations.django.request import _repeated_query_severity


def n_plus_one_view(request):
    # Simulate DB queries by invoking record_request_db_query
    count = int(request.GET.get("count", "15"))
    duration_ms = float(request.GET.get("duration_ms", "10"))
    sql = request.GET.get("sql", "SELECT id, name FROM users WHERE id = %s")
    for _ in range(count):
        record_request_db_query(sql, duration_ms)
    return HttpResponse("OK", status=200)


def multi_query_view(request):
    # Simulate many distinct queries (e.g., 30 distinct queries)
    for i in range(30):
        record_request_db_query(f"SELECT val FROM table_{i}", 10)
    return HttpResponse("OK", status=200)


def n_plus_one_then_raise_view(request):
    for _ in range(15):
        record_request_db_query("SELECT id FROM users WHERE id = ?", 1.0)
    raise RuntimeError("expected test exception")


def multiple_repeated_queries_view(request):
    for pattern in range(7):
        for _ in range(15 + pattern):
            record_request_db_query(f"SELECT value FROM candidate_{pattern} WHERE id = ?", 2.0)
    return HttpResponse("OK")


def repeated_real_sql_view(request):
    with connection.cursor() as cursor:
        for value in range(15):
            cursor.execute("SELECT %s", [value])
            cursor.fetchone()
    return HttpResponse("OK")


urlpatterns = [
    path("api/test/n-plus-one/", n_plus_one_view, name="n-plus-one-view"),
    path("api/test/multi-query/", multi_query_view, name="multi-query-view"),
    path("api/test/n-plus-one-error/", n_plus_one_then_raise_view, name="n-plus-one-error-view"),
    path("api/test/repeated-patterns/", multiple_repeated_queries_view, name="repeated-patterns-view"),
    path("api/test/real-sql/", repeated_real_sql_view, name="real-sql-view"),
]


@pytest.fixture(autouse=True)
def setup_django_and_tp_trace():
    tp_trace._reset_for_testing()
    settings.ROOT_URLCONF = "tests.test_n_plus_one"
    clear_url_caches()
    integration = DjangoIntegration()
    integration.instrument()
    yield
    integration.uninstrument()
    tp_trace._reset_for_testing()


def _get_metric_data_points(metric_reader: InMemoryMetricReader, metric_name: str):
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


def test_tracker_basic_counting_and_reset():
    """Verify request db tracker tracks queries, finds dominant fingerprint, and resets."""
    # Without init, recording is a no-op
    record_request_db_query("SELECT 1")
    stats = get_request_db_query_stats()
    assert stats.total_queries == 0
    assert stats.dominant_query is None

    token = init_request_db_tracker(max_fingerprints=3)
    try:
        record_request_db_query("SELECT * FROM items WHERE id = ?", 10)
        record_request_db_query("SELECT * FROM items WHERE id = ?", 20)
        record_request_db_query("SELECT * FROM items WHERE id = ?", 30)
        record_request_db_query("SELECT count(*) FROM orders", 5)

        stats = get_request_db_query_stats()
        assert stats.total_queries == 4
        assert stats.dominant_query == "SELECT * FROM items WHERE id = ?"
        assert stats.dominant_count == 3
        assert stats.dominant_duration_ms == 60
        assert stats.tracker_overflowed is False
    finally:
        reset_request_db_tracker(token)

    # After reset, returns empty stats
    stats_after = get_request_db_query_stats()
    assert stats_after.total_queries == 0
    assert stats_after.dominant_query is None


@pytest.mark.parametrize(
    ("count", "impact", "severity", "reason"),
    [
        (15, 0.0, "info", "structural_repetition"),
        (30, 0.0, "low", "repetition_volume"),
        (15, 20.0, "medium", "request_impact"),
        (100, 50.0, "high", "request_impact_and_repetition"),
    ],
)
def test_repeated_query_severity_is_bounded(count, impact, severity, reason):
    assert _repeated_query_severity(count, impact) == (severity, reason)


def test_tracker_bounded_fingerprints_overflow():
    """Verify tracker bounds memory footprint and sets overflow flag."""
    token = init_request_db_tracker(max_fingerprints=2)
    try:
        record_request_db_query("QUERY A", 10)
        record_request_db_query("QUERY B", 10)
        # 3rd distinct query exceeds max_fingerprints=2
        record_request_db_query("QUERY C", 10)

        stats = get_request_db_query_stats()
        assert stats.total_queries == 3
        assert stats.tracker_overflowed is True
        # Original fingerprints still counted
        record_request_db_query("QUERY A", 10)
        stats2 = get_request_db_query_stats()
        assert stats2.dominant_query == "QUERY A"
        assert stats2.dominant_count == 2
    finally:
        reset_request_db_tracker(token)


def test_tracker_multiple_candidates_and_patterns():
    """Verify tracker collects all patterns and identifies all candidates above threshold."""
    token = init_request_db_tracker(max_fingerprints=10)
    try:
        # Query A x 20
        for _ in range(20):
            record_request_db_query("SELECT * FROM table_a WHERE id = ?", 2.0)
        # Query B x 15
        for _ in range(15):
            record_request_db_query("SELECT * FROM table_b WHERE id = ?", 3.0)
        # Query C x 2
        for _ in range(2):
            record_request_db_query("SELECT count(*) FROM table_c", 1.0)

        stats = get_request_db_query_stats(threshold=15)
        assert stats.total_queries == 37
        assert len(stats.patterns) == 3
        # Both Query A and Query B are candidates (>= 15)
        assert stats.candidate_count == 2
        assert len(stats.candidates) == 2
        assert stats.dominant_query == "SELECT * FROM table_a WHERE id = ?"
        assert stats.dominant_count == 20
        assert stats.dominant_duration_ms == 40.0
    finally:
        reset_request_db_tracker(token)


def test_repeated_query_metric_cardinality_preserves_counts_and_owners():
    reader = InMemoryMetricReader()
    init_metrics(
        config=SDKConfig(project_name="my-app", cluster_name="prod", metric_cardinality_limit=1),
        metric_readers=[reader],
    )
    for i in range(100):
        record_repeated_query_affected_request(
            method="method-{}".format(i),
            route="/route-{}/".format(i),
            service="service-{}".format(i),
            extra_attributes={"institute.subdomain": "tenant-{}".format(i)},
        )
        record_repeated_query_affected_request(
            method="GET",
            route="/items/",
            project_name="new-{}".format(i),
            cluster_name=str(i),
        )
    # Already admitted series must remain detailed after the budget is exhausted.
    record_repeated_query_affected_request(
        method="method-0",
        route="/route-0/",
        service="service-0",
        extra_attributes={"institute.subdomain": "tenant-0"},
    )
    points = _get_metric_data_points(reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 3  # One detailed series, one admitted-owner overflow, one new-owner overflow.
    assert sum(p.value for p in points) == 201
    assert sum(p.value for p in points if p.attributes["project_name"] == "my-app") == 101
    assert sum(p.value for p in points if p.attributes["project_name"] == "__other__") == 100
    detailed = [p for p in points if p.attributes["http.route"] == "/route-0/"]
    assert len(detailed) == 1
    assert detailed[0].value == 2
    for point in points:
        assert "worker_id" not in point.attributes
        assert set(point.attributes) <= {
            "project_name",
            "cluster_name",
            "normalized_service",
            "http.method",
            "http.route",
            "metric_source",
            "institute.subdomain",
        }


def test_repeated_query_metric_allowlist():
    """Verify the affected-request metric enforces the strict label allowlist."""
    metric_reader = InMemoryMetricReader()
    init_metrics(
        config=SDKConfig(project_name="my-app", cluster_name="prod-us"),
        metric_readers=[metric_reader],
    )

    record_repeated_query_affected_request(
        method="POST",
        route="/api/v1/checkout/",
        service="django",
        extra_attributes={
            "institute.subdomain": "acme-corp",
            "forbidden_worker_id": "9999",
            "sql_query": "SELECT * FROM users",
            "trace_id": "abc123",
        },
    )

    points = _get_metric_data_points(metric_reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 1
    attrs = dict(points[0].attributes)

    # Allowed labels:
    assert attrs["project_name"] == "my-app"
    assert attrs["cluster_name"] == "prod-us"
    assert attrs["normalized_service"] == "django"
    assert attrs["http.method"] == "POST"
    assert attrs["http.route"] == "/api/v1/checkout/"
    assert attrs["metric_source"] == "tp_trace_sdk"
    assert attrs["institute.subdomain"] == "acme-corp"

    # Strictly forbidden labels (cardinality explosion prevention):
    assert "worker_id" not in attrs
    assert "forbidden_worker_id" not in attrs
    assert "sql_query" not in attrs
    assert "trace_id" not in attrs
    assert "query" not in attrs
    assert "statement" not in attrs
    assert points[0].value == 1

    # Verify deprecated alias apm_n_plus_one_total is NOT emitted to prevent double-counting
    legacy_points = _get_metric_data_points(metric_reader, "apm_n_plus_one_total")
    assert len(legacy_points) == 0


def test_django_n_plus_one_under_threshold():
    """14 repeated queries does not trigger anomaly when threshold is 15."""
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    tp_trace.init(
        project_name="test-proj",
        cluster_name="test-cluster",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=1.0,
        metric_readers=[metric_reader],
        n_plus_one_duplicate_threshold=15,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    request = rf.get("/api/test/n-plus-one/?count=14")

    response = handler(request.environ, lambda s, h: None)
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    root_span = next(s for s in spans if s.kind.name == "SERVER")
    assert "n_plus_one.detected" not in root_span.attributes

    points = _get_metric_data_points(metric_reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 0


def test_django_n_plus_one_at_or_above_threshold():
    """15 repeated queries triggers anomaly and emits metric."""
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    tp_trace.init(
        project_name="test-proj",
        cluster_name="test-cluster",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=1.0,
        metric_readers=[metric_reader],
        n_plus_one_duplicate_threshold=15,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    request = rf.get("/api/test/n-plus-one/?count=15")

    response = handler(request.environ, lambda s, h: None)
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    root_span = next(s for s in spans if s.kind.name == "SERVER")
    assert root_span.attributes.get("n_plus_one.detected") is True
    assert root_span.attributes.get("db.query_count") == 15
    assert root_span.attributes.get("n_plus_one.dominant_count") == 15
    assert root_span.attributes.get("n_plus_one.dominant_duration_ms") == 150
    assert "SELECT id, name FROM users" in root_span.attributes.get("n_plus_one.dominant_query")
    assert root_span.attributes.get("n_plus_one.tracker_overflowed") is False

    points = _get_metric_data_points(metric_reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 1
    assert points[0].value == 1
    assert points[0].attributes.get("http.route") == "/api/test/n-plus-one/"


def test_django_keeps_five_bounded_candidate_summaries_on_root_span():
    exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-proj",
        cluster_name="test-cluster",
        exporter=exporter,
        export_batch=False,
        sample_rate=1.0,
        metric_readers=[metric_reader],
    )

    request = RequestFactory().get("/api/test/repeated-patterns/")
    response = WSGIHandler()(request.environ, lambda s, h: None)
    assert response.status_code == 200

    root = next(s for s in exporter.get_finished_spans() if s.kind.name == "SERVER")
    attrs = root.attributes
    assert attrs["n_plus_one.candidate_count"] == 7
    assert attrs["repeated_query.omitted_candidate_count"] == 2
    assert attrs["repeated_query.candidate_counts"] == (21, 20, 19, 18, 17)
    assert attrs["repeated_query.candidate_duration_ms"] == (42.0, 40.0, 38.0, 36.0, 34.0)
    assert attrs["repeated_query.candidate_avg_duration_ms"] == (2.0,) * 5
    assert len(attrs["repeated_query.candidate_queries"]) == 5
    assert "candidate_6" in attrs["repeated_query.candidate_queries"][0]
    points = _get_metric_data_points(metric_reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 1
    assert points[0].value == 1


def test_real_django_database_calls_count_once_with_unsampled_trace():
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-proj",
        cluster_name="test-cluster",
        sample_rate=0.0,
        metric_readers=[metric_reader],
    )
    request = RequestFactory().get("/api/test/real-sql/")
    response = WSGIHandler()(request.environ, lambda s, h: None)
    assert response.status_code == 200
    points = _get_metric_data_points(metric_reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 1
    assert points[0].value == 1
    assert points[0].attributes["http.route"] == "/api/test/real-sql/"


def test_django_n_plus_one_is_recorded_when_view_raises():
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-proj",
        cluster_name="test-cluster",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=1.0,
        metric_readers=[metric_reader],
        n_plus_one_duplicate_threshold=15,
    )

    request = RequestFactory().get("/api/test/n-plus-one-error/")
    response = WSGIHandler()(request.environ, lambda s, h: None)
    assert response.status_code == 500

    root_span = next(s for s in span_exporter.get_finished_spans() if s.kind.name == "SERVER")
    assert root_span.attributes.get("n_plus_one.detected") is True
    assert root_span.attributes.get("n_plus_one.dominant_count") == 15
    points = _get_metric_data_points(metric_reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 1
    assert points[0].attributes.get("http.route") == "/api/test/n-plus-one-error/"


def test_django_repeated_queries_structural_detection_even_if_fast():
    """Fifteen fast repeated queries (15ms total) ARE structurally detected, with duration recorded as impact."""
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    tp_trace.init(
        project_name="test-proj",
        cluster_name="test-cluster",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=1.0,
        metric_readers=[metric_reader],
        n_plus_one_duplicate_threshold=15,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    request = rf.get("/api/test/n-plus-one/?count=15&duration_ms=1")

    response = handler(request.environ, lambda s, h: None)
    assert response.status_code == 200

    root_span = next(s for s in span_exporter.get_finished_spans() if s.kind.name == "SERVER")
    assert root_span.attributes.get("n_plus_one.detected") is True
    assert root_span.attributes.get("repeated_query.detected") is True
    assert root_span.attributes.get("n_plus_one.dominant_count") == 15
    assert root_span.attributes.get("n_plus_one.dominant_duration_ms") == 15
    assert root_span.attributes.get("n_plus_one.dominant_avg_duration_ms") == 1.0
    assert "n_plus_one.request_impact_pct" in root_span.attributes
    assert root_span.attributes.get("n_plus_one.candidate_count") >= 1
    assert root_span.attributes.get("n_plus_one.severity") in {"info", "low", "medium", "high"}
    assert root_span.attributes.get("n_plus_one.severity_reason")

    points = _get_metric_data_points(metric_reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 1
    assert points[0].value == 1
    assert points[0].attributes.get("http.route") == "/api/test/n-plus-one/"


def test_django_n_plus_one_unmatched_route_skips_metric():
    """Unmatched route or numeric status skips the affected-request metric."""
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    tp_trace.init(
        project_name="test-proj",
        cluster_name="test-cluster",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=1.0,
        metric_readers=[metric_reader],
        n_plus_one_duplicate_threshold=15,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    # 404 request where 15 queries were executed before 404 (e.g. middleware / lookup failure)
    # Simulate with unmatched URL:
    _token = init_request_db_tracker()
    try:
        for _ in range(15):
            record_request_db_query("SELECT 1 FROM missing")
    finally:
        pass  # keep tracker active for request

    request = rf.get("/unmatched/path/not/found/")
    response = handler(request.environ, lambda s, h: None)
    assert response.status_code == 404

    # No metric is recorded for the 404 / unmatched route.
    points = _get_metric_data_points(metric_reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 0


def test_django_distinct_queries_not_treated_as_n_plus_one():
    """30 distinct queries in one request should NOT trigger repeated query anomaly."""
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    tp_trace.init(
        project_name="test-proj",
        cluster_name="test-cluster",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=1.0,
        metric_readers=[metric_reader],
        n_plus_one_duplicate_threshold=15,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    request = rf.get("/api/test/multi-query/")

    response = handler(request.environ, lambda s, h: None)
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    root_span = next(s for s in spans if s.kind.name == "SERVER")
    assert "n_plus_one.detected" not in root_span.attributes

    points = _get_metric_data_points(metric_reader, "apm_repeated_query_affected_requests_total")
    assert len(points) == 0


def test_config_n_plus_one_parsing_and_defaults(monkeypatch):
    """Verify non-raising config parsing and environment variable aliases."""
    monkeypatch.setenv("TP_TRACE_N_PLUS_ONE_DUPLICATE_THRESHOLD", "20")
    monkeypatch.setenv("TP_TRACE_N_PLUS_ONE_MAX_FINGERPRINTS", "500")

    cfg = SDKConfig.from_env_and_kwargs()
    assert cfg.n_plus_one_duplicate_threshold == 20
    assert cfg.n_plus_one_max_fingerprints == 500

    # Explicit kwarg takes precedence over env
    cfg_override = SDKConfig.from_env_and_kwargs(n_plus_one_duplicate_threshold=5)
    assert cfg_override.n_plus_one_duplicate_threshold == 5

    # Invalid values fall back gracefully without raising
    monkeypatch.setenv("TP_TRACE_N_PLUS_ONE_DUPLICATE_THRESHOLD", "-5")
    monkeypatch.setenv("TP_TRACE_N_PLUS_ONE_MAX_FINGERPRINTS", "invalid_not_a_number")
    cfg_fallback = SDKConfig.from_env_and_kwargs()
    assert cfg_fallback.n_plus_one_duplicate_threshold == 15
    assert cfg_fallback.n_plus_one_max_fingerprints == 256


def test_multiple_repeated_query_patterns_formatted_in_span():
    """Verify multiple distinct repeated query shapes in one request are formatted clearly."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    tp_trace.init(
        project_name="test-proj",
        cluster_name="test-cluster",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=1.0,
        metric_readers=[metric_reader],
        n_plus_one_duplicate_threshold=15,
    )

    handler = WSGIHandler()
    rf = RequestFactory()
    request = rf.get("/api/test/repeated-patterns/")
    response = handler(request.environ, lambda s, h: None)
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    root_span = next(s for s in spans if s.kind.name == "SERVER")
    dominant_query = root_span.attributes.get("n_plus_one.dominant_query")
    assert "-- [Pattern #1" in dominant_query
    assert "-- [Pattern #2" in dominant_query
    assert "candidate_" in dominant_query


def test_wide_model_distinct_queries_do_not_false_positive():
    """Verify distinct queries on a wide model (>256 chars before WHERE) are not merged into N+1."""
    from tp_trace.integrations.postgres.cursor import _build_db_span_context

    # Build a SELECT column list that is 300+ characters long
    columns = ", ".join(f'"app_model"."field_{i}"' for i in range(25))
    base_select = f'SELECT {columns} FROM "app_model"'
    assert len(base_select) > 256

    token = init_request_db_tracker()
    try:
        # Simulate 15 different queries with distinct WHERE clauses
        for i in range(15):
            sql = f'{base_select} WHERE "app_model"."field_x_{i}" = 123'
            _, span_attrs, tracker_fingerprint = _build_db_span_context(sql, None)
            assert "db.statement.full_fingerprint" not in span_attrs
            record_request_db_query(tracker_fingerprint, 1.0)

        stats = get_request_db_query_stats(threshold=15)
        # 15 distinct query patterns of count 1 -> should NOT detect N+1
        assert stats.candidate_count == 0
        assert stats.dominant_count == 1
    finally:
        reset_request_db_tracker(token)
