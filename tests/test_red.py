"""Unit tests for RED metrics span processing and sampling gates in tp_trace.

Validates that:
1. RedSpanProcessor captures 100% of finished dependency spans (Postgres, Redis, Requests, Celery)
   and updates Prometheus RED metrics (calls_total, duration_milliseconds) regardless of sample rate.
2. Server spans for Django are captured alongside dependencies by RedSpanProcessor.
3. Internal framework spans are ignored.
4. Ignored requests (e.g. health checks) suppress dependency metric recording.
5. High-cardinality operations and routes collapse to '__other__' once configured thresholds are reached.
6. SampledOnlySpanProcessor drops RECORD_ONLY spans from export while forwarding sampled spans.
7. End-to-end trace sampling at 0.0% still produces 100% dependency RED metrics while exporting 0 traces.
"""

from unittest.mock import MagicMock
import pytest
from opentelemetry.context import Context
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import (
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
)

import tp_trace
from tp_trace.red import RedSpanProcessor, SampledOnlySpanProcessor


def _create_mock_span(
    name: str = "test-span",
    kind: SpanKind = SpanKind.CLIENT,
    attributes: dict = None,
    sampled: bool = True,
    status_code: StatusCode = StatusCode.OK,
    duration_ms: float = 15.0,
) -> ReadableSpan:
    """Create a mock ReadableSpan with standard OpenTelemetry SDK interfaces."""
    attrs = dict(attributes or {})
    start_time_ns = 1_000_000_000
    end_time_ns = start_time_ns + int(duration_ms * 1_000_000)

    trace_flags = TraceFlags(TraceFlags.SAMPLED if sampled else 0)
    context = SpanContext(
        trace_id=12345678901234567890123456789012,
        span_id=1234567890123456,
        is_remote=False,
        trace_flags=trace_flags,
        trace_state=TraceState(),
    )

    span = MagicMock(spec=ReadableSpan)
    span.name = name
    span.kind = kind
    span.attributes = attrs
    span.start_time = start_time_ns
    span.end_time = end_time_ns
    span.status = Status(status_code=status_code)
    span.get_span_context.return_value = context
    return span


def _get_metric_points(metric_reader: InMemoryMetricReader, metric_name: str):
    """Extract data points for a named metric from InMemoryMetricReader."""
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


@pytest.fixture(autouse=True)
def clean_state():
    tp_trace._reset_for_testing()
    yield
    tp_trace._reset_for_testing()


def test_red_span_processor_records_postgres_span():
    """Verify RedSpanProcessor records calls_total and duration for Postgres queries."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
    )

    processor = RedSpanProcessor()
    span = _create_mock_span(
        name="SELECT test_table",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system": "postgresql",
            "db.operation": "SELECT",
            "db.name": "defaultdb",
            "net.peer.name": "127.0.0.1",
            "http.route": "/api/users/",
        },
        duration_ms=42.0,
    )

    processor.on_end(span)

    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) == 1
    pt = calls[0]
    assert pt.value == 1
    assert pt.attributes["normalized_service"] == "postgres"
    assert pt.attributes["normalized_operation"] == "SELECT"
    assert pt.attributes["error"] == "false"
    assert pt.attributes["span_kind"] == "SPAN_KIND_CLIENT"
    assert pt.attributes["http.route"] == "/api/users/"

    durations = _get_metric_points(reader, "duration_milliseconds")
    assert len(durations) == 1
    d_pt = durations[0]
    assert d_pt.count == 1
    assert d_pt.sum == pytest.approx(42.0, rel=1e-2)


def test_red_span_processor_records_redis_span():
    """Verify RedSpanProcessor records Redis operations."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
    )

    processor = RedSpanProcessor()
    span = _create_mock_span(
        name="GET user:123",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system": "redis",
            "db.operation": "GET",
        },
        duration_ms=2.5,
    )

    processor.on_end(span)

    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) == 1
    assert calls[0].attributes["normalized_service"] == "redis"
    assert calls[0].attributes["normalized_operation"] == "GET"
    assert calls[0].attributes["error"] == "false"


