"""Independent RED Metrics recorder for tp_trace.

Decouples request throughput (calls_total), error rates, and duration histograms
from trace sampling, guaranteeing 100% metric fidelity even when traces are sampled down.
"""

import logging
import os
import socket
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

from tp_trace.config import SDKConfig

logger = logging.getLogger("tp_trace.metrics")

# Explicit bucket boundaries in milliseconds, exactly matching the OTel Collector
# spanmetrics configuration for backwards compatibility with Grafana APM dashboards.
# Reduced from 11 to 8 explicit boundaries (12 to 9 total buckets including +Inf).
LATENCY_BUCKETS_MS: List[float] = [
    5.0,
    25.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
]

_PROCESS_IDENTITY: Optional[Tuple[int, str, str]] = None
_HOSTNAME: Optional[str] = None


def _get_hostname() -> str:
    global _HOSTNAME
    if _HOSTNAME is None:
        try:
            _HOSTNAME = socket.gethostname() or "localhost"
        except Exception:
            _HOSTNAME = "localhost"
    return _HOSTNAME


def get_process_worker_identity(prefix: Optional[str] = None) -> str:
    """Return a process identity bounded to PID space and resolved prefix.

    Lock-free memo keyed by (pid, prefix) that derives fork safety by checking
    os.getpid() on invocation. Multiple services or recorders with different
    prefixes in the same process do not clash.
    """
    global _PROCESS_IDENTITY
    current_pid = os.getpid()
    resolved_prefix = (
        prefix
        or os.environ.get("TP_TRACE_SERVICE_INSTANCE_ID")
        or os.environ.get("OTEL_SERVICE_INSTANCE_ID")
        or _get_hostname()
    )
    cached = _PROCESS_IDENTITY
    if cached is not None and cached[0] == current_pid and cached[1] == resolved_prefix:
        return cached[2]

    identity = f"{resolved_prefix}:{current_pid}"
    _PROCESS_IDENTITY = (current_pid, resolved_prefix, identity)
    return identity


_ACTIVE_RECORDER: Optional["RedMetricsRecorder"] = None


