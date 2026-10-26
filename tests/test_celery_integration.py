"""Comprehensive unit and behavioral tests for tp_trace Celery integration."""

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery, signals
from opentelemetry import trace
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from opentelemetry.trace.status import StatusCode

import tp_trace
from tp_trace.integrations.celery import CeleryIntegration
from tp_trace.integrations.celery.context import (
    clear_admitted_task_names,
    compute_bounded_operation,
    get_admitted_task_names,
    get_bounded_task_label,
    set_admitted_task_names,
)
from tp_trace.route_context import (
    reset_request_route,
    reset_request_tags,
    reset_request_view,
    set_request_route,
    set_request_tag,
    set_request_view,
)


@pytest.fixture(autouse=True)
def clean_telemetry():
    """Reset tp_trace and clean any Celery/OTel receivers between tests."""
    tp_trace._reset_for_testing()
    clear_admitted_task_names()
    try:
        inst = CeleryInstrumentor()
        if getattr(inst, "is_instrumented_by_opentelemetry", False):
            inst.uninstrument()
    except Exception:
        pass
    yield
    tp_trace._reset_for_testing()
    clear_admitted_task_names()
    try:
        inst = CeleryInstrumentor()
        if getattr(inst, "is_instrumented_by_opentelemetry", False):
            inst.uninstrument()
    except Exception:
        pass