def test_red_span_processor_records_celery_spans():
    """Verify RedSpanProcessor records Celery PRODUCER and CONSUMER spans."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
    )

    processor = RedSpanProcessor()

    # Task publish
    producer_span = _create_mock_span(
        name="tasks.send_email apply_async",
        kind=SpanKind.PRODUCER,
        attributes={
            "normalized.service": "celery",
            "normalized.operation": "tasks.send_email",
        },
    )
    processor.on_end(producer_span)

    # Task execution
    consumer_span = _create_mock_span(
        name="tasks.send_email execute",
        kind=SpanKind.CONSUMER,
        attributes={
            "normalized.service": "celery",
            "normalized.operation": "tasks.send_email",
        },
    )
    processor.on_end(consumer_span)

    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) == 2
    services = {c.attributes["normalized_service"] for c in calls}
    kinds = {c.attributes["span_kind"] for c in calls}
    assert services == {"celery"}
    assert kinds == {"SPAN_KIND_PRODUCER", "SPAN_KIND_CONSUMER"}


def test_red_span_processor_messaging_service_identification():
    """Verify messaging spans use messaging.system and do not mislabel Kafka/generic as celery."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
    )

    processor = RedSpanProcessor()

    # Kafka producer span
    kafka_span = _create_mock_span(
        name="orders publish",
        kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "kafka",
            "messaging.destination": "orders-topic",
        },
    )
    processor.on_end(kafka_span)

    # Generic consumer span without celery indicators
    generic_span = _create_mock_span(
        name="worker receive",
        kind=SpanKind.CONSUMER,
        attributes={},
    )
    processor.on_end(generic_span)

    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) == 2
    services = {c.attributes["normalized_service"] for c in calls}
    assert "kafka" in services
    assert "messaging" in services
    assert "celery" not in services


def test_red_span_processor_records_error_status():
    """Verify error spans are marked with status='ERROR' and error flag."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
    )

    processor = RedSpanProcessor()
    span = _create_mock_span(
        name="HTTP POST /checkout",
        kind=SpanKind.CLIENT,
        attributes={
            "peer.service": "payment-gateway",
            "http.method": "POST",
            "http.status_code": 500,
            "error": True,
        },
        status_code=StatusCode.ERROR,
    )

    processor.on_end(span)

    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) == 1
    assert calls[0].attributes["error"] == "true"


def test_red_span_processor_skips_internal_spans_and_records_server_spans():
    """Verify internal spans are skipped while Django SERVER spans are recorded."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
    )

    processor = RedSpanProcessor()

    # Internal span is ignored
    internal_span = _create_mock_span(
        name="internal_helper",
        kind=SpanKind.INTERNAL,
        attributes={"some.attr": "value"},
    )
    processor.on_end(internal_span)

    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) == 0

    # Django SERVER span is recorded
    server_span = _create_mock_span(
        name="GET /api/test/",
        kind=SpanKind.SERVER,
        attributes={
            "normalized.service": "django",
            "django.route": "/api/test/",
            "http.status_code": 200,
        },
    )
    processor.on_end(server_span)

    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) == 1
    assert calls[0].attributes["normalized_service"] == "django"
    assert calls[0].attributes["span_kind"] == "SPAN_KIND_SERVER"
    assert calls[0].attributes["http.route"] == "/api/test/"
    assert calls[0].attributes["error"] == "false"


def test_red_span_processor_server_span_missing_status_code():
    """Verify server span without http.status_code records 500 if error and skips if ok."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
    )

    processor = RedSpanProcessor()

    # OK server span without status code -> skipped
    ok_span = _create_mock_span(
        name="GET /incomplete",
        kind=SpanKind.SERVER,
        attributes={"django.route": "/incomplete"},
        status_code=StatusCode.OK,
    )
    processor.on_end(ok_span)
    assert len(_get_metric_points(reader, "calls_total")) == 0

    # ERROR server span without status code -> defaults to 500 error
    error_span = _create_mock_span(
        name="GET /crash",
        kind=SpanKind.SERVER,
        attributes={"django.route": "/crash"},
        status_code=StatusCode.ERROR,
    )
    processor.on_end(error_span)
    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) == 1
    assert calls[0].attributes["status_code"] == "500"
    assert calls[0].attributes["error"] == "true"


def test_red_span_processor_skips_ignored_requests():
    """Verify that child dependency spans executed during ignored requests are not recorded."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
        ignore_endpoints=["/health*"],
    )

    tracer = tp_trace.get_tracer("test")

    with tracer.start_as_current_span("GET /health", attributes={"http.target": "/health"}):
        with tracer.start_as_current_span(
            "SELECT health_status",
            kind=SpanKind.CLIENT,
            attributes={
                "db.system": "postgresql",
                "db.operation": "SELECT",
            },
        ):
            pass

    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) == 0


