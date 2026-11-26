"""Tests for exact per-request time breakdown tracking."""

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind

from tp_trace.breakdown import RequestBreakdownSpanProcessor, compute_request_breakdown
from tp_trace.config import SDKConfig
from tp_trace.metrics import create_red_metrics_recorder, set_metrics_recorder


def _ms(val: float) -> int:
    return int(val * 1_000_000)


def test_compute_request_breakdown_no_dependencies():
    res = compute_request_breakdown(_ms(0), _ms(100), [])
    assert res == {("django", "", ""): 100.0}


def test_compute_request_breakdown_sequential_dependencies():
    intervals = [
        (_ms(10), _ms(50), "postgres", "default", "default"),
        (_ms(60), _ms(75), "redis", "", ""),
    ]
    res = compute_request_breakdown(_ms(0), _ms(100), intervals)
    assert res[("postgres", "default", "default")] == 40.0
    assert res[("redis", "", "")] == 15.0
    assert res[("django", "", "")] == 45.0
    assert sum(res.values()) == 100.0


def test_compute_request_breakdown_overlapping_parallel_dependencies():
    # Two parallel 48ms DB calls during a 60ms request:
    # Overlap [0, 48] is split equally (24ms each), remaining 12ms is Django.
    intervals = [
        (_ms(0), _ms(48), "postgres", "default", ""),
        (_ms(0), _ms(48), "postgres", "slave1", ""),
    ]
    res = compute_request_breakdown(_ms(0), _ms(60), intervals)
    assert res[("postgres", "default", "")] == 24.0
    assert res[("postgres", "slave1", "")] == 24.0
    assert res[("django", "", "")] == 12.0
    assert sum(res.values()) == 60.0


def test_compute_request_breakdown_partial_overlap_and_clamping():
    # Request [10, 110] (100ms)
    # Child A: [0, 50] -> clamped [10, 50]
    # Child B: [30, 80] -> overlap [30, 50] split 10ms each; [50, 80] is 30ms to B
    # Total dep = 70ms ([10, 80]), Django = 30ms ([80, 110])
    intervals = [
        (_ms(0), _ms(50), "postgres", "default", ""),
        (_ms(30), _ms(80), "requests", "", ""),
    ]
    res = compute_request_breakdown(_ms(10), _ms(110), intervals)
    assert res[("postgres", "default", "")] == 30.0  # 20ms solo + 10ms shared
    assert res[("requests", "", "")] == 40.0  # 10ms shared + 30ms solo
    assert res[("django", "", "")] == 30.0
    assert sum(res.values()) == 100.0


def test_span_processor_records_django_http_and_excludes_non_http_and_pgbouncer():
    reader = InMemoryMetricReader()
    cfg = SDKConfig(project_name="proj-a", cluster_name="dev")
    recorder = create_red_metrics_recorder(config=cfg, metric_readers=[reader])
    set_metrics_recorder(recorder)

    provider = TracerProvider()
    proc = RequestBreakdownSpanProcessor(max_active_requests=100)
    provider.add_span_processor(proc)
    tracer = provider.get_tracer("test")

    try:
        # 1. Valid Django HTTP SERVER request with postgres, redis, and pgbouncer (excluded)
        with tracer.start_as_current_span(
            "GET /items/",
            kind=SpanKind.SERVER,
            attributes={
                "normalized.service": "django",
                "normalized.operation": "GET /items/",
                "project_name": "proj-a",
                "cluster_name": "dev",
            },
        ):
            with tracer.start_as_current_span(
                "SELECT",
                kind=SpanKind.CLIENT,
                attributes={
                    "normalized.service": "postgres",
                    "db.instance": "default",
                    "db.connection.pool": "pool1",
                },
            ):
                pass
            with tracer.start_as_current_span(
                "GET",
                kind=SpanKind.CLIENT,
                attributes={"normalized.service": "redis"},
            ):
                pass
            with tracer.start_as_current_span(
                "connect",
                kind=SpanKind.CLIENT,
                attributes={"normalized.service": "pgbouncer", "db.instance": "default"},
            ):
                pass

        # 2. django.command.* SERVER span should NOT be recorded
        with tracer.start_as_current_span(
            "django.command.migrate",
            kind=SpanKind.SERVER,
            attributes={
                "normalized.service": "django",
                "normalized.operation": "django.command.migrate",
            },
        ):
            with tracer.start_as_current_span(
                "SELECT",
                kind=SpanKind.CLIENT,
                attributes={"normalized.service": "postgres", "db.instance": "default"},
            ):
                pass

        # 3. Celery CONSUMER trace should NOT be recorded
        with tracer.start_as_current_span(
            "celery.run",
            kind=SpanKind.CONSUMER,
            attributes={"normalized.service": "celery", "normalized.operation": "celery.run"},
        ):
            with tracer.start_as_current_span(
                "SELECT",
                kind=SpanKind.CLIENT,
                attributes={"normalized.service": "postgres", "db.instance": "default"},
            ):
                pass

        metrics_data = reader.get_metrics_data()
        breakdown_points = []
        for rm in metrics_data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == "apm_request_time_breakdown_milliseconds_total":
                        breakdown_points.extend(metric.data.data_points)

        by_dep = {dp.attributes["dependency"]: dp.value for dp in breakdown_points}
        assert "django" in by_dep
        assert "postgres / default" in by_dep
        assert "redis" in by_dep
        assert "pgbouncer / default" not in by_dep
        assert all(dp.attributes["project_name"] == "proj-a" for dp in breakdown_points)
        assert all(dp.attributes["metric_source"] == "tp_trace_sdk" for dp in breakdown_points)
        assert all(dp.attributes["worker_id"] for dp in breakdown_points)
    finally:
        provider.shutdown()
        recorder.shutdown()
        set_metrics_recorder(None)