@pytest.fixture
def memory_exporter():
    """Initializes tp_trace with Celery enabled and an in-memory exporter."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-celery-service",
        environment="test",
        exporter=exporter,
        export_batch=False,
        integrations={"celery": True},
    )
    return exporter


@pytest.fixture
def test_celery_app():
    """Configures an in-memory Celery application for tests."""
    app = Celery("test_celery_app", broker="memory://", backend="cache+memory://")
    app.conf.update(
        task_always_eager=True,
        task_eager_propagates=False,
    )
    return app


# ==============================================================================
# 1. Import Safety & Manager Registration
# ==============================================================================


def test_import_safety_without_celery():
    """Ensure tp_trace and its integrations import cleanly even when celery is absent."""
    with patch.dict(sys.modules, {"celery": None, "opentelemetry.instrumentation.celery": None}):
        from tp_trace.integrations.celery.context import compute_bounded_operation

        assert compute_bounded_operation("run", "my.task") == "celery.run.unknown"
        integ = CeleryIntegration()
        assert integ.is_installed() is False
        assert integ.instrument() is False


def test_celery_integration_manager_defaults():
    """Verify Celery is enabled by default and explicit opt-out is respected."""
    from tp_trace.integrations import get_integration_manager

    mgr = get_integration_manager()
    assert "celery" in mgr._registered_classes
    assert mgr._resolve_class(mgr._registered_classes["celery"]) is CeleryIntegration

    # Default apply_integrations activates installed Celery without an override
    instrumented = mgr.apply_integrations(config=tp_trace.SDKConfig(project_name="test"))
    assert "celery" in instrumented
    assert mgr.get("celery")._instrumented is True
    mgr.uninstrument_all()

    # Explicit enable remains supported
    instrumented = mgr.apply_integrations(config=tp_trace.SDKConfig(project_name="test", integrations={"celery": True}))
    assert "celery" in instrumented
    mgr.uninstrument_all()

    # Explicit opt-out leaves celery disabled
    instrumented = mgr.apply_integrations(
        config=tp_trace.SDKConfig(project_name="test", integrations={"celery": False})
    )
    assert "celery" not in instrumented
    assert mgr.get("celery") is None
    mgr.uninstrument_all()


@pytest.mark.parametrize(
    "kwargs, enabled",
    [
        ({}, True),
        ({"integrations": {"celery": False}}, False),
        ({"auto_patch": False}, False),
    ],
)
def test_init_celery_default_and_opt_out(kwargs, enabled):
    """The public init path enables installed Celery and honors both opt-outs."""
    from tp_trace.integrations import get_integration_manager

    tp_trace.init(exporter=InMemorySpanExporter(), export_batch=False, **kwargs)
    integration = get_integration_manager().get("celery")
    assert bool(integration and integration._instrumented) is enabled


# ==============================================================================
# 2. Lifecycle, Idempotency, and Coexistence
# ==============================================================================


def test_lifecycle_and_idempotency(test_celery_app):
    """Test idempotency and clean activation/deactivation cycles."""
    integ = CeleryIntegration()
    assert integ.instrument() is True
    assert integ.instrument() is True  # Idempotent
    assert integ._instrumented is True

    assert integ.uninstrument() is True
    assert integ._instrumented is False

    # Second cycle
    assert integ.instrument() is True
    assert integ._instrumented is True
    assert integ.uninstrument() is True


def test_unrelated_receiver_preservation(test_celery_app):
    """Verify uninstrument() does not disconnect unrelated Celery signal receivers."""
    called = []

    def external_receiver(*args, **kwargs):
        called.append(True)

    signals.task_prerun.connect(external_receiver, weak=False)
    try:
        integ = CeleryIntegration()
        integ.instrument()
        integ.uninstrument()

        # Fire signal to prove external receiver is still connected
        signals.task_prerun.send(sender="test", task_id="1", task=None, args=(), kwargs={})
        assert len(called) == 1
    finally:
        signals.task_prerun.disconnect(external_receiver)


def test_coexistence_with_upstream_instrumentor(test_celery_app):
    """If upstream CeleryInstrumentor is already active, CeleryIntegration skips cleanly."""
    upstream = CeleryInstrumentor()
    upstream.instrument()
    try:
        integ = CeleryIntegration()
        # Must return False without error
        assert integ.instrument() is False
        assert integ._instrumented is False

        # Teardown should leave upstream active
        integ.uninstrument()
        assert upstream.is_instrumented_by_opentelemetry is True
    finally:
        upstream.uninstrument()


# ==============================================================================
# 3. Task Execution Topology, Parenting, and Outcomes
# ==============================================================================


def test_task_execution_topology_and_success(test_celery_app, memory_exporter):
    """Verify trace propagation, parent-child topology, and success attributes using in-process worker."""
    test_celery_app.conf.update(task_always_eager=False)

    @test_celery_app.task(name="sample.tasks.add")
    def add(x, y):
        return x + y

    set_admitted_task_names(["sample.tasks.add"])

    worker = test_celery_app.Worker(concurrency=1, pool="solo", quiet=True)
    worker_thread = threading.Thread(target=worker.start, daemon=True)
    worker_thread.start()
    time.sleep(0.3)

    tokens = set_request_route("/api/calculate/", "POST")
    set_request_tag("tenant", "test-school")
    view_token = set_request_view("calculator.views.AddView.post")

    try:
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("django.request", kind=SpanKind.SERVER):
            result = add.apply_async((10, 20))
            assert result.get(timeout=5, disable_sync_subtasks=False) == 30
    finally:
        reset_request_view(view_token)
        reset_request_route(tokens)
        reset_request_tags()
        worker.stop()
        worker_thread.join(timeout=2)

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 3

    server = next(s for s in spans if s.name == "django.request")
    producer = next(s for s in spans if s.name == "🟣 sample.tasks.add.apply_async")
    consumer = next(s for s in spans if s.name == "🟣 sample.tasks.add.run")

    # Trace ID propagation & topology
    assert producer.context.trace_id == server.context.trace_id
    assert consumer.context.trace_id == server.context.trace_id
    assert producer.parent.span_id == server.context.span_id
    assert consumer.parent.span_id == producer.context.span_id

    # Producer assertions
    assert producer.kind == SpanKind.PRODUCER
    assert producer.attributes.get("normalized.service") == "celery"
    assert producer.attributes.get("normalized.operation") == "celery.apply_async.sample.tasks.add"
    assert producer.attributes.get("celery.task_name") == "sample.tasks.add"
    assert producer.attributes.get("error") is False
    assert producer.attributes.get("tenant") == "test-school"
    assert producer.attributes.get("celery.trigger.view") == "calculator.views.AddView.post"

    # HTTP routes MUST NOT be on Celery spans
    for bad in ("http.route", "http.method", "http.request.method", "http.route_template", "django.route"):
        assert bad not in producer.attributes
        assert bad not in consumer.attributes

    # Consumer assertions
    assert consumer.kind == SpanKind.CONSUMER
    assert consumer.attributes.get("normalized.service") == "celery"
    assert consumer.attributes.get("normalized.operation") == "celery.run.sample.tasks.add"
    assert consumer.attributes.get("celery.task_name") == "sample.tasks.add"
    assert consumer.attributes.get("celery.task_state") == "SUCCESS"
    assert consumer.attributes.get("error") is False
    assert consumer.attributes.get("celery.trigger.view") == "calculator.views.AddView.post"
    assert consumer.status.status_code == StatusCode.OK


def test_celery_trigger_view_absent_when_no_active_view(test_celery_app, memory_exporter):
    """Verify celery.trigger.view is omitted when task is published outside a view context."""
    from tp_trace.integrations.celery.context import set_admitted_task_names

    set_admitted_task_names(["sample.tasks.add"])

    @test_celery_app.task(name="sample.tasks.add")
    def add(x, y):
        return x + y

    add.delay(5, 5)

    spans = memory_exporter.get_finished_spans()
    producer = next((s for s in spans if s.name == "🟣 sample.tasks.add.apply_async"), None)
    consumer = next((s for s in spans if s.name == "🟣 sample.tasks.add.run"), None)

    if producer:
        assert "celery.trigger.view" not in producer.attributes
    if consumer:
        assert "celery.trigger.view" not in consumer.attributes


def test_celery_trigger_view_eager_execution(test_celery_app, memory_exporter):
    """Verify celery.trigger.view is captured on consumer span during eager execution."""
    from tp_trace.integrations.celery.context import set_admitted_task_names

    set_admitted_task_names(["sample.tasks.add"])

    @test_celery_app.task(name="sample.tasks.add")
    def add(x, y):
        return x + y

    view_token = set_request_view("reports.views.GenerateReportView.post")
    try:
        add.delay(3, 7)
    finally:
        reset_request_view(view_token)

    spans = memory_exporter.get_finished_spans()
    consumer = next(s for s in spans if s.name == "🟣 sample.tasks.add.run")
    assert consumer.attributes.get("celery.trigger.view") == "reports.views.GenerateReportView.post"


def test_task_failure_outcomes(test_celery_app, memory_exporter):
    """Verify failure sets ERROR status, error=True, and exception type."""

    @test_celery_app.task(name="sample.tasks.failing_task")
    def failing_task():
        raise ValueError("Invalid calculation")

    set_admitted_task_names(["sample.tasks.failing_task"])

    res = failing_task.apply_async()
    assert res.status == "FAILURE"

    spans = memory_exporter.get_finished_spans()
    consumer = next(s for s in spans if s.name == "🟣 sample.tasks.failing_task.run")

    assert consumer.attributes.get("error") is True
    assert consumer.attributes.get("error.type") == "ValueError"
    assert consumer.attributes.get("celery.task_state") == "FAILURE"
    assert consumer.status.status_code == StatusCode.ERROR
    assert len([event for event in consumer.events if event.name == "exception"]) == 1


def test_task_throws_exception_outcome(test_celery_app, memory_exporter):
    """Verify tasks declaring `throws` still set ERROR status and error=True in tp_trace."""

    class ExpectedTaskError(Exception):
        pass

    @test_celery_app.task(name="sample.tasks.throws_task", throws=(ExpectedTaskError,))
    def throws_task():
        raise ExpectedTaskError("Expected domain exception")

    set_admitted_task_names(["sample.tasks.throws_task"])

    res = throws_task.apply_async()
    assert res.status == "FAILURE"

    spans = memory_exporter.get_finished_spans()
    consumer = next(s for s in spans if s.name == "🟣 sample.tasks.throws_task.run")

    assert consumer.attributes.get("error") is True
    assert consumer.attributes.get("error.type") == "ExpectedTaskError"
    assert consumer.status.status_code == StatusCode.ERROR


def test_task_retry_outcome(test_celery_app, memory_exporter):
    """Verify task retry keeps UNSET status and error=False with task_state='RETRY'."""
    from celery.exceptions import Retry

    mgr = tp_trace.integrations.manager.get_integration_manager()
    integ = mgr.get("celery")
    assert integ is not None and integ._our_instrumentor is not None

    inst = integ._our_instrumentor

    class DummyTask:
        name = "sample.tasks.retry_task"

    task = DummyTask()
    mock_task_request = MagicMock()
    mock_task_request.id = "retry-id-123"

    mock_span = MagicMock()
    mock_span.is_recording.return_value = True

    from opentelemetry.instrumentation.celery import utils

    utils.attach_context(task, "retry-id-123", mock_span, MagicMock(), None)

    try:
        inst._trace_retry(
            sender=task,
            request=mock_task_request,
            reason=Retry("Temporary lock collision"),
        )
        mock_span.set_attribute.assert_any_call("celery.task_state", "RETRY")
        mock_span.set_attribute.assert_any_call("error", False)
        mock_span.set_attribute.assert_any_call("celery.retry.reason", "Temporary lock collision")
    finally:
        utils.detach_context(task, "retry-id-123")


# ==============================================================================
# 4. Cardinality and Bounded Operations
# ==============================================================================


def test_cardinality_bounds_and_unknown_fallback():
    """Verify 256 task name admission bound, overlong names, and unknown fallbacks."""
    large_set = [f"app.tasks.task_{i:04d}" for i in range(300)]
    overlong = "a" * 205
    large_set.append(overlong)

    set_admitted_task_names(large_set)
    admitted = get_admitted_task_names()

    assert len(admitted) == 256
    assert overlong not in admitted
    assert get_bounded_task_label(overlong) == "unknown"

    assert get_bounded_task_label("app.tasks.task_0299") == "unknown"
    assert get_bounded_task_label(None) == "unknown"
    assert get_bounded_task_label("nonexistent.task") == "unknown"

    assert get_bounded_task_label("app.tasks.task_0001") == "app.tasks.task_0001"
    assert compute_bounded_operation("run", "app.tasks.task_0001") == "celery.run.app.tasks.task_0001"
    assert compute_bounded_operation("apply_async", "unknown.task") == "celery.apply_async.unknown"


def test_unknown_task_producer_naming(test_celery_app, memory_exporter):
    """Verify publishing an unadmitted/unknown task produces celery.apply_async.unknown."""
    set_admitted_task_names(["sample.tasks.known"])

    test_celery_app.conf.update(task_always_eager=False)
    test_celery_app.send_task("sample.tasks.unregistered_anonymous", args=(1,))

    spans = memory_exporter.get_finished_spans()
    producer = next(s for s in spans if s.name == "🟣 sample.tasks.unregistered_anonymous.apply_async")

    assert producer.attributes.get("normalized.service") == "celery"
    assert producer.attributes.get("normalized.operation") == "celery.apply_async.unknown"
    assert producer.attributes.get("celery.task_name") == "sample.tasks.unregistered_anonymous"
    assert "sample.tasks.unregistered_anonymous" not in get_admitted_task_names()


def test_celery_apply_async_fallback_span_naming(test_celery_app, memory_exporter):
    """Verify publishing with no resolvable task name falls back to '🟣 celery.apply_async'."""
    mgr = tp_trace.integrations.manager.get_integration_manager()
    integ = mgr.get("celery")
    assert integ is not None and integ._our_instrumentor is not None
    inst = integ._our_instrumentor

    # Directly invoke before_publish with sender=None and message headers with task_id
    inst._trace_before_publish(
        sender=None,
        headers={"id": "test-fallback-id"},
        body={},
    )
    spans = memory_exporter.get_finished_spans()
    producers = [s for s in spans if s.kind == SpanKind.PRODUCER]
    assert len(producers) == 1
    assert producers[0].name == "🟣 celery.apply_async"


def test_celery_run_fallback_span_naming(test_celery_app, memory_exporter):
    """Verify executing a task with no name falls back to '🟣 celery.run'."""
    from opentelemetry.instrumentation.celery import utils

    mgr = tp_trace.integrations.manager.get_integration_manager()
    integ = mgr.get("celery")
    assert integ is not None and integ._our_instrumentor is not None
    inst = integ._our_instrumentor

    class NamelessTask:
        name = None
        request = MagicMock()

    task = NamelessTask()
    task_id = "test-nameless-id"
    inst._trace_prerun(sender=task, task=task, task_id=task_id, args=(), kwargs={})
    try:
        ctx = utils.retrieve_context(task, task_id)
        if ctx is not None:
            span, _, _ = ctx
            assert span.name == "🟣 celery.run"
    finally:
        inst._trace_postrun(sender=task, task_id=task_id, task=task, args=(), kwargs={}, retval=None, state="SUCCESS")


# ==============================================================================
# 5. Metrics Suppression
# ==============================================================================


def test_upstream_metrics_suppressed(test_celery_app, memory_exporter):
    """Verify Flower histogram is suppressed and self.metrics is empty dict."""
    mgr = tp_trace.integrations.manager.get_integration_manager()
    integ = mgr.get("celery")
    assert integ is not None and integ._our_instrumentor is not None
    inst = integ._our_instrumentor

    assert inst.metrics == {}
    inst._record_histograms("task-123", {"task": "foo"})


# ==============================================================================
# 6. Broker Publication Failure Cleanup
# ==============================================================================


def test_broker_publication_failure_cleanup(test_celery_app, memory_exporter):
    """Verify broker error after before_publish cleans up producer span and preserves context."""
    import kombu

    test_celery_app.conf.update(task_always_eager=False)

    @test_celery_app.task(name="sample.tasks.broker_fail")
    def broker_fail():
        pass

    set_admitted_task_names(["sample.tasks.broker_fail"])

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("parent_request") as parent:
        with patch.object(
            kombu.messaging.Producer, "_publish", autospec=True, side_effect=RuntimeError("Broker unavailable")
        ):
            with pytest.raises(RuntimeError, match="Broker unavailable"):
                broker_fail.apply_async()

        current = trace.get_current_span()
        assert current == parent

    spans = memory_exporter.get_finished_spans()
    producer = next(s for s in spans if s.name == "🟣 sample.tasks.broker_fail.apply_async")
    assert producer.attributes.get("error") is True
    assert producer.attributes.get("error.type") == "RuntimeError"
    assert producer.status.status_code == StatusCode.ERROR


# ==============================================================================
# 7. Postrun Cleanup & Timing Map Leak Regression
# ==============================================================================


def test_timing_map_leak_regression(test_celery_app, memory_exporter):
    """Regression test: task_id_to_start_time is popped even on failure or older floor version."""
    mgr = tp_trace.integrations.manager.get_integration_manager()
    integ = mgr.get("celery")
    inst = integ._our_instrumentor

    class DummyTask:
        name = "sample.task"

    task = DummyTask()
    inst.task_id_to_start_time["leak-task-id"] = 100.0

    inst._trace_postrun(task=task, task_id="leak-task-id", state="SUCCESS")
    assert "leak-task-id" not in inst.task_id_to_start_time


def test_postrun_enrichment_failure_still_finalizes(test_celery_app, memory_exporter):
    """If attribute enrichment raises in postrun, upstream finalization and cleanup still run."""
    mgr = tp_trace.integrations.manager.get_integration_manager()
    integ = mgr.get("celery")
    inst = integ._our_instrumentor

    class BadState:
        def __str__(self):
            raise RuntimeError("Failing __str__")

    class DummyTask:
        name = "sample.task"

    task = DummyTask()
    inst.task_id_to_start_time["bad-state-id"] = 50.0

    inst._trace_postrun(task=task, task_id="bad-state-id", state=BadState())
    assert "bad-state-id" not in inst.task_id_to_start_time


# ==============================================================================
# 8. External Span Isolation
# ==============================================================================


def test_external_celery_span_not_rewritten():
    """External Celery spans created without tp_trace creation meta retain original semantics."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("opentelemetry.instrumentation.celery")

    span = tracer.start_span("run/external_task", kind=SpanKind.CONSUMER)
    span.end()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.attributes.get("tp_trace.celery.owned") is None
    assert s.name == "run/external_task"


