"""Tests for Phase 2: Independent RED Metrics Recording.

Verifies that RED metrics (calls_total, duration_milliseconds) are accurately recorded
for 100% of requests regardless of trace sampling rates (1.0, 0.1, 0.01, 0.0),
including exceptions, early responses, and that metric failures never break requests.
"""

import os
import socket
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
    assert "worker_id" in attrs
    assert attrs["worker_id"] == f"{socket.gethostname()}:{os.getpid()}"


def test_worker_identity_contains_hostname_and_pid():
    """Verify production default worker identity includes hostname and pid."""
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="prod-identity-test",
        metric_readers=[metric_reader],
        export_batch=False,
    )
    handler = WSGIHandler()
    rf = RequestFactory()
    req = rf.get("/api/test/ok/")
    handler.get_response(req)

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1
    attrs = calls_points[0].attributes
    worker_id = attrs["worker_id"]
    assert worker_id == f"{socket.gethostname()}:{os.getpid()}"


def test_worker_identity_stable_inside_same_process():
    """Within the same process, worker identity remains stable across multiple requests."""
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="stable-identity-test",
        metric_readers=[metric_reader],
        export_batch=False,
    )
    handler = WSGIHandler()
    rf = RequestFactory()

    handler.get_response(rf.get("/api/test/ok/"))
    handler.get_response(rf.get("/api/test/ok/"))

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1  # Same identity increments the same series
    assert calls_points[0].value == 2


def test_worker_identity_changes_after_fork(monkeypatch):
    """When a process forks (PID changes), worker identity refreshes with the new PID."""
    from tp_trace.metrics import get_process_worker_identity

    orig_identity = get_process_worker_identity()
    assert orig_identity == f"{socket.gethostname()}:{os.getpid()}"

    # Simulate fork to new PID
    new_pid = os.getpid() + 1000
    monkeypatch.setattr(os, "getpid", lambda: new_pid)

    forked_identity = get_process_worker_identity()
    assert forked_identity != orig_identity
    assert forked_identity == f"{socket.gethostname()}:{new_pid}"


def test_worker_identity_thread_safe_concurrent_initialization(monkeypatch):
    """Verify concurrent calls from multiple threads initialize identity safely without racing."""
    import concurrent.futures
    import tp_trace.metrics as tm

    # Reset cache to simulate cold start
    monkeypatch.setattr(tm, "_PROCESS_IDENTITY", None, raising=False)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(tm.get_process_worker_identity) for _ in range(50)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    expected = f"{socket.gethostname()}:{os.getpid()}"
    assert len(results) == 50
    assert all(r == expected for r in results)


