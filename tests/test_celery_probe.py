"""The explicit live probe must reject stale metrics and missing worker evidence."""

import importlib.util
from pathlib import Path

import pytest


_spec = importlib.util.spec_from_file_location("celery_probe", Path(__file__).parent / "e2e/check_celery.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def metric_sample(value):
    return {name: {(): value} for name in ("producer", "success", "error", "count", "sum", "inf", "finite", "tiny")}


def test_probe_rejects_stale_metrics():
    baseline = metric_sample(100)
    with pytest.raises(AssertionError, match="fresh publication"):
        probe.check_metric_increments(baseline, baseline, 6)


def test_probe_checks_histogram_units_and_resets():
    baseline = metric_sample(0)
    after = metric_sample(6)
    after["sum"] = {(): 3000}
    after["tiny"] = {(): 0}
    assert probe.check_metric_increments(baseline, after, 6)
    after["sum"] = {(): 3}
    with pytest.raises(AssertionError, match="mean task duration"):
        probe.check_metric_increments(baseline, after, 6)
    with pytest.raises(RuntimeError, match="Counter reset"):
        probe.counter_increments(metric_sample(10), metric_sample(1))
    after["count"] = {}
    with pytest.raises(RuntimeError, match="series disappeared"):
        probe.counter_increments(metric_sample(0), after)


def test_probe_worker_evidence_is_tied_to_requested_tasks():
    logs = """
worker | [2026-10-22: INFO/ForkPoolWorker-1] Task sample_app.tasks.demo_task[a] succeeded in 0.5s: 2
worker | [2026-10-22: INFO/ForkPoolWorker-2] Task sample_app.tasks.demo_task[b] succeeded in 0.5s: 4
worker | [2026-10-22: INFO/ForkPoolWorker-3] Task sample_app.tasks.demo_task[unrelated] succeeded in 0.5s: 6
"""
    assert probe.worker_processes_for_tasks(logs, {"a", "b"}) == {"a": "ForkPoolWorker-1", "b": "ForkPoolWorker-2"}
    assert probe.worker_processes_for_tasks(logs, {"missing"}) == {}


def test_probe_rejects_duplicate_consumer_spans():
    producer = {"name": "celery.apply_async", "attributes": {"celery.task_id": "a"}, "spanId": "p", "traceId": "t"}
    consumer = {"name": "celery.run", "attributes": {"celery.task_id": "a"}, "parentSpanId": "p", "traceId": "t"}
    assert probe.task_span_pair([producer, consumer], "a") == (producer, consumer)
    assert probe.task_span_pair([producer], "a") is None
    with pytest.raises(AssertionError, match="Duplicate"):
        probe.task_span_pair([producer, consumer, consumer], "a")