@pytest.mark.parametrize("cached_sender", [False, True])
def test_publish_failure_with_cached_sender(test_celery_app, cached_sender):
    """An already-cached sender still cleans up exactly one failing publication."""
    if cached_sender:
        _ = test_celery_app.amqp.send_task_message
    exporter = InMemorySpanExporter()
    tp_trace.init(exporter=exporter, export_batch=False, integrations={"celery": True})
    test_celery_app.conf.task_always_eager = False

    @test_celery_app.task(name="regression.cached_sender")
    def task():
        return 1

    error = RuntimeError("broker unavailable")
    from opentelemetry import context

    original = context.get_current()
    try:
        with trace.get_tracer("test").start_as_current_span("parent") as parent:
            with patch("kombu.messaging.Producer._publish", autospec=True, side_effect=error) as publish:
                with pytest.raises(RuntimeError) as caught:
                    task.apply_async()
                assert caught.value is error
                publish.assert_called_once()
            assert trace.get_current_span() is parent
            assert parent.is_recording()
            with trace.get_tracer("test").start_as_current_span("following") as following:
                assert following.parent.span_id == parent.get_span_context().span_id
    finally:
        # A failing regression must not contaminate the next test's context.
        context.attach(original)
    producers = [s for s in exporter.get_finished_spans() if s.name == "🟣 regression.cached_sender.apply_async"]
    assert len(producers) == 1
    assert producers[0].attributes["error"] is True