def test_remote_parent_unsampled_checks_ignore_endpoints():
    """Verify RemoteParentAwareSampler evaluates ignore_endpoints for both sampled and unsampled remote parents."""
    from tp_trace.sampler import RemoteParentAwareSampler, TpTraceRuleBasedSampler
    from opentelemetry.sdk.trace.sampling import Decision

    root = TpTraceRuleBasedSampler(global_sample_rate=0.0, ignore_endpoints=["/health*", "/ping"])
    unsampled_remote = RemoteParentAwareSampler(root_sampler=root, default_sampled=False)
    sampled_remote = RemoteParentAwareSampler(root_sampler=root, default_sampled=True)

    # 1. Ignored path on unsampled remote parent -> DROP
    res1 = unsampled_remote.should_sample(None, 1234, "GET /health", attributes={"http.target": "/health"})
    assert res1.decision == Decision.DROP

    # 2. Ignored path on sampled remote parent -> DROP
    res2 = sampled_remote.should_sample(None, 1234, "GET /ping", attributes={"http.target": "/ping"})
    assert res2.decision == Decision.DROP

    # 3. Normal path on unsampled remote parent -> RECORD_ONLY
    res3 = unsampled_remote.should_sample(None, 1234, "GET /api/orders/", attributes={"http.target": "/api/orders/"})
    assert res3.decision == Decision.RECORD_ONLY

    # 4. Normal path on sampled remote parent -> RECORD_AND_SAMPLE
    res4 = sampled_remote.should_sample(None, 1234, "GET /api/orders/", attributes={"http.target": "/api/orders/"})
    assert res4.decision == Decision.RECORD_AND_SAMPLE


def test_e2e_unsampled_traceparent_to_ignored_endpoint_drops_spans_and_metrics():
    """Verify an incoming request with an unsampled traceparent to an ignored path drops spans and emits 0 metrics."""
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="unsampled-parent-test",
        exporter=exporter,
        export_batch=False,
        sample_rate=0.0,
        ignore_endpoints=["/health*", "/ping"],
        metric_readers=[reader],
    )

    propagator = TraceContextTextMapPropagator()
    # W3C traceparent ending in 00 (unsampled remote parent)
    carrier = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"}
    ctx = propagator.extract(carrier)

    tracer = tp_trace.get_tracer("test")

    # 1. Ignored endpoint with unsampled remote parent: must drop root and child
    with tracer.start_as_current_span("GET /health", context=ctx, attributes={"http.target": "/health"}) as root:
        assert not root.is_recording()
        with tracer.start_as_current_span(
            "SELECT 1",
            kind=SpanKind.CLIENT,
            attributes={"db.system": "postgresql", "db.operation": "SELECT"},
        ):
            pass

    calls_health = _get_metric_points(reader, "calls_total")
    assert len(calls_health) == 0
    assert len(exporter.get_finished_spans()) == 0

    # 2. Normal business endpoint with unsampled remote parent: records RED metrics, drops trace export
    with tracer.start_as_current_span("GET /api/orders/", context=ctx, attributes={"http.target": "/api/orders/"}) as root:
        assert root.is_recording()
        with tracer.start_as_current_span(
            "SELECT 1",
            kind=SpanKind.CLIENT,
            attributes={"db.system": "postgresql", "db.operation": "SELECT"},
        ):
            pass

    calls_orders = _get_metric_points(reader, "calls_total")
    assert len(calls_orders) == 1
    assert len(exporter.get_finished_spans()) == 0