def test_worker_identity_refresh_does_not_publish_parent_identity(monkeypatch):
    """A second thread refreshing after fork must never read the parent's identity."""
    import concurrent.futures
    import threading
    import tp_trace.metrics as tm

    parent_identity = tm.get_process_worker_identity()
    child_pid = os.getpid() + 1000
    monkeypatch.setattr(os, "getpid", lambda: child_pid)
    initializing = threading.Event()
    release = threading.Event()
    real_getenv = os.environ.get

    def slow_prefix(key, default=None):
        if key == "OTEL_SERVICE_INSTANCE_ID" and threading.current_thread().name == "initializer":
            initializing.set()
            assert release.wait(3)
        return real_getenv(key, default)

    monkeypatch.setattr(os.environ, "get", slow_prefix)
    results = []
    initializer = threading.Thread(target=lambda: results.append(tm.get_process_worker_identity()), name="initializer")
    initializer.start()
    try:
        assert initializing.wait(3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            second = executor.submit(tm.get_process_worker_identity)
            try:
                identity = second.result(timeout=1)
            finally:
                release.set()
        assert identity != parent_identity
        assert identity.endswith(f":{child_pid}")
    finally:
        release.set()
        initializer.join(timeout=3)


def test_worker_identity_fork_during_initialization():
    """Forking during identity construction must not leave a child blocked."""
    import subprocess
    import sys
    import textwrap

    if not hasattr(os, "fork"):
        pytest.skip("os.fork is not available on this platform")
    # Isolate the fork and timeout from pytest and its SDK background threads.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import os
            import signal
            import threading
            import tp_trace.metrics as metrics

            entered = threading.Event()
            release = threading.Event()
            real_get = os.environ.get

            def slow_get(key, default=None):
                if key == "OTEL_SERVICE_INSTANCE_ID" and threading.current_thread().name == "initializer":
                    entered.set()
                    release.wait(5)
                return real_get(key, default)

            os.environ.get = slow_get
            thread = threading.Thread(target=metrics.get_process_worker_identity, name="initializer")
            thread.start()
            assert entered.wait(3)
            child = os.fork()
            if child == 0:
                signal.alarm(2)
                identity = metrics.get_process_worker_identity()
                os._exit(0 if identity.endswith(":" + str(os.getpid())) else 1)
            release.set()
            thread.join(3)
            _, status = os.waitpid(child, 0)
            assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, status
        """),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_real_fork_produces_distinct_worker_identities():
    """Verify that an actual OS fork generates distinct identities in parent and child."""
    if not hasattr(os, "fork"):
        pytest.skip("os.fork is not available on this platform")

    from tp_trace.metrics import get_process_worker_identity

    parent_id = get_process_worker_identity()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        child_id = get_process_worker_identity()
        with os.fdopen(w, "w") as f:
            f.write(child_id)
        os._exit(0)
    else:
        os.close(w)
        with os.fdopen(r, "r") as f:
            child_id = f.read()
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        assert child_id != parent_id
        assert child_id.endswith(f":{pid}")


def test_worker_identity_unique_with_shared_env_instance_id(monkeypatch):
    """Verify distinct worker PIDs get unique identities even when OTEL_SERVICE_INSTANCE_ID is shared."""
    from tp_trace.metrics import get_metrics_recorder

    monkeypatch.setenv("OTEL_SERVICE_INSTANCE_ID", "shared-container-env")
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="worker-env-test",
        metric_readers=[metric_reader],
        export_batch=False,
    )
    recorder = get_metrics_recorder()
    assert recorder is not None

    # Simulate Worker 1 with shared env
    monkeypatch.setattr(os, "getpid", lambda: 20001)
    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=10.0,
    )

    # Simulate Worker 2 with same shared env
    monkeypatch.setattr(os, "getpid", lambda: 20002)
    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=20.0,
    )

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 2
    worker_ids = [pt.attributes.get("worker_id") for pt in calls_points]
    assert worker_ids[0] != worker_ids[1]
    assert worker_ids[0] == "shared-container-env:20001"
    assert worker_ids[1] == "shared-container-env:20002"


def test_request_attributes_cannot_override_worker_identity():
    """Verify request extra tags cannot override process worker_id."""
    from tp_trace.metrics import get_metrics_recorder

    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="tamper-test",
        metric_readers=[metric_reader],
        export_batch=False,
    )
    recorder = get_metrics_recorder()
    assert recorder is not None

    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=12.0,
        extra_attributes={
            "worker_id": "malicious-worker-override",
        },
    )

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1
    attrs = calls_points[0].attributes
    assert attrs["worker_id"] != "malicious-worker-override"
    assert attrs["worker_id"] == f"{socket.gethostname()}:{os.getpid()}"


def test_multiworker_independent_streams(monkeypatch):
    """Verify that multiple worker processes emit separate worker_id series rather than colliding."""
    from tp_trace.metrics import get_metrics_recorder

    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="worker-test",
        metric_readers=[metric_reader],
        export_batch=False,
    )
    recorder = get_metrics_recorder()
    assert recorder is not None

    # Simulate Worker 1 (pid 10001)
    monkeypatch.setattr(os, "getpid", lambda: 10001)
    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=15.0,
    )
    # Simulate Worker 2 (pid 10002, e.g. forked child process)
    monkeypatch.setattr(os, "getpid", lambda: 10002)
    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=25.0,
    )

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 2
    worker_ids = {pt.attributes.get("worker_id") for pt in calls_points}
    assert len(worker_ids) == 2
    assert any(wid.endswith(":10001") for wid in worker_ids)
    assert any(wid.endswith(":10002") for wid in worker_ids)


def test_worker_identity_hostname_failure_fallback(monkeypatch):
    """When hostname resolution raises an exception, identity safely falls back to localhost."""
    import tp_trace.metrics as tm

    monkeypatch.setattr(tm, "_PROCESS_IDENTITY", None, raising=False)
    monkeypatch.setattr(tm, "_HOSTNAME", None, raising=False)

    def fail_hostname():
        raise OSError("DNS resolution failure")

    monkeypatch.setattr(socket, "gethostname", fail_hostname)
    identity = tm.get_process_worker_identity()
    assert identity == f"localhost:{os.getpid()}"


def test_worker_identity_with_config_service_instance_id(monkeypatch):
    """Verify service_instance_id passed via init is propagated to worker_id."""
    import tp_trace.metrics as tm
    from tp_trace.metrics import get_metrics_recorder

    monkeypatch.setattr(tm, "_PROCESS_IDENTITY", None, raising=False)
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="config-instance-test",
        service_instance_id="my-custom-prefix",
        metric_readers=[metric_reader],
        export_batch=False,
    )
    recorder = get_metrics_recorder()
    assert recorder is not None

    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=10.0,
    )

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1
    attrs = calls_points[0].attributes
    assert attrs["worker_id"] == f"my-custom-prefix:{os.getpid()}"


def test_worker_identity_with_tp_trace_service_instance_id_env(monkeypatch):
    """Verify TP_TRACE_SERVICE_INSTANCE_ID environment variable overrides worker_id prefix."""
    import tp_trace.metrics as tm
    from tp_trace.metrics import get_metrics_recorder

    monkeypatch.setattr(tm, "_PROCESS_IDENTITY", None, raising=False)
    monkeypatch.setenv("TP_TRACE_SERVICE_INSTANCE_ID", "tp-env-prefix")
    metric_reader = InMemoryMetricReader()
    tp_trace.init(
        project_name="tp-env-test",
        metric_readers=[metric_reader],
        export_batch=False,
    )
    recorder = get_metrics_recorder()
    assert recorder is not None

    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=10.0,
    )

    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1
    attrs = calls_points[0].attributes
    assert attrs["worker_id"] == f"tp-env-prefix:{os.getpid()}"


def test_worker_identity_already_populated_cache_respects_new_prefix():
    """Verify that calling get_process_worker_identity with a new prefix does not return an existing cached prefix."""
    import tp_trace.metrics as tm

    pid = os.getpid()
    id_a = tm.get_process_worker_identity(prefix="service-a")
    assert id_a == f"service-a:{pid}"

    # With cache populated with service-a, requesting service-b must respect service-b
    id_b = tm.get_process_worker_identity(prefix="service-b")
    assert id_b == f"service-b:{pid}"
    assert id_b != id_a

    # Requesting service-a again should retrieve cached service-a
    assert tm.get_process_worker_identity(prefix="service-a") == f"service-a:{pid}"


def test_two_recorders_with_different_prefixes_in_same_process():
    """Two independent recorders in the same process must maintain distinct worker_id streams."""
    from opentelemetry.sdk.metrics import MeterProvider
    from tp_trace.config import SDKConfig
    from tp_trace.metrics import RedMetricsRecorder

    reader_a = InMemoryMetricReader()
    provider_a = MeterProvider(metric_readers=[reader_a])
    config_a = SDKConfig(project_name="svc-a", service_instance_id="prefix-alpha")
    recorder_a = RedMetricsRecorder(meter_provider=provider_a, config=config_a)

    reader_b = InMemoryMetricReader()
    provider_b = MeterProvider(metric_readers=[reader_b])
    config_b = SDKConfig(project_name="svc-b", service_instance_id="prefix-beta")
    recorder_b = RedMetricsRecorder(meter_provider=provider_b, config=config_b)

    pid = os.getpid()

    # Interleaved requests across both recorders
    for _ in range(3):
        recorder_a.record_request(
            method="GET",
            route="/api/a/",
            status_code=200,
            error=False,
            duration_ms=10.0,
        )
        recorder_b.record_request(
            method="POST",
            route="/api/b/",
            status_code=201,
            error=False,
            duration_ms=20.0,
        )

    pts_a = _get_metric_data_points(reader_a, "calls_total")
    assert len(pts_a) == 1
    assert pts_a[0].attributes["worker_id"] == f"prefix-alpha:{pid}"
    assert pts_a[0].value == 3

    pts_b = _get_metric_data_points(reader_b, "calls_total")
    assert len(pts_b) == 1
    assert pts_b[0].attributes["worker_id"] == f"prefix-beta:{pid}"
    assert pts_b[0].value == 3


def test_service_instance_id_configuration_precedence(monkeypatch):
    """Verify configuration precedence: explicit kwarg > TP_TRACE_* > OTEL_* > default None."""
    from tp_trace.config import SDKConfig

    # 1. Explicit kwarg overrides all env vars
    monkeypatch.setenv("TP_TRACE_SERVICE_INSTANCE_ID", "env-tp")
    monkeypatch.setenv("OTEL_SERVICE_INSTANCE_ID", "env-otel")
    cfg = SDKConfig.from_env_and_kwargs(service_instance_id="explicit-pfx")
    assert cfg.service_instance_id == "explicit-pfx"

    # 2. TP_TRACE_SERVICE_INSTANCE_ID overrides OTEL_SERVICE_INSTANCE_ID
    cfg = SDKConfig.from_env_and_kwargs()
    assert cfg.service_instance_id == "env-tp"

    # 3. OTEL_SERVICE_INSTANCE_ID applies when TP_TRACE_* is absent
    monkeypatch.delenv("TP_TRACE_SERVICE_INSTANCE_ID")
    cfg = SDKConfig.from_env_and_kwargs()
    assert cfg.service_instance_id == "env-otel"

    # 4. Default is None when no options/env are configured
    monkeypatch.delenv("OTEL_SERVICE_INSTANCE_ID")
    cfg = SDKConfig.from_env_and_kwargs()
    assert cfg.service_instance_id is None


def test_service_instance_id_sets_worker_id_not_resource_instance_id():
    """service_instance_id configures worker_id prefix, preserving Resource service.instance.id."""
    from tp_trace.metrics import get_metrics_recorder

    metric_reader = InMemoryMetricReader()
    provider = tp_trace.init(
        project_name="res-id-isolation-test",
        service_instance_id="worker-prefix-only",
        metric_readers=[metric_reader],
        export_batch=False,
    )
    recorder = get_metrics_recorder()
    assert recorder is not None

    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=10.0,
    )

    # 1. Datapoint worker_id uses the configured prefix
    calls_points = _get_metric_data_points(metric_reader, "calls_total")
    assert len(calls_points) == 1
    attrs = calls_points[0].attributes
    assert attrs["worker_id"] == f"worker-prefix-only:{os.getpid()}"

    # 2. Resource service.instance.id is NOT modified by service_instance_id
    res_attrs = provider.resource.attributes
    assert "service.instance.id" in res_attrs
    assert res_attrs["service.instance.id"] == f"{socket.gethostname()}-{os.getpid()}"
    assert not res_attrs["service.instance.id"].startswith("worker-prefix-only")


def test_no_config_recorder_tp_trace_service_instance_id_env(monkeypatch):
    """create_red_metrics_recorder without config honors TP_TRACE_SERVICE_INSTANCE_ID."""
    from tp_trace.metrics import create_red_metrics_recorder

    monkeypatch.setenv("TP_TRACE_SERVICE_INSTANCE_ID", "tp-noconf")
    reader = InMemoryMetricReader()
    recorder = create_red_metrics_recorder(config=None, metric_readers=[reader])

    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=10.0,
    )

    pts = _get_metric_data_points(reader, "calls_total")
    assert len(pts) == 1
    assert pts[0].attributes["worker_id"] == f"tp-noconf:{os.getpid()}"


def test_no_config_recorder_otel_service_instance_id_env(monkeypatch):
    """create_red_metrics_recorder without config honors OTEL_SERVICE_INSTANCE_ID."""
    from tp_trace.metrics import create_red_metrics_recorder

    monkeypatch.setenv("OTEL_SERVICE_INSTANCE_ID", "otel-noconf")
    reader = InMemoryMetricReader()
    recorder = create_red_metrics_recorder(config=None, metric_readers=[reader])

    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=10.0,
    )

    pts = _get_metric_data_points(reader, "calls_total")
    assert len(pts) == 1
    assert pts[0].attributes["worker_id"] == f"otel-noconf:{os.getpid()}"


def test_no_config_recorder_env_precedence(monkeypatch):
    """Without config, TP_TRACE_SERVICE_INSTANCE_ID takes precedence over OTEL_SERVICE_INSTANCE_ID."""
    from tp_trace.metrics import create_red_metrics_recorder

    monkeypatch.setenv("TP_TRACE_SERVICE_INSTANCE_ID", "tp-wins")
    monkeypatch.setenv("OTEL_SERVICE_INSTANCE_ID", "otel-loses")
    reader = InMemoryMetricReader()
    recorder = create_red_metrics_recorder(config=None, metric_readers=[reader])

    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=10.0,
    )

    pts = _get_metric_data_points(reader, "calls_total")
    assert len(pts) == 1
    assert pts[0].attributes["worker_id"] == f"tp-wins:{os.getpid()}"


def test_no_config_recorder_identity_unchanged_after_later_env_mutation(monkeypatch):
    """Prefix resolved at recorder creation remains unchanged even if env variables mutate later."""
    from tp_trace.metrics import create_red_metrics_recorder

    monkeypatch.setenv("TP_TRACE_SERVICE_INSTANCE_ID", "frozen-prefix")
    reader = InMemoryMetricReader()
    recorder = create_red_metrics_recorder(config=None, metric_readers=[reader])

    # Mutate environment after recorder creation
    monkeypatch.setenv("TP_TRACE_SERVICE_INSTANCE_ID", "mutated-tp")
    monkeypatch.setenv("OTEL_SERVICE_INSTANCE_ID", "mutated-otel")

    recorder.record_request(
        method="GET",
        route="/api/test/ok/",
        status_code=200,
        error=False,
        duration_ms=10.0,
    )

    pts = _get_metric_data_points(reader, "calls_total")
    assert len(pts) == 1
    assert pts[0].attributes["worker_id"] == f"frozen-prefix:{os.getpid()}"


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
        metric_readers=None,
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