def test_unknown_publication_does_not_enrich_parent(test_celery_app, memory_exporter):
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("parent") as parent:
        test_celery_app.conf.update(task_always_eager=False)
        test_celery_app.send_task("regression.unregistered")
        assert trace.get_current_span() is parent
        assert parent.is_recording()
        assert "celery.task_id" not in parent.attributes
        assert "celery.task_name" not in parent.attributes
        assert "celery.queue" not in parent.attributes


def test_prerun_span_creation_failure_restores_context(test_celery_app, memory_exporter):
    from opentelemetry import context
    from opentelemetry.instrumentation.celery import utils

    @test_celery_app.task(name="regression.prerun")
    def task():
        return 42

    task.push_request(id="prerun-failure", traceparent="00-" + "1" * 32 + "-" + "2" * 16 + "-01")
    inst = tp_trace.integrations.manager.get_integration_manager().get("celery")._our_instrumentor
    original = context.get_current()
    try:
        with trace.get_tracer("test").start_as_current_span("parent") as parent:
            with patch.object(inst._tracer, "start_span", side_effect=RuntimeError("telemetry")):
                inst._trace_prerun(task=task, task_id="prerun-failure")
            assert trace.get_current_span() is parent
            assert utils.retrieve_context(task, "prerun-failure") is None
            assert "prerun-failure" not in inst.task_id_to_start_time
    finally:
        task.pop_request()
        context.attach(original)