class RedMetricsRecorder:
    """Manages independent RED metric recording for HTTP requests."""

    def __init__(
        self,
        meter_provider: MeterProvider,
        config: Optional[SDKConfig] = None,
        readers: Optional[List[MetricReader]] = None,
    ) -> None:
        self.meter_provider = meter_provider
        self.config = config
        self.worker_id_prefix: str = (
            config.service_instance_id
            if (config and config.service_instance_id)
            else (
                os.environ.get("TP_TRACE_SERVICE_INSTANCE_ID")
                or os.environ.get("OTEL_SERVICE_INSTANCE_ID")
                or _get_hostname()
            )
        )
        self._cached_identity: Optional[Tuple[int, str]] = None
        self.readers: List[MetricReader] = readers or []
        self.meter = meter_provider.get_meter("tp_trace")
        self.metric_cardinality_limit = max(1, int(getattr(config, "metric_cardinality_limit", 5000))) if config else 5000
        self._metric_series_lock = threading.Lock()
        self._metric_series: Set[Tuple[Tuple[str, str], ...]] = set()

        # Instrument contracts matching spanmetrics connector:
        # - calls_total: Counter of completed requests
        # - duration_milliseconds: Histogram of request execution times
        self.calls_total: Counter = self.meter.create_counter(
            name="calls_total",
            description="Total HTTP request calls",
            unit="1",
        )
        self.duration_milliseconds: Histogram = self.meter.create_histogram(
            name="duration_milliseconds",
            description="Duration of HTTP requests in milliseconds",
            unit="ms",
        )
        self.repeated_query_affected_requests_total: Counter = self.meter.create_counter(
            name="apm_repeated_query_affected_requests_total",
            description="Total HTTP requests affected by detected repeated query (potential N+1) patterns",
            unit="1",
        )

    def shutdown(self) -> None:
        """Shut down metric readers and meter provider."""
        try:
            for r in self.readers:
                try:
                    r.shutdown()
                except Exception:
                    pass
            self.meter_provider.shutdown()
        except Exception:
            pass

    def record_request(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        error: bool,
        duration_ms: float,
        service: str = "django",
        operation: Optional[str] = None,
        project_name: Optional[str] = None,
        cluster_name: Optional[str] = None,
        extra_attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record RED metrics for a completed request.

        Fail-safe: Any error during recording is safely absorbed and never disrupts
        the host application.
        """
        try:
            cfg = self.config
            resolved_project = project_name or (cfg.project_name if cfg else None) or "default"
            resolved_cluster = cluster_name or (cfg.cluster_name if cfg else None) or ""
            norm_operation = operation or f"{method} {route}"
            error_str = "true" if error else "false"

            current_pid = os.getpid()
            cached_ident = self._cached_identity
            if cached_ident is not None and cached_ident[0] == current_pid:
                worker_identity = cached_ident[1]
            else:
                worker_identity = get_process_worker_identity(prefix=self.worker_id_prefix)
                self._cached_identity = (current_pid, worker_identity)

            attributes: Dict[str, Any] = {
                "project_name": str(resolved_project),
                "cluster_name": str(resolved_cluster),
                "normalized_service": str(service or "django"),
                "normalized_operation": str(norm_operation),
                "http.method": str(method or "GET"),
                "http.route": str(route or str(status_code)),
                "http.status_code": int(status_code),
                "status_code": str(status_code),
                "span_kind": "SPAN_KIND_SERVER",
                "error": error_str,
                "metric_source": "tp_trace_sdk",
            }

            subdomain = (
                (extra_attributes.get("institute.subdomain") or extra_attributes.get("institute_subdomain") or extra_attributes.get("institute"))
                if extra_attributes
                else None
            )
            if subdomain:
                attributes["institute.subdomain"] = str(subdomain)
                attributes["institute"] = str(subdomain)
                attributes["institute_subdomain"] = str(subdomain)

            if extra_attributes:
                for k, v in extra_attributes.items():
                    if v is not None and k not in attributes:
                        attributes[k] = str(v)

            # Authoritatively set process/worker identity so request tags cannot override it.
            # worker_id isolates independent worker metric streams in multi-process runtimes (Gunicorn).
            attributes["worker_id"] = worker_identity
            attributes = self._bound_metric_attributes(attributes)

            self.calls_total.add(1, attributes)
            self.duration_milliseconds.record(max(0.0, float(duration_ms)), attributes)
        except Exception as exc:
            logger.debug("Failed to record independent RED metrics: %s", exc)

    def record_dependency_span(
        self,
        *,
        service: str,
        operation: str,
        duration_ms: float,
        error: bool = False,
        span_kind: str = "SPAN_KIND_CLIENT",
        route: Optional[str] = None,
        db_instance: Optional[str] = None,
        db_system: Optional[str] = None,
        http_method: Optional[str] = None,
        status_code: Optional[Any] = None,
        project_name: Optional[str] = None,
        cluster_name: Optional[str] = None,
        subdomain: Optional[str] = None,
        extra_attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record RED metrics for a completed dependency span (Postgres, Redis, Requests, Celery).

        Fail-safe: Any error during recording is safely absorbed and never disrupts
        the host application.
        """
        try:
            cfg = self.config
            resolved_project = project_name or (cfg.project_name if cfg else None) or "default"
            resolved_cluster = cluster_name or (cfg.cluster_name if cfg else None) or ""
            error_str = "true" if error else "false"

            current_pid = os.getpid()
            cached_ident = self._cached_identity
            if cached_ident is not None and cached_ident[0] == current_pid:
                worker_identity = cached_ident[1]
            else:
                worker_identity = get_process_worker_identity(prefix=self.worker_id_prefix)
                self._cached_identity = (current_pid, worker_identity)

            attributes: Dict[str, Any] = {
                "project_name": str(resolved_project),
                "cluster_name": str(resolved_cluster),
                "normalized_service": str(service or "unknown"),
                "normalized_operation": str(operation or "unknown"),
                "span_kind": str(span_kind),
                "error": error_str,
                "metric_source": "tp_trace_sdk",
            }

            if route:
                attributes["http.route"] = str(route)
            if http_method:
                attributes["http.method"] = str(http_method)
            if status_code is not None:
                try:
                    attributes["http.status_code"] = int(status_code)
                    attributes["status_code"] = str(status_code)
                except (ValueError, TypeError):
                    attributes["status_code"] = str(status_code)
            if db_system:
                attributes["db.system"] = str(db_system)
            if db_instance:
                attributes["db.instance"] = str(db_instance)

            if subdomain:
                attributes["institute.subdomain"] = str(subdomain)
                attributes["institute"] = str(subdomain)
                attributes["institute_subdomain"] = str(subdomain)
            elif extra_attributes:
                for k in ("institute.subdomain", "institute_subdomain", "institute"):
                    val = extra_attributes.get(k)
                    if val is not None:
                        attributes["institute.subdomain"] = str(val)
                        attributes["institute"] = str(val)
                        attributes["institute_subdomain"] = str(val)
                        break

            attributes["worker_id"] = worker_identity
            attributes = self._bound_metric_attributes(attributes)

            self.calls_total.add(1, attributes)
            self.duration_milliseconds.record(max(0.0, float(duration_ms)), attributes)
        except Exception as exc:
            logger.debug("Failed to record dependency RED metrics: %s", exc)

    def _bound_metric_attributes(self, attributes: Dict[str, Any]) -> Dict[str, Any]:
        """Keep the total number of emitted label combinations bounded."""
        key = tuple(sorted((str(k), str(v)) for k, v in attributes.items()))
        with self._metric_series_lock:
            if key in self._metric_series:
                return attributes
            if len(self._metric_series) < self.metric_cardinality_limit:
                self._metric_series.add(key)
                return attributes

        # Never retain incoming label values or keys in overflow state. Even
        # apparently stable dimensions (method, status, service) can vary without
        # bound. Two fixed series retain the aggregate error split without growing
        # the tracking set beyond the configured detailed-combination budget.
        cfg = self.config
        return {
            "project_name": str(cfg.project_name if cfg else "default"),
            "cluster_name": str(cfg.cluster_name if cfg else ""),
            "worker_id": get_process_worker_identity(prefix=self.worker_id_prefix),
            "metric_source": "tp_trace_sdk",
            "normalized_service": "__other__",
            "normalized_operation": "__other__",
            "span_kind": "__other__",
            "http.route": "__other__",
            "db.instance": "__other__",
            "institute": "__other__",
            "institute.subdomain": "__other__",
            "institute_subdomain": "__other__",
            "error": "true" if attributes.get("error") == "true" else "false",
        }

    def record_repeated_query_affected_request(
        self,
        *,
        method: str,
        route: str,
        service: str = "django",
        project_name: Optional[str] = None,
        cluster_name: Optional[str] = None,
        extra_attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record one request affected by a repeated-query candidate.

        Fail-safe: Any error during recording is safely absorbed and never disrupts
        the host application.
        Strict low-cardinality label allowlist: only normalized service, http.route, http.method,
        project_name, cluster_name, metric_source, and optionally institute.subdomain.
        Never emits raw SQL, query text, worker_id, or trace IDs.
        """
        try:
            cfg = self.config
            resolved_project = project_name or (cfg.project_name if cfg else None) or "default"
            resolved_cluster = cluster_name or (cfg.cluster_name if cfg else None) or ""

            attributes: Dict[str, Any] = {
                "project_name": str(resolved_project),
                "cluster_name": str(resolved_cluster),
                "normalized_service": str(service or "django"),
                "http.method": str(method or "GET"),
                "http.route": str(route),
                "metric_source": "tp_trace_sdk",
            }

            if extra_attributes:
                # Strictly allow only institute.subdomain (or institute_subdomain)
                for k in ("institute.subdomain", "institute_subdomain"):
                    val = extra_attributes.get(k)
                    if val is not None:
                        attributes["institute.subdomain"] = str(val)
                        break

            self.repeated_query_affected_requests_total.add(1, attributes)
        except Exception as exc:
            logger.debug("Failed to record repeated query anomaly metric: %s", exc)


def create_red_metrics_recorder(
    config: Optional[SDKConfig] = None,
    metric_readers: Optional[List[MetricReader]] = None,
    resource: Optional[Resource] = None,
) -> RedMetricsRecorder:
    """Create and configure a RedMetricsRecorder with standardized histogram views."""
    if metric_readers is None:
        readers: List[MetricReader] = []
        if config and not config.disabled and getattr(config, "metrics_enabled", True):
            if getattr(config, "metrics_endpoint", None):
                otlp_metrics_ep = config.metrics_endpoint
            else:
                base_ep = config.endpoint.rstrip("/")
                otlp_metrics_ep = base_ep if base_ep.endswith("/v1/metrics") else f"{base_ep}/v1/metrics"

            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
                from tp_trace.exporter import SafeMetricExporter

                raw_exporter = OTLPMetricExporter(
                    endpoint=otlp_metrics_ep,
                    headers=config.headers or None,
                )
                safe_exporter = SafeMetricExporter(raw_exporter, endpoint=otlp_metrics_ep)
                interval_ms = getattr(config, "metrics_export_interval_millis", 60000)
                reader = PeriodicExportingMetricReader(
                    safe_exporter,
                    export_interval_millis=interval_ms,
                    export_timeout_millis=interval_ms,
                )
                readers.append(reader)
                logger.info(
                    "tp_trace: Configured OTLP metrics export to %s (interval=%dms)", otlp_metrics_ep, interval_ms
                )
            except Exception as exc:
                logger.warning("tp_trace: Failed to configure OTLPMetricExporter: %s", exc)
    else:
        readers = metric_readers

    # Configure latency histogram view with explicit APM buckets
    latency_view = View(
        instrument_name="duration_milliseconds",
        aggregation=ExplicitBucketHistogramAggregation(boundaries=LATENCY_BUCKETS_MS),
    )

    if resource is None:
        res_attrs: Dict[str, Any] = {}
        if config:
            if config.project_name:
                res_attrs["service.name"] = config.project_name
                res_attrs["project_name"] = config.project_name
            if config.cluster_name:
                res_attrs["cluster_name"] = config.cluster_name
            if config.environment:
                res_attrs["deployment.environment"] = config.environment
        resource = Resource.create(res_attrs)

    provider = MeterProvider(
        resource=resource,
        metric_readers=readers,
        views=[latency_view],
    )
    return RedMetricsRecorder(meter_provider=provider, config=config, readers=readers)


def init_metrics(
    config: Optional[SDKConfig] = None,
    metric_readers: Optional[List[MetricReader]] = None,
    resource: Optional[Resource] = None,
) -> RedMetricsRecorder:
    """Initialize the global RED metrics recorder."""
    global _ACTIVE_RECORDER
    recorder = create_red_metrics_recorder(
        config=config,
        metric_readers=metric_readers,
        resource=resource,
    )
    _ACTIVE_RECORDER = recorder
    return recorder


def get_metrics_recorder() -> Optional[RedMetricsRecorder]:
    """Get the active RED metrics recorder, if initialized."""
    return _ACTIVE_RECORDER


def set_metrics_recorder(recorder: Optional[RedMetricsRecorder]) -> None:
    """Set or override the active RED metrics recorder."""
    global _ACTIVE_RECORDER
    _ACTIVE_RECORDER = recorder


def record_request(
    *,
    method: str,
    route: str,
    status_code: int,
    error: bool,
    duration_ms: float,
    service: str = "django",
    operation: Optional[str] = None,
    project_name: Optional[str] = None,
    cluster_name: Optional[str] = None,
    extra_attributes: Optional[Dict[str, Any]] = None,
) -> None:
    """Record request RED metrics using the active recorder.

    Idempotent and safe: if no recorder is active or disabled, this is a no-op.
    """
    recorder = _ACTIVE_RECORDER
    if recorder is None:
        return

    if recorder.config and recorder.config.disabled:
        return

    recorder.record_request(
        method=method,
        route=route,
        status_code=status_code,
        error=error,
        duration_ms=duration_ms,
        service=service,
        operation=operation,
        project_name=project_name,
        cluster_name=cluster_name,
        extra_attributes=extra_attributes,
    )


def record_dependency_span(
    *,
    service: str,
    operation: str,
    duration_ms: float,
    error: bool = False,
    span_kind: str = "SPAN_KIND_CLIENT",
    route: Optional[str] = None,
    db_instance: Optional[str] = None,
    db_system: Optional[str] = None,
    http_method: Optional[str] = None,
    status_code: Optional[Any] = None,
    project_name: Optional[str] = None,
    cluster_name: Optional[str] = None,
    subdomain: Optional[str] = None,
    extra_attributes: Optional[Dict[str, Any]] = None,
) -> None:
    """Record RED metrics for a completed dependency span.

    Idempotent and safe: if no recorder is active or disabled, this is a no-op.
    """
    recorder = _ACTIVE_RECORDER
    if recorder is None:
        return

    if recorder.config and recorder.config.disabled:
        return

    recorder.record_dependency_span(
        service=service,
        operation=operation,
        duration_ms=duration_ms,
        error=error,
        span_kind=span_kind,
        route=route,
        db_instance=db_instance,
        db_system=db_system,
        http_method=http_method,
        status_code=status_code,
        project_name=project_name,
        cluster_name=cluster_name,
        subdomain=subdomain,
        extra_attributes=extra_attributes,
    )


def record_repeated_query_affected_request(
    *,
    method: str,
    route: str,
    service: str = "django",
    project_name: Optional[str] = None,
    cluster_name: Optional[str] = None,
    extra_attributes: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one request affected by a repeated-query candidate.

    Idempotent and safe: if no recorder is active or disabled, this is a no-op.
    """
    recorder = _ACTIVE_RECORDER
    if recorder is None:
        return

    if recorder.config and recorder.config.disabled:
        return

    recorder.record_repeated_query_affected_request(
        method=method,
        route=route,
        service=service,
        project_name=project_name,
        cluster_name=cluster_name,
        extra_attributes=extra_attributes,
    )


record_repeated_query_pattern = record_repeated_query_affected_request