def test_red_span_processor_cardinality_limits():
    """Verify that operations and routes exceeding cardinality limits collapse to __other__."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
    )

    # Limit cardinality to 3 per service and 3 routes
    processor = RedSpanProcessor(cardinality_limit_per_service=3, route_limit=3)

    for i in range(6):
        span = _create_mock_span(
            name=f"query_{i}",
            kind=SpanKind.CLIENT,
            attributes={
                "db.system": "redis",
                "db.operation": f"CMD_{i}",
                "http.route": f"/route/{i}",
            },
        )
        processor.on_end(span)

    calls = _get_metric_points(reader, "calls_total")
    assert len(calls) > 0

    operations = {c.attributes["normalized_operation"] for c in calls}
    routes = {c.attributes.get("http.route") for c in calls}

    # At most 3 distinct + "__other__"
    assert "__other__" in operations
    assert "__other__" in routes
    assert len(operations) <= 4
    assert len(routes) <= 4


def test_red_span_processor_service_cardinality_limits():
    """Verify that distinct services exceeding service_limit collapse to __other__."""
    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="test-red",
        metric_readers=[reader],
    )

    # Limit services to 2
    processor = RedSpanProcessor(service_limit=2)

    for s_id in ["svc_a", "svc_b", "svc_c", "svc_d"]:
        span = _create_mock_span(
            name="task",
            kind=SpanKind.CLIENT,
            attributes={"normalized.service": s_id, "db.operation": "run"},
        )
        processor.on_end(span)

    calls = _get_metric_points(reader, "calls_total")
    services = {c.attributes["normalized_service"] for c in calls}
    assert "__other__" in services
    assert len(services) <= 3


def test_sampled_only_span_processor_drops_unsampled():
    """Verify SampledOnlySpanProcessor forwards sampled spans and drops unsampled spans."""
    delegate = MagicMock(spec=SpanProcessor)
    processor = SampledOnlySpanProcessor(delegate)

    sampled_span = _create_mock_span(name="sampled", sampled=True)
    unsampled_span = _create_mock_span(name="unsampled", sampled=False)

    ctx = Context()

    # on_start: forwarded for sampled, dropped for unsampled
    processor.on_start(sampled_span, ctx)
    delegate.on_start.assert_called_once_with(sampled_span, parent_context=ctx)
    delegate.on_start.reset_mock()

    processor.on_start(unsampled_span, ctx)
    delegate.on_start.assert_not_called()

    # on_end: forwarded for sampled, dropped for unsampled
    processor.on_end(sampled_span)
    assert delegate.on_end.call_count == 1
    assert delegate.on_end.call_args[0][0] == sampled_span

    processor.on_end(unsampled_span)
    assert delegate.on_end.call_count == 1  # Unchanged!

    # Test lifecycle passthrough
    processor.force_flush(5000)
    delegate.force_flush.assert_called_once_with(5000)

    processor.shutdown()
    delegate.shutdown.assert_called_once()


def test_e2e_dependency_red_metrics_with_zero_sample_rate():
    """Verify that even with sample_rate=0.0, dependency spans update RED metrics while 0 traces are exported."""
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    tp_trace.init(
        project_name="zero-sample-test",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=0.0,  # 0% trace sampling!
        metric_readers=[metric_reader],
    )

    tracer = tp_trace.get_tracer("test")

    # Simulate 10 database client spans
    for i in range(10):
        with tracer.start_as_current_span(
            "SELECT users",
            kind=SpanKind.CLIENT,
            attributes={
                "db.system": "postgresql",
                "db.operation": "SELECT",
                "db.name": "defaultdb",
            },
        ):
            pass

    # 1. Traces: 0 spans exported!
    finished_spans = span_exporter.get_finished_spans()
    assert len(finished_spans) == 0, f"Expected 0 exported spans at sample_rate=0.0, got {len(finished_spans)}"

    # 2. RED Metrics: All 10 recorded!
    calls = _get_metric_points(metric_reader, "calls_total")
    assert len(calls) > 0
    total_calls = sum(pt.value for pt in calls if pt.attributes.get("normalized_service") == "postgres")
    assert total_calls == 10, f"Expected 10 calls recorded in RED metrics, got {total_calls}"

    durations = _get_metric_points(metric_reader, "duration_milliseconds")
    total_durations = sum(pt.count for pt in durations if pt.attributes.get("normalized_service") == "postgres")
    assert total_durations == 10


def test_ignored_request_does_not_leak_to_subsequent_request():
    """Verify that an ignored request (e.g. /health) does not suppress metrics for later requests on the same context."""
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    tp_trace.init(
        project_name="leak-test",
        exporter=span_exporter,
        export_batch=False,
        sample_rate=0.0,
        ignore_endpoints=["/health*", "/readyz"],
        metric_readers=[metric_reader],
    )

    tracer = tp_trace.get_tracer("test")

    # 1. First: Execute an ignored request (/healthz) with child database query
    with tracer.start_as_current_span("GET /healthz", attributes={"http.target": "/healthz"}):
        with tracer.start_as_current_span(
            "SELECT 1",
            kind=SpanKind.CLIENT,
            attributes={"db.system": "postgresql", "db.operation": "SELECT"},
        ):
            pass

    # No metrics should be recorded for health check or its child
    calls_after_health = _get_metric_points(metric_reader, "calls_total")
    assert len(calls_after_health) == 0

    # 2. Second: Immediately execute a normal business request (/api/orders/) on the SAME thread
    with tracer.start_as_current_span("GET /api/orders/", attributes={"http.target": "/api/orders/"}):
        with tracer.start_as_current_span(
            "SELECT orders",
            kind=SpanKind.CLIENT,
            attributes={"db.system": "postgresql", "db.operation": "SELECT", "db.name": "defaultdb"},
        ):
            pass

    # Metrics MUST be recorded for the child query under the normal request!
    calls = _get_metric_points(metric_reader, "calls_total")
    assert len(calls) > 0
    recorded = sum(pt.value for pt in calls if pt.attributes.get("normalized_service") == "postgres")
    assert recorded == 1, f"Expected 1 recorded call for normal request after ignored request, got {recorded}"


def test_red_span_processor_overhead_benchmark():
    """Verify that RedSpanProcessor execution is fast and adds minimal CPU overhead per span."""
    import time

    reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="bench-test",
        metric_readers=[reader],
    )

    processor = RedSpanProcessor()
    span = _create_mock_span(
        name="SELECT test_table",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system": "postgresql",
            "db.operation": "SELECT",
            "db.name": "defaultdb",
        },
    )

    # Warmup
    for _ in range(50):
        processor.on_end(span)

    start = time.perf_counter()
    iterations = 5000
    for _ in range(iterations):
        processor.on_end(span)
    elapsed = time.perf_counter() - start

    avg_microseconds = (elapsed / iterations) * 1_000_000
    # Must comfortably execute within 200 microseconds per span on shared CI runners
    assert avg_microseconds < 200.0, f"Average overhead {avg_microseconds:.2f}µs exceeded 200µs"


def test_exported_dependency_spans_carry_tp_trace_red_attribute():
    """Verify exported dependency and server spans carry tp_trace.red=1 so Collector drops them."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="tag-assert-test",
        exporter=exporter,
        export_batch=False,
        sample_rate=1.0,
    )

    tracer = tp_trace.get_tracer("test")

    with tracer.start_as_current_span(
        "GET /api/users",
        kind=SpanKind.SERVER,
        attributes={"http.method": "GET", "django.route": "/api/users"},
    ):
        pass

    with tracer.start_as_current_span(
        "SELECT users",
        kind=SpanKind.CLIENT,
        attributes={"db.system": "postgresql", "db.operation": "SELECT"},
    ):
        pass

    with tracer.start_as_current_span(
        "internal_step",
        kind=SpanKind.INTERNAL,
    ):
        pass

    spans = exporter.get_finished_spans()
    server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
    client_spans = [s for s in spans if s.kind == SpanKind.CLIENT]
    internal_spans = [s for s in spans if s.kind == SpanKind.INTERNAL]

    assert len(server_spans) == 1
    assert server_spans[0].attributes.get("tp_trace.red") == 1

    assert len(client_spans) == 1
    assert client_spans[0].attributes.get("tp_trace.red") == 1

    assert len(internal_spans) == 1
    assert "tp_trace.red" not in internal_spans[0].attributes


