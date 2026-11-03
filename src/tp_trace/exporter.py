"""Fail-safe span exporter wrapper for tp_trace.

Guarantees that telemetry export failures (network errors, connection refused,
unreachable collectors, DNS failures, timeouts) NEVER crash or disrupt the host application.
"""

import logging
import os
import time
from typing import Any, Optional
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


class AllSpansBatchSpanProcessor(BatchSpanProcessor):
    """BatchSpanProcessor that emits all ended spans to the batch exporter.

    Standard BatchSpanProcessor drops spans where not (span.context and span.context.trace_flags.sampled).
    To support sampling-independent RED metrics in the OpenTelemetry Collector via spanmetrics,
    this processor forwards both sampled spans and RECORD_ONLY unsampled spans.
    The Collector aggregates 100% of finished spans into golden signals and statelessly drops
    unsampled spans before exporting traces to Tempo.
    """

    def on_end(self, span: ReadableSpan) -> None:
        if getattr(self, "done", False):
            return

        if hasattr(self, "_batch_processor"):
            # Modern OTel SDK (>= 1.34 / 1.35)
            self._batch_processor.emit(span)
            return

        # OTel SDK 1.20 - 1.33: deque-based queue with worker thread and fork handling
        try:
            if hasattr(self, "_pid") and self._pid != os.getpid():
                if hasattr(self, "_at_fork_reinit"):
                    if _BSP_RESET_ONCE is not None:
                        _BSP_RESET_ONCE.do_once(self._at_fork_reinit)
                    else:
                        self._at_fork_reinit()

            if hasattr(self, "queue"):
                q = self.queue
                if hasattr(q, "appendleft"):
                    max_q = getattr(self, "max_queue_size", None)
                    if max_q is not None and len(q) == max_q:
                        if hasattr(self, "_spans_dropped") and not getattr(self, "_spans_dropped", False):
                            setattr(self, "_spans_dropped", True)
                    q.appendleft(span)
                    max_batch = getattr(self, "max_export_batch_size", 0)
                    if hasattr(self, "condition") and len(q) >= max_batch:
                        with self.condition:
                            self.condition.notify()
                elif hasattr(q, "put_nowait"):
                    q.put_nowait(span)
            else:
                super().on_end(span)
        except Exception as exc:
            logger.debug("tp_trace: Exception while queuing span: %s", exc)


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
