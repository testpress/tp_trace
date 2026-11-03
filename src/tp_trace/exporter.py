"""Fail-safe span exporter wrapper for tp_trace.

Guarantees that telemetry export failures (network errors, connection refused,
unreachable collectors, DNS failures, timeouts) NEVER crash or disrupt the host application.
"""

import logging
import os
import time
from typing import Any, Dict, Optional
from opentelemetry.context import attach, detach, set_value
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

try:
    from opentelemetry.sdk.trace.export import _BSP_RESET_ONCE  # type: ignore[attr-defined]
except ImportError:
    _BSP_RESET_ONCE = None  # type: ignore

try:
    from opentelemetry.sdk.trace.export import _SUPPRESS_INSTRUMENTATION_KEY
except ImportError:
    _SUPPRESS_INSTRUMENTATION_KEY = "suppress_instrumentation"

from tp_trace.normalize import normalize_span_attributes

logger = logging.getLogger("tp_trace.exporter")


def _ensure_normalized_attributes(span: Any) -> None:
    try:
        normalize_span_attributes(span)
    except Exception:
        pass


class SafeSpanExporter(SpanExporter):
    """Fail-safe wrapper around any OpenTelemetry SpanExporter.

    If the collector is down, unreachable, or refusing connections (e.g. Errno 61),
    this wrapper absorbs the error, logs a rate-limited diagnostic warning, and returns
    SpanExportResult.FAILURE without allowing any exception to bubble up.
    """

    def __init__(self, exporter: SpanExporter, endpoint: Optional[str] = None):
        self._exporter = exporter
        self._endpoint = endpoint or getattr(exporter, "_endpoint", "collector")
        self._last_log_time = -float("inf")
        self._error_count = 0

    def export(self, spans: Any) -> SpanExportResult:
        try:
            if hasattr(spans, "__iter__"):
                for s in spans:
                    _ensure_normalized_attributes(s)
            res = self._exporter.export(spans)
            if res == SpanExportResult.SUCCESS and self._error_count > 0:
                logger.info("tp_trace: Connection to collector at %s restored.", self._endpoint)
                self._error_count = 0
            return res
        except Exception as exc:
            self._error_count += 1
            now = time.monotonic()
            # Rate-limit warnings: log first failure, then at most once every 5 minutes
            if now - self._last_log_time > 300:
                self._last_log_time = now
                logger.warning(
                    "tp_trace: Unable to export spans to collector at %s (%s). "
                    "Host application is completely unaffected. (Failures: %d)",
                    self._endpoint,
                    exc,
                    self._error_count,
                )
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._exporter.shutdown()
        except Exception:
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self._exporter.force_flush(timeout_millis)
        except Exception:
            return False


def batch_export_options() -> Dict[str, int]:
    """Keep bounded defaults while honoring standard OTel queue tuning knobs."""
    options = {}
    for key, env, default in (
        ("max_queue_size", "OTEL_BSP_MAX_QUEUE_SIZE", 2048),
        ("max_export_batch_size", "OTEL_BSP_MAX_EXPORT_BATCH_SIZE", 256),
        ("schedule_delay_millis", "OTEL_BSP_SCHEDULE_DELAY", 1000),
    ):
        try:
            value = int(os.environ.get(env, default))
            if value <= 0:
                raise ValueError("must be positive")
        except (TypeError, ValueError):
            logger.warning("tp_trace: Invalid %s; using %s", env, default)
            value = default
        options[key] = value
    options["max_export_batch_size"] = min(options["max_export_batch_size"], options["max_queue_size"])
    return options