@pytest.mark.parametrize(
    "missing",
    [
        "_trace_prerun",
        "_trace_postrun",
        "_trace_before_publish",
        "_trace_after_publish",
        "_trace_failure",
        "_trace_retry",
        "create_celery_metrics",
        "_record_histograms",
    ],
)
def test_compatibility_requires_all_upstream_hooks(monkeypatch, missing):
    monkeypatch.delattr(CeleryInstrumentor, missing)
    integration = CeleryIntegration()
    assert integration._check_compatibility() is False
    assert integration.instrument() is False


def test_compatibility_requires_retry_reason(monkeypatch):
    from opentelemetry.instrumentation.celery import utils

    monkeypatch.delattr(utils, "retrieve_reason")
    assert CeleryIntegration()._check_compatibility() is False


def test_instrumentation_preserves_task_registration_across_apps():
    """Worker tasks declared on another app must survive telemetry startup."""
    declaring_app = Celery("declaring-app", fixups=[])
    worker_app = Celery("worker-app", fixups=[])
    integration = CeleryIntegration()
    try:
        assert integration.instrument()

        @declaring_app.task(name="regression.shared_registration_after_telemetry")
        def task():
            return 42

        # Celery normally imports task modules before finalizing the worker app.
        worker_app.finalize()
        assert "regression.shared_registration_after_telemetry" in worker_app.tasks
        assert worker_app.tasks["regression.shared_registration_after_telemetry"].run() == 42
    finally:
        integration.uninstrument()
        declaring_app.close()
        worker_app.close()


