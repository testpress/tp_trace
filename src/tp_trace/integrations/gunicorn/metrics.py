"""Minimal OpenTelemetry metrics collector for Gunicorn StatsD and worker state."""

import os
import threading
from typing import Any, Dict, Iterable, Optional

from opentelemetry.metrics import Observation, get_meter

from tp_trace.config import SDKConfig
from tp_trace.metrics import get_metrics_recorder, get_process_worker_identity
from tp_trace.safety import attempt

_DEFAULT_METER_NAME = "tp_trace.gunicorn"


class GunicornMetricsCollector:
    """Thread-safe OTel collector for Gunicorn native StatsD metrics and worker state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config: Optional[SDKConfig] = None
        self._meter_provider_id: Optional[int] = None
        self._instruments_ready = False
        self._active_requests = 0
        self._gauges: Dict[str, float] = {}

        self._counter: Any = None
        self._duration: Any = None
        self._gauge: Any = None
        self._worker_state: Any = None

    def set_config(self, config: Optional[SDKConfig]) -> None:
        with self._lock:
            self._config = config

    def is_enabled(self) -> bool:
        disabled = os.environ.get("TP_TRACE_DISABLED", "").strip().lower()
        if disabled in ("1", "true", "yes", "on"):
            return False
        return not (self._config is not None and self._config.disabled)

    def _ensure_instruments(self) -> bool:
        if not self.is_enabled():
            return False

        recorder = get_metrics_recorder()
        meter = recorder.meter if recorder is not None else get_meter(_DEFAULT_METER_NAME)
        provider_id = id(recorder.meter_provider) if recorder is not None else id(meter)

        with self._lock:
            if self._instruments_ready and self._meter_provider_id == provider_id:
                return True

            self._counter = meter.create_counter(
                name="gunicorn_statsd_counter",
                description="Gunicorn native StatsD counters",
                unit="1",
            )
            self._duration = meter.create_histogram(
                name="gunicorn_statsd_duration_ms",
                description="Gunicorn native StatsD timers and histograms in ms",
                unit="ms",
            )
            self._gauge = meter.create_observable_gauge(
                name="gunicorn_statsd_gauge",
                callbacks=[self._observe_gauges],
                description="Gunicorn native StatsD gauges",
                unit="1",
            )
            self._worker_state = meter.create_observable_gauge(
                name="gunicorn_worker_state",
                callbacks=[self._observe_worker_state],
                description="Gunicorn worker busy/idle state",
                unit="1",
            )
            self._meter_provider_id = provider_id
            self._instruments_ready = True
            return True

    def _observe_gauges(self, _options: Any) -> Iterable[Observation]:
        with self._lock:
            return [Observation(val, {"metric": name}) for name, val in self._gauges.items()]

    def _observe_worker_state(self, _options: Any) -> Iterable[Observation]:
        with self._lock:
            is_busy = self._active_requests > 0
            worker_id = get_process_worker_identity()
            return [
                Observation(1 if is_busy else 0, {"worker_id": worker_id, "state": "busy"}),
                Observation(0 if is_busy else 1, {"worker_id": worker_id, "state": "idle"}),
            ]

    def reset_worker(self) -> None:
        attempt(lambda: self._reset_active_requests(), _label="gunicorn.reset_worker")

    def _reset_active_requests(self) -> None:
        if not self._ensure_instruments():
            return
        with self._lock:
            self._active_requests = 0

    def on_request_started(self) -> None:
        def _run() -> None:
            if not self._ensure_instruments():
                return
            with self._lock:
                self._active_requests += 1

        attempt(_run, _label="gunicorn.request_started")

    def on_request_finished(self) -> None:
        def _run() -> None:
            if not self._ensure_instruments():
                return
            with self._lock:
                if self._active_requests > 0:
                    self._active_requests -= 1

        attempt(_run, _label="gunicorn.request_finished")

    def record_gauge(self, name: str, value: Any) -> None:
        def _run() -> None:
            if not self._ensure_instruments():
                return
            with self._lock:
                self._gauges[name] = float(value)

        attempt(_run, _label="gunicorn.statsd.gauge")

    def record_counter(self, name: str, value: Any) -> None:
        def _run() -> None:
            if not self._ensure_instruments():
                return
            amount = int(value)
            if amount > 0:
                self._counter.add(amount, {"metric": name})

        attempt(_run, _label="gunicorn.statsd.counter")

    def record_duration(self, name: str, value: Any) -> None:
        def _run() -> None:
            if not self._ensure_instruments():
                return
            self._duration.record(float(value), {"metric": name})

        attempt(_run, _label="gunicorn.statsd.duration")


_COLLECTOR = GunicornMetricsCollector()


def get_gunicorn_collector() -> GunicornMetricsCollector:
    """Return the process-wide Gunicorn metrics collector."""
    return _COLLECTOR
