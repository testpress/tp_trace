"""Unit tests for the Gunicorn integration and StatsD OTel bridge."""

import datetime
import sys
import types

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tp_trace.config import SDKConfig
from tp_trace.integrations.gunicorn import (
    GunicornIntegration,
    post_fork,
    post_request,
    pre_request,
)
from tp_trace.integrations.gunicorn.metrics import GunicornMetricsCollector
from tp_trace.integrations.manager import IntegrationManager


def _collect_metrics(reader: InMemoryMetricReader) -> dict:
    data = reader.get_metrics_data()
    out = {}
    if not data or not data.resource_metrics:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = metric.data.data_points
    return out


def _install_fake_gunicorn(monkeypatch):
    class FakeStatsd:
        def __init__(self, cfg):
            self.cfg = cfg
            self.sock = None

        def gauge(self, name, value):
            pass

        def increment(self, name, value, sampling_rate=1.0):
            pass

        def decrement(self, name, value, sampling_rate=1.0):
            pass

        def timer(self, name, value):
            pass

        def histogram(self, name, value):
            pass

        def access(self, resp, req, environ, request_time):
            ms = request_time.seconds * 1000 + float(request_time.microseconds) / 1000.0
            status = int(str(resp.status).split(None, 1)[0])
            self.timer("gunicorn.request.duration", ms)
            self.increment("gunicorn.requests", 1)
            self.increment(f"gunicorn.request.status.{status}", 1)

        def error(self, msg, *args, **kwargs):
            self.increment("gunicorn.log.error", 1)

    class FakeWorker:
        def handle_request(self, *args, **kwargs):
            return "ok"

    gunicorn_mod = types.ModuleType("gunicorn")
    instrument_mod = types.ModuleType("gunicorn.instrument")
    statsd_mod = types.ModuleType("gunicorn.instrument.statsd")
    statsd_mod.Statsd = FakeStatsd
    workers_mod = types.ModuleType("gunicorn.workers")
    sync_mod = types.ModuleType("gunicorn.workers.sync")
    sync_mod.SyncWorker = FakeWorker
    gthread_mod = types.ModuleType("gunicorn.workers.gthread")
    gthread_mod.ThreadWorker = FakeWorker

    for name, mod in {
        "gunicorn": gunicorn_mod,
        "gunicorn.instrument": instrument_mod,
        "gunicorn.instrument.statsd": statsd_mod,
        "gunicorn.workers": workers_mod,
        "gunicorn.workers.sync": sync_mod,
        "gunicorn.workers.gthread": gthread_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "tp_trace.integrations.gunicorn.logger", raising=False)


@pytest.fixture()
def isolated_collector(monkeypatch):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    collector = GunicornMetricsCollector()
    for target in (
        "tp_trace.integrations.gunicorn.metrics.get_gunicorn_collector",
        "tp_trace.integrations.gunicorn.hooks.get_gunicorn_collector",
        "tp_trace.integrations.gunicorn.integration.get_gunicorn_collector",
    ):
        monkeypatch.setattr(target, lambda: collector)
    monkeypatch.setattr(
        "tp_trace.integrations.gunicorn.metrics.get_metrics_recorder",
        lambda: types.SimpleNamespace(
            meter=provider.get_meter("tp_trace.gunicorn"),
            meter_provider=provider,
        ),
    )
    yield collector, reader
    provider.shutdown()


def test_statsd_logger_bridges_native_metrics(isolated_collector, monkeypatch):
    _install_fake_gunicorn(monkeypatch)
    from tp_trace.integrations.gunicorn.logger import TPTraceGunicornLogger

    logger = TPTraceGunicornLogger(types.SimpleNamespace())
    logger.gauge("gunicorn.workers", 4)
    logger.error("Worker error")

    resp = types.SimpleNamespace(status="200 OK")
    req = types.SimpleNamespace(method="GET", path="/")
    logger.access(resp, req, {}, datetime.timedelta(milliseconds=15.0))
    logger.histogram("gunicorn.request.duration", 25.0)

    metrics = _collect_metrics(isolated_collector[1])
    assert metrics["gunicorn_statsd_gauge"][0].value == 4.0
    counters = {dp.attributes["metric"]: dp.value for dp in metrics["gunicorn_statsd_counter"]}
    assert counters == {
        "gunicorn.log.error": 1,
        "gunicorn.requests": 1,
        "gunicorn.request.status.200": 1,
    }
    assert metrics["gunicorn_statsd_duration_ms"][0].count == 2
    assert metrics["gunicorn_statsd_duration_ms"][0].sum == 40.0


def test_worker_busy_idle_tracks_concurrent_requests(isolated_collector):
    _, reader = isolated_collector
    worker = types.SimpleNamespace(pid=1234)

    post_fork(None, worker)
    states = {
        dp.attributes["state"]: dp.value
        for dp in _collect_metrics(reader)["gunicorn_worker_state"]
    }
    assert states == {"busy": 0, "idle": 1}

    pre_request(worker, None)
    pre_request(worker, None)
    post_request(worker, None, {}, None)
    states = {
        dp.attributes["state"]: dp.value
        for dp in _collect_metrics(reader)["gunicorn_worker_state"]
    }
    assert states == {"busy": 1, "idle": 0}

    post_request(worker, None, {}, None)
    states = {
        dp.attributes["state"]: dp.value
        for dp in _collect_metrics(reader)["gunicorn_worker_state"]
    }
    assert states == {"busy": 0, "idle": 1}


def test_disabled_sdk_is_noop(isolated_collector):
    collector, reader = isolated_collector
    collector.set_config(SDKConfig(disabled=True))
    worker = types.SimpleNamespace(pid=1234)
    post_fork(None, worker)
    pre_request(worker, None)
    collector.record_counter("gunicorn.requests", 1)

    assert _collect_metrics(reader) == {}


def test_proxy_meter_provider_skips_registration(monkeypatch):
    from opentelemetry import metrics as otel_metrics

    class ProxyMeterProvider:
        pass

    collector = GunicornMetricsCollector()
    monkeypatch.setattr(
        "tp_trace.integrations.gunicorn.metrics.get_metrics_recorder",
        lambda: None,
    )
    monkeypatch.setattr(
        otel_metrics,
        "get_meter_provider",
        lambda: ProxyMeterProvider(),
    )
    collector.on_request_started()
    assert collector._instruments_ready is False


def test_after_fork_clears_inherited_gauges_and_requests(isolated_collector):
    collector, _ = isolated_collector
    collector.record_gauge("gunicorn.workers", 4)
    collector.on_request_started()
    collector.record_duration("gunicorn.request.duration", -5.0)

    collector._after_fork_in_child()
    assert collector._gauges == {}
    assert collector._active_requests == 0


def test_integration_manager_registers_and_uninstruments_gunicorn(monkeypatch):
    _install_fake_gunicorn(monkeypatch)
    manager = IntegrationManager()
    assert "gunicorn" in manager._registered_classes

    integration = GunicornIntegration(SDKConfig(disabled=False))
    assert integration.is_installed() is True
    assert integration.instrument() is True
    assert integration.uninstrument() is True