def test_snapshot_does_not_finalize_application():
    app = Celery("snapshot-lazy-app", fixups=[])
    try:
        assert not app.finalized
        CeleryIntegration()._build_task_snapshot()
        assert not app.finalized
    finally:
        app.close()


def test_future_application_finalization_admits_tasks_and_disconnects():
    from celery import _state

    integration = CeleryIntegration()
    app = None
    try:
        assert integration.instrument()
        app = Celery("created-after-telemetry", fixups=[])

        @app.task(name="regression.future_application_task")
        def task():
            return 42

        assert not app.finalized
        app.finalize()
        assert get_bounded_task_label("regression.future_application_task") == "regression.future_application_task"
        assert integration._watch_app_finalization in _state._on_app_finalizers
        assert app.on_after_finalize.has_listeners()
        integration.uninstrument()
        assert integration._watch_app_finalization not in _state._on_app_finalizers
        assert not app.on_after_finalize.has_listeners()
        app.on_after_finalize.send(sender=app)
        assert not get_admitted_task_names()
    finally:
        integration.uninstrument()
        if app is not None:
            app.close()


def test_snapshot_observes_already_finalized_application():
    app = Celery("already-finalized", fixups=[])
    try:

        @app.task(name="regression.already_registered")
        def task():
            return 42

        app.finalize()
        CeleryIntegration()._build_task_snapshot()
        assert get_bounded_task_label("regression.already_registered") == "regression.already_registered"
    finally:
        app.close()