class AllSpansBatchSpanProcessor(BatchSpanProcessor):
    """BatchSpanProcessor that emits all ended spans to the batch exporter.

    Standard BatchSpanProcessor drops spans where not (span.context and span.context.trace_flags.sampled).
    To support sampling-independent RED metrics in the OpenTelemetry Collector via spanmetrics,
    this processor forwards both sampled spans and RECORD_ONLY unsampled spans.
    The Collector aggregates 100% of finished spans into golden signals and statelessly drops
    unsampled spans before exporting traces to Tempo.
    """

    def _warn_queue_failure(self, reason: Any) -> None:
        if not getattr(self, "_queue_failure_warned", False):
            self._queue_failure_warned = True
            logger.warning(
                "tp_trace: Unable to queue all recording spans (%s); RED metrics may undercount. "
                "Check Collector availability, export throughput, and SDK compatibility.",
                reason,
            )

    def on_end(self, span: ReadableSpan) -> None:
        # Both compatibility paths are fail-safe: processor failures must not
        # escape span.end() into application code.
        try:
            if getattr(self, "done", False):
                return
            if hasattr(self, "_batch_processor"):
                self._batch_processor.emit(span)
                return

            # OTel SDK 1.20–1.33: isolate private deque/fork integration here.
            if hasattr(self, "_pid") and self._pid != os.getpid():
                reset = getattr(self, "_at_fork_reinit")
                if _BSP_RESET_ONCE is not None:
                    _BSP_RESET_ONCE.do_once(reset)
                else:
                    reset()

            q: Any = getattr(self, "queue", None)
            if hasattr(q, "appendleft"):
                if len(q) == getattr(self, "max_queue_size"):
                    self._spans_dropped = True
                    self._warn_queue_failure("bounded export queue is full")
                q.appendleft(span)
                if hasattr(self, "condition") and len(q) >= getattr(self, "max_export_batch_size"):
                    with self.condition:
                        self.condition.notify()
            elif hasattr(q, "put_nowait"):
                q.put_nowait(span)
            else:
                # Upstream on_end silently rejects RECORD_ONLY spans. Do not
                # pretend that fallback preserves full-population metrics.
                self._warn_queue_failure("unsupported OpenTelemetry batch queue")
        except Exception as exc:
            self._warn_queue_failure(type(exc).__name__)


class AllSpansSimpleSpanProcessor(SimpleSpanProcessor):
    """SimpleSpanProcessor that exports all ended spans synchronously."""

    def on_end(self, span: ReadableSpan) -> None:
        if getattr(self, "_shutdown", False):
            return
        token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            self.span_exporter.export((span,))
        except Exception as exc:
            logger.debug("tp_trace: Exception while exporting span: %s", exc)
        finally:
            if hasattr(self, "_metrics") and hasattr(self._metrics, "finish_items"):
                try:
                    finish_fn = getattr(self._metrics, "finish_items")
                    finish_fn(1)
                except Exception:
                    pass
            detach(token)



try:
    from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
except ImportError:
    MetricExporter = object  # type: ignore
    MetricExportResult = None  # type: ignore


class SafeMetricExporter(MetricExporter):
    """Fail-safe wrapper around any OpenTelemetry MetricExporter.

    If the collector is down or unreachable, absorbs errors, logs rate-limited
    diagnostic warnings, and returns MetricExportResult.FAILURE without breaking
    the application.
    """

    def __init__(self, exporter: Any, endpoint: Optional[str] = None):
        pref_temp = getattr(exporter, "_preferred_temporality", None)
        pref_agg = getattr(exporter, "_preferred_aggregation", None)
        try:
            super().__init__(preferred_temporality=pref_temp, preferred_aggregation=pref_agg)
        except Exception:
            pass
        self._exporter = exporter
        self._endpoint = endpoint or getattr(exporter, "_endpoint", "collector")
        self._last_log_time = -float("inf")
        self._error_count = 0

    @property
    def _preferred_temporality(self) -> Any:  # type: ignore[override]
        return getattr(self._exporter, "_preferred_temporality", {})

    @property
    def _preferred_aggregation(self) -> Any:  # type: ignore[override]
        return getattr(self._exporter, "_preferred_aggregation", {})

    def export(self, metrics_data: Any, timeout_millis: float = 10000, **kwargs: Any) -> Any:
        try:
            res = self._exporter.export(metrics_data, timeout_millis=timeout_millis, **kwargs)
            if MetricExportResult is not None and res == MetricExportResult.SUCCESS and self._error_count > 0:
                logger.info("tp_trace: Connection to collector metrics at %s restored.", self._endpoint)
                self._error_count = 0
            return res
        except Exception as exc:
            self._error_count += 1
            now = time.monotonic()
            if now - self._last_log_time > 300:
                self._last_log_time = now
                logger.warning(
                    "tp_trace: Unable to export metrics to collector at %s (%s). "
                    "Host application is completely unaffected. (Failures: %d)",
                    self._endpoint,
                    exc,
                    self._error_count,
                )
            if MetricExportResult is not None:
                return MetricExportResult.FAILURE
            return None

    def shutdown(self, timeout_millis: float = 30000, **kwargs: Any) -> None:
        try:
            self._exporter.shutdown(timeout_millis=timeout_millis, **kwargs)
        except Exception:
            pass

    def force_flush(self, timeout_millis: float = 30000) -> bool:
        try:
            return bool(self._exporter.force_flush(timeout_millis=timeout_millis))
        except Exception:
            return False
