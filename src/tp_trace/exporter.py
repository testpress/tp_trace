"""Fail-safe span exporter wrapper for tp_trace.

Guarantees that telemetry export failures (network errors, connection refused,
unreachable collectors, DNS failures, timeouts) NEVER crash or disrupt the host application.
"""

import logging
import time
from typing import Any, Optional
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logger = logging.getLogger("tp_trace.exporter")


def _ensure_normalized_attributes(span: Any) -> None:
    try:
        attrs = getattr(span, "_attributes", None)
        if attrs is None:
            attrs = getattr(span, "attributes", None)
        target_dict = getattr(attrs, "_dict", None)
        if target_dict is None and hasattr(attrs, "__setitem__"):
            target_dict = attrs

        if target_dict is not None:
            is_owned_celery = bool(
                target_dict.get("tp_trace.celery.owned") or (attrs and attrs.get("tp_trace.celery.owned"))
            )
            if is_owned_celery:
                for bad_key in (
                    "http.method",
                    "http.request.method",
                    "http.route",
                    "http.route_template",
                    "django.route",
                ):
                    if bad_key in target_dict:
                        try:
                            del target_dict[bad_key]
                        except Exception:
                            pass
                if not target_dict.get("normalized.service"):
                    target_dict["normalized.service"] = "celery"
                if not target_dict.get("normalized.operation"):
                    from opentelemetry.trace import SpanKind

                    action = "apply_async" if getattr(span, "kind", None) == SpanKind.PRODUCER else "run"
                    target_dict["normalized.operation"] = f"celery.{action}.unknown"
                return

            if not target_dict.get("normalized.service"):
                svc = (
                    target_dict.get("peer.service")
                    or target_dict.get("db.system")
                    or target_dict.get("rpc.system")
                    or target_dict.get("component")
                )
                if svc == "postgresql":
                    svc = "postgres"
                elif svc == "aws-api":
                    svc = "aws-s3"
                target_dict["normalized.service"] = str(svc or "django")

            svc = target_dict.get("normalized.service")
            if not target_dict.get("normalized.operation"):
                op = None
                if svc == "redis" or target_dict.get("db.system") == "redis":
                    if "db.redis.pipeline_length" in target_dict:
                        op = "redis.pipeline"
                    elif target_dict.get("db.operation"):
                        op = f"redis.{str(target_dict.get('db.operation')).lower()}"
                    elif getattr(span, "name", ""):
                        raw = (
                            getattr(span, "name", "")
                            .replace("🔸", "")
                            .replace("REDIS:", "")
                            .replace("redis:", "")
                            .strip()
                            .lower()
                        )
                        op = f"redis.{raw}" if raw else "redis.command"
                    else:
                        op = "redis.command"
                elif svc in ("postgres", "pgbouncer") or target_dict.get("db.system") in ("postgres", "postgresql"):
                    op = target_dict.get("db.statement") or "postgres.query"
                elif svc == "django":
                    route_val = target_dict.get("http.route_template") or target_dict.get("http.route")
                    if target_dict.get("http.method") and route_val:
                        op = f"{target_dict.get('http.method')} {route_val}"
                    else:
                        op = getattr(span, "name", "")
                elif target_dict.get("rpc.service") and target_dict.get("rpc.method"):
                    op = f"{target_dict.get('rpc.service')}.{target_dict.get('rpc.method')}"
                elif target_dict.get("db.statement"):
                    op = target_dict.get("db.statement")
                elif target_dict.get("http.method") and (
                    target_dict.get("http.route_template") or target_dict.get("http.route")
                ):
                    route_val = target_dict.get("http.route_template") or target_dict.get("http.route")
                    op = f"{target_dict.get('http.method')} {route_val}"
                else:
                    op = getattr(span, "name", "")
                if op:
                    target_dict["normalized.operation"] = str(op)
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