def test_snapshot_does_not_create_default_application(monkeypatch):
    from celery import _state

    monkeypatch.setattr(_state, "_apps", set())
    monkeypatch.setattr(_state, "default_app", None)
    monkeypatch.setattr(_state._tls, "current_app", None, raising=False)
    CeleryIntegration()._build_task_snapshot()
    assert _state.default_app is None
    assert _state._tls.current_app is None


def test_finalized_registries_cannot_evict_admitted_names():
    initial = {f"z.task_{i:03}" for i in range(255)}
    set_admitted_task_names(initial)
    set_admitted_task_names(["a.new_task", "b.excess"], extend=True)
    assert get_admitted_task_names() == initial | {"a.new_task"}
    set_admitted_task_names(["a.even_earlier"], extend=True)
    assert get_admitted_task_names() == initial | {"a.new_task"}


def test_late_registered_task_has_matching_producer_and_consumer_operations(test_celery_app, memory_exporter):
    """An app can finalize before a later task module registers its decorators."""
    test_celery_app.finalize()
    clear_admitted_task_names()
    name = "regression.late_registered_task"

    @test_celery_app.task(name=name)
    def task(value):
        return value * 2

    assert get_bounded_task_label(name) == "unknown"
    test_celery_app.conf.task_always_eager = False
    with patch("kombu.messaging.Producer._publish", autospec=True):
        task.apply_async(args=[21])
    result = task.apply(args=[21])
    assert result.successful()
    assert result.result == 42
    spans = memory_exporter.get_finished_spans()
    producer = next(s for s in spans if s.kind == SpanKind.PRODUCER)
    consumer = next(s for s in spans if s.kind == SpanKind.CONSUMER)
    assert producer.attributes["celery.task_name"] == name
    assert producer.attributes["normalized.operation"] == "celery.apply_async." + name
    assert consumer.attributes["normalized.operation"] == "celery.run." + name


def test_late_registration_cannot_exceed_metric_name_limit(test_celery_app, memory_exporter):
    test_celery_app.finalize()
    initial = {f"existing.task_{i}" for i in range(256)}
    set_admitted_task_names(initial)

    @test_celery_app.task(name="regression.excess_registered_task")
    def task():
        return 42

    test_celery_app.conf.task_always_eager = False
    with patch("kombu.messaging.Producer._publish", autospec=True):
        task.apply_async()
    producer = next(s for s in memory_exporter.get_finished_spans() if s.kind == SpanKind.PRODUCER)
    assert producer.attributes["normalized.operation"] == "celery.apply_async.unknown"
    assert get_admitted_task_names() == initial


def test_failed_activation_removes_owned_receivers_and_allows_retry(monkeypatch):
    from celery import _state
    from tp_trace.integrations.celery.instrumentor import TpTraceCeleryInstrumentor

    integration = CeleryIntegration()
    baseline = list(signals.task_prerun.receivers)
    with monkeypatch.context() as failure:
        failure.setattr(integration, "_build_task_snapshot", MagicMock(side_effect=RuntimeError("setup failed")))
        assert integration.instrument() is False
    assert not TpTraceCeleryInstrumentor._instance.is_instrumented_by_opentelemetry
    assert signals.task_prerun.receivers == baseline
    assert integration._watch_app_finalization not in _state._on_app_finalizers
    try:
        assert integration.instrument() is True
    finally:
        integration.uninstrument()


def test_partial_signal_activation_is_rolled_back(monkeypatch):
    integration = CeleryIntegration()
    before = list(signals.task_prerun.receivers)
    with monkeypatch.context() as failure:
        failure.setattr(signals.task_postrun, "connect", MagicMock(side_effect=RuntimeError("connection failed")))
        assert not integration.instrument()
    assert signals.task_prerun.receivers == before
    try:
        assert integration.instrument()
    finally:
        integration.uninstrument()


def test_second_integration_cannot_adopt_active_instrumentor():
    owner = CeleryIntegration()
    other = CeleryIntegration()
    try:
        assert owner.instrument()
        set_admitted_task_names(["owned.task"])
        assert not other.instrument()
        other.uninstrument()
        assert owner._our_instrumentor.is_instrumented_by_opentelemetry
        assert get_bounded_task_label("owned.task") == "owned.task"
    finally:
        owner.uninstrument()