def test_span_processor_evicts_oldest_when_capped():
    proc = RequestBreakdownSpanProcessor(max_active_requests=2)
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer("test")

    s1 = tracer.start_span("r1", kind=SpanKind.SERVER)
    s2 = tracer.start_span("r2", kind=SpanKind.SERVER)
    s3 = tracer.start_span("r3", kind=SpanKind.SERVER)
    try:
        assert len(proc._server_buckets) == 2
        assert s1.get_span_context().span_id not in proc._server_buckets
        assert s2.get_span_context().span_id in proc._server_buckets
        assert s3.get_span_context().span_id in proc._server_buckets
    finally:
        s1.end()
        s2.end()
        s3.end()
        provider.shutdown()


def test_compute_request_breakdown_large_n_plus_one_is_fast():
    import time

    intervals = [(i * 100_000, i * 100_000 + 50_000, "postgres", "default", "") for i in range(5000)]
    t0 = time.monotonic()
    res = compute_request_breakdown(_ms(0), _ms(600), intervals)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.25
    assert abs(res[("postgres", "default", "")] - 250.0) < 1e-6
    assert abs(res[("django", "", "")] - 350.0) < 1e-6


def test_span_processor_caps_intervals_per_request():
    proc = RequestBreakdownSpanProcessor(max_intervals_per_request=3)
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer("test")

    try:
        with tracer.start_as_current_span("outer", kind=SpanKind.SERVER) as srv:
            sid = srv.get_span_context().span_id
            for _ in range(5):
                with tracer.start_as_current_span(
                    "q",
                    kind=SpanKind.CLIENT,
                    attributes={"normalized.service": "postgres", "db.instance": "default"},
                ):
                    pass
            assert len(proc._server_buckets[sid].intervals) == 3
    finally:
        provider.shutdown()


def test_span_processor_isolates_nested_server_spans_sharing_trace_id():
    reader = InMemoryMetricReader()
    cfg = SDKConfig(project_name="proj-nested", cluster_name="dev")
    recorder = create_red_metrics_recorder(config=cfg, metric_readers=[reader])
    set_metrics_recorder(recorder)

    provider = TracerProvider()
    proc = RequestBreakdownSpanProcessor()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer("test")

    try:
        with tracer.start_as_current_span(
            "GET /outer/",
            kind=SpanKind.SERVER,
            attributes={"normalized.service": "django", "normalized.operation": "GET /outer/"},
        ):
            with tracer.start_as_current_span(
                "redis_call",
                kind=SpanKind.CLIENT,
                attributes={"normalized.service": "redis"},
            ):
                pass
            # Inner SERVER span in the same trace (e.g. in-process test client or self-call)
            with tracer.start_as_current_span(
                "GET /inner/",
                kind=SpanKind.SERVER,
                attributes={"normalized.service": "django", "normalized.operation": "GET /inner/"},
            ):
                with tracer.start_as_current_span(
                    "inner_pg",
                    kind=SpanKind.CLIENT,
                    attributes={"normalized.service": "postgres", "db.instance": "default"},
                ):
                    pass

        metrics_data = reader.get_metrics_data()
        breakdown_points = []
        for rm in metrics_data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == "apm_request_time_breakdown_milliseconds_total":
                        breakdown_points.extend(metric.data.data_points)

        deps = {dp.attributes["dependency"] for dp in breakdown_points}
        assert deps == {"django", "redis", "postgres / default"}
    finally:
        provider.shutdown()
        recorder.shutdown()
        set_metrics_recorder(None)
