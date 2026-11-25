"""Gunicorn Statsd logger subclass that exports native metrics via tp_trace."""

from typing import Any

from gunicorn.instrument.statsd import Statsd

from tp_trace.integrations.gunicorn.metrics import get_gunicorn_collector


class TPTraceGunicornLogger(Statsd):
    """Bridges Gunicorn's native Statsd metrics into tp_trace OpenTelemetry instruments."""

    def gauge(self, name: str, value: Any) -> None:
        get_gunicorn_collector().record_gauge(name, value)
        if self.sock:
            super().gauge(name, value)

    def increment(self, name: str, value: Any, sampling_rate: float = 1.0) -> None:
        get_gunicorn_collector().record_counter(name, value)
        if self.sock:
            super().increment(name, value, sampling_rate=sampling_rate)

    def decrement(self, name: str, value: Any, sampling_rate: float = 1.0) -> None:
        get_gunicorn_collector().record_counter(name, value, negate=True)
        if self.sock:
            super().decrement(name, value, sampling_rate=sampling_rate)

    def timer(self, name: str, value: Any) -> None:
        get_gunicorn_collector().record_duration(name, value)
        if self.sock:
            super().timer(name, value)

    def histogram(self, name: str, value: Any) -> None:
        get_gunicorn_collector().record_duration(name, value)
        if self.sock:
            super().histogram(name, value)
