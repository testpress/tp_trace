"""Minimal OpenTelemetry metrics collector for Gunicorn StatsD and worker state.

``gunicorn_worker_state`` and ``gunicorn_statsd_gauge`` attach ``worker_id``
(``<host>:<pid>`` via :func:`get_process_worker_identity`) so forked workers
sharing a Resource do not collide. Dashboards and alerts should aggregate
across workers with ``sum by (state)`` rather than tracking per-PID series.

With ``threads > 1`` (e.g. ``gthread``), ``gunicorn_worker_state`` is boolean:
any active request (``_active_requests > 0``) marks the worker ``busy``.
"""

import os
import threading
from typing import Any, Dict, Iterable, Optional

from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import Observation

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
        if recorder is not None:
            meter = recorder.meter
            provider_id = id(recorder.meter_provider)
        else:
            provider = otel_metrics.get_meter_provider()
            if type(provider).__name__ in ("ProxyMeterProvider", "NoOpMeterProvider"):
                # Avoid registering callbacks on the pre-init no-op proxy meter.
                return False
            meter = otel_metrics.get_meter(_DEFAULT_METER_NAME)
            provider_id = id(provider)

        with self._lock:
            if self._instruments_ready and self._meter_provider_id == provider_id:
                return True

            self._counter = meter.create_up_down_counter(
                name="gunicorn_statsd_counter",
                description="Gunicorn native StatsD counters",
                unit="1",
            )
            self._duration = meter.create_histogram(
                name="gunicorn_statsd_duration_ms",
                description="Gunicorn native StatsD timers and histograms in ms",
                unit="ms",
            )
            def _gauge_cb(opts: Any) -> Iterable[Observation]:
                return self._observe_gauges(opts, provider_id)

            def _state_cb(opts: Any) -> Iterable[Observation]:
                return self._observe_worker_state(opts, provider_id)

            self._gauge = meter.create_observable_gauge(
                name="gunicorn_statsd_gauge",
                callbacks=[_gauge_cb],
                description="Gunicorn native StatsD gauges",
                unit="1",
            )
            self._worker_state = meter.create_observable_gauge(
                name="gunicorn_worker_state",
                callbacks=[_state_cb],
                description="Gunicorn worker busy/idle state (busy when active requests > 0)",
                unit="1",
            )
            self._meter_provider_id = provider_id
            self._instruments_ready = True
            return True

    def _observe_gauges(
        self, _options: Any, expected_provider_id: Optional[int] = None
    ) -> Iterable[Observation]:
        with self._lock:
            if (
                expected_provider_id is not None
                and self._meter_provider_id != expected_provider_id
            ):
                return []
            # Include worker_id so forked workers sharing a Resource do not collide.
            worker_id = get_process_worker_identity()
            return [
                Observation(val, {"metric": name, "worker_id": worker_id})
                for name, val in self._gauges.items()
            ]

    def _observe_worker_state(
        self, _options: Any, expected_provider_id: Optional[int] = None
    ) -> Iterable[Observation]:
        with self._lock:
            if (
                expected_provider_id is not None
                and self._meter_provider_id != expected_provider_id
            ):
                return []
            is_busy = self._active_requests > 0
            worker_id = get_process_worker_identity()
            return [
                Observation(1 if is_busy else 0, {"worker_id": worker_id, "state": "busy"}),
                Observation(0 if is_busy else 1, {"worker_id": worker_id, "state": "idle"}),
            ]

    def _after_fork_in_child(self) -> None:
        """Reset lock and master-inherited state in forked workers without deadlocking."""
        self._lock = threading.RLock()
        self._active_requests = 0
        self._gauges.clear()

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

    def record_counter(self, name: str, value: Any, *, negate: bool = False) -> None:
        def _run() -> None:
            if not self._ensure_instruments():
                return
            amount = int(value)
            if negate:
                amount = -amount
            if amount != 0:
                self._counter.add(amount, {"metric": name})

        attempt(_run, _label="gunicorn.statsd.counter")

    def record_duration(self, name: str, value: Any) -> None:
        def _run() -> None:
            if not self._ensure_instruments():
                return
            val = float(value)
            if val >= 0:
                self._duration.record(val, {"metric": name})

        attempt(_run, _label="gunicorn.statsd.duration")


_COLLECTOR = GunicornMetricsCollector()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_COLLECTOR._after_fork_in_child)


def get_gunicorn_collector() -> GunicornMetricsCollector:
    """Return the process-wide Gunicorn metrics collector."""
    return _COLLECTOR
