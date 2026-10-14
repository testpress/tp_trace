"""
tp_trace: Python Observability SDK for OpenTelemetry.

Provides complete distributed tracing and request waterfall instrumentation for Django and PostgreSQL.
"""

import atexit
import logging
import threading
from typing import Any, Dict, List, Optional, Union

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    TraceIdRatioBased,
)
from opentelemetry.trace import get_current_span, get_tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagate import set_global_textmap

from tp_trace.config import SDKConfig
from tp_trace.exporter import SafeSpanExporter
from tp_trace.route_context import RouteEnrichingSpanProcessor
from tp_trace.integrations import BaseIntegration, get_integration_manager
from tp_trace.sanitize import sanitize_query_string, sanitize_sql, sanitize_url
from tp_trace.tracing import traced_span
from tp_trace.version import __version__

logger = logging.getLogger("tp_trace")

_INITIALIZED = False
_INIT_LOCK = threading.Lock()
_ACTIVE_PROVIDER: Optional[TracerProvider] = None
_ACTIVE_CONFIG: Optional[SDKConfig] = None
_ATEXIT_REGISTERED = False


def init(
    project: Optional[str] = None,
    project_name: Optional[str] = None,
    cluster_name: Optional[str] = None,
    environment: Optional[str] = None,
    version: Optional[str] = None,
    endpoint: Optional[str] = None,
    traces_endpoint: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    sample_rate: Optional[float] = None,
    disabled: Optional[bool] = None,
    debug: Optional[bool] = None,
    resource_attributes: Optional[Dict[str, Any]] = None,
    integrations: Optional[Dict[str, bool]] = None,
    exporter: Optional[SpanExporter] = None,
    span_processor: Optional[SpanProcessor] = None,
    export_batch: bool = True,
    auto_patch: bool = True,
    extract_trace_context: Optional[Any] = None,
    ignore_endpoints: Optional[List[str]] = None,
    endpoint_sample_rules: Optional[Dict[str, float]] = None,
    metric_readers: Optional[List[Any]] = None,
    **kwargs: Any,
) -> TracerProvider:
    """
    Initialize the tp_trace OpenTelemetry SDK.

    Configures the global TracerProvider, W3C context propagation,
    and OTLP HTTP trace exporter. Thread-safe and idempotent.

    By default, auto_patch=True so all installed integrations (Django,
    PostgreSQL, Redis, requests) are automatically detected and patched.
    A single call in your Django settings.py is all you need:

        import tp_trace
        tp_trace.init(project_name="my-service")

    The project name is auto-detected from Django settings if not provided.
    The OTLP endpoint defaults to OTEL_EXPORTER_OTLP_ENDPOINT env var,
    then http://localhost:4318.

    Resource attributes follow OTel semantic conventions:
      service.name (= the project), service.version, deployment.environment.name, service.instance.id
    In tp_trace's own vocabulary the application is the "project" and a running
    component such as PostgreSQL or Redis is a "service".
    """
    global _INITIALIZED, _ACTIVE_PROVIDER, _ACTIVE_CONFIG

    with _INIT_LOCK:
        if _INITIALIZED and _ACTIVE_PROVIDER is not None:
            logger.debug("tp_trace is already initialized. Returning existing provider.")
            return _ACTIVE_PROVIDER

        try:
            config = SDKConfig.from_env_and_kwargs(
                project=project,
                cluster_name=cluster_name,
                project_name=project_name,
                environment=environment,
                version=version,
                endpoint=endpoint,
                traces_endpoint=traces_endpoint,
                headers=headers,
                sample_rate=sample_rate,
                disabled=disabled,
                debug=debug,
                resource_attributes=resource_attributes,
                integrations=integrations,
                auto_patch=auto_patch,
                extract_trace_context=extract_trace_context,
                ignore_endpoints=ignore_endpoints,
                endpoint_sample_rules=endpoint_sample_rules,
                **kwargs,
            )
        except Exception as exc:
            logger.error(
                "tp_trace: Invalid configuration detected (%s: %s). "
                "Falling back to default safe configuration to prevent host application crash.",
                type(exc).__name__,
                exc,
            )
            config = SDKConfig()
        _ACTIVE_CONFIG = config

        if config.debug:
            logger.setLevel(logging.DEBUG)

        # Build Resource attributes
        resource_data = {
            "service.name": config.project_name,
            "project_name": config.project_name,
            "telemetry.sdk.name": "tp_trace",
            "telemetry.sdk.language": "python",
            "telemetry.sdk.version": __version__,
        }
        if config.cluster_name is not None:
            resource_data["cluster_name"] = str(config.cluster_name)
        if config.environment is not None:
            resource_data["deployment.environment.name"] = str(config.environment)
            resource_data["deployment.environment"] = str(config.environment)
        if config.version is not None:
            resource_data["service.version"] = str(config.version)
        if config.tags:
            resource_data.update(config.tags)
        if config.resource_attributes:
            resource_data.update(config.resource_attributes)
        try:
            import socket, os
            resource_data.setdefault("service.instance.id", f"{socket.gethostname()}-{os.getpid()}")
        except Exception:
            pass
        resource = Resource.create(resource_data)

        # Configure Sampler
        if config.disabled:
            sampler = ALWAYS_OFF
        else:
            from tp_trace.sampler import create_tp_trace_sampler
            sampler = create_tp_trace_sampler(
                global_sample_rate=config.sample_rate,
                ignore_endpoints=config.ignore_endpoints,
                endpoint_sample_rules=config.endpoint_sample_rules,
            )

        # Create TracerProvider
        provider = TracerProvider(resource=resource, sampler=sampler)

        # Enrich spans with the in-flight Django http.route and global tags
        static_span_attrs = {
            "project_name": config.project_name,
            "cluster_name": config.cluster_name,
        }
        if config.tags:
            static_span_attrs.update(config.tags)

        try:
            provider.add_span_processor(RouteEnrichingSpanProcessor(static_attributes=static_span_attrs))
        except Exception as exc:
            logger.debug("tp_trace: Failed to register RouteEnrichingSpanProcessor: %s", exc)

        # Configure Exporter and Processor if not disabled
        if not config.disabled:
            if exporter is None:
                if config.traces_endpoint:
                    otlp_endpoint = config.traces_endpoint
                else:
                    base_ep = config.endpoint.rstrip("/")
                    otlp_endpoint = base_ep if base_ep.endswith("/v1/traces") else f"{base_ep}/v1/traces"

                try:
                    exporter = OTLPSpanExporter(
                        endpoint=otlp_endpoint,
                        headers=config.headers or None,
                    )
                except Exception as exc:
                    logger.warning("tp_trace: Failed to create OTLPSpanExporter: %s", exc)
                    exporter = None

            if exporter is not None:
                # Wrap with SafeSpanExporter: telemetry errors NEVER crash or disrupt the host app
                safe_exporter = SafeSpanExporter(
                    exporter,
                    endpoint=otlp_endpoint if "otlp_endpoint" in locals() else None,
                )

                if span_processor is None:
                    if export_batch:
                        span_processor = BatchSpanProcessor(
                            safe_exporter,
                            max_queue_size=2048,
                            max_export_batch_size=256,
                            schedule_delay_millis=1000,
                        )
                    else:
                        span_processor = SimpleSpanProcessor(safe_exporter)

                provider.add_span_processor(span_processor)

        # Set as global tracer provider
        trace.set_tracer_provider(provider)
        _ACTIVE_PROVIDER = provider
        _INITIALIZED = True

        # Setup W3C Trace Context and Baggage composite propagator
        try:
            set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))
        except Exception:
            set_global_textmap(TraceContextTextMapPropagator())

        # Auto-patch installed integrations if requested
        if config.auto_patch and not config.disabled:
            try:
                manager = get_integration_manager()
                manager.apply_integrations(config=config)
            except Exception as exc:
                logger.debug("Failed to auto-apply integrations during init: %s", exc)

        # Initialize independent RED metrics recorder
        try:
            from tp_trace.metrics import init_metrics
            init_metrics(config=config, metric_readers=metric_readers, resource=resource)
        except Exception as exc:
            logger.debug("tp_trace: Failed to initialize independent RED metrics: %s", exc)

        # Register shutdown on process exit exactly once
        global _ATEXIT_REGISTERED
        if not _ATEXIT_REGISTERED:
            def _shutdown():
                global _ACTIVE_PROVIDER
                try:
                    if _ACTIVE_PROVIDER is not None:
                        _ACTIVE_PROVIDER.shutdown()
                except Exception:
                    pass
                try:
                    from tp_trace.metrics import get_metrics_recorder
                    rec = get_metrics_recorder()
                    if rec is not None and rec.meter_provider is not None:
                        rec.meter_provider.shutdown()
                except Exception:
                    pass

            atexit.register(_shutdown)
            _ATEXIT_REGISTERED = True

        logger.info(
            "tp_trace initialized successfully (project=%s, env=%s, endpoint=%s)",
            config.project_name,
            config.environment,
            config.endpoint,
        )

        return provider


def get_config() -> SDKConfig:
    """Get active SDKConfig or default configuration."""
    if _ACTIVE_CONFIG is not None:
        return _ACTIVE_CONFIG
    return SDKConfig.from_env_and_kwargs()


def patch_all(**kwargs: Any) -> Dict[str, Any]:
    """Auto-detect and apply all installed integrations (Django, PostgreSQL, etc.)."""
    manager = get_integration_manager()
    return manager.apply_integrations(config=get_config(), **kwargs)


def _reset_for_testing() -> None:
    """Internal helper to reset singleton state between unit tests."""
    global _INITIALIZED, _ACTIVE_PROVIDER, _ACTIVE_CONFIG
    with _INIT_LOCK:
        if _ACTIVE_PROVIDER is not None:
            try:
                _ACTIVE_PROVIDER.shutdown()
            except Exception:
                pass
        _INITIALIZED = False
        _ACTIVE_PROVIDER = None
        _ACTIVE_CONFIG = None
        # Uninstrument all active integrations
        try:
            get_integration_manager().uninstrument_all()
        except Exception:
            pass
        # Reset OpenTelemetry global state safely for testing
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = None  # type: ignore
        if hasattr(trace, "_TRACER_PROVIDER_SET_ONCE"):
            try:
                trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore
            except Exception:
                pass
        try:
            from tp_trace.metrics import set_metrics_recorder
            set_metrics_recorder(None)
        except Exception:
            pass
        try:
            from tp_trace.integrations.django.request import _CALLBACK_ARITY_CACHE
            _CALLBACK_ARITY_CACHE.clear()
        except Exception:
            pass




def set_tag(key: str, value: Any) -> None:
    """Set an attribute on the active span AND propagate it to all downstream child spans (DB, Redis, HTTP)."""
    from tp_trace.route_context import set_request_tag
    set_request_tag(key, value)
    span = get_current_span()
    if span and hasattr(span, "set_attribute"):
        try:
            span.set_attribute(key, value)
        except Exception:
            pass


def set_attribute(key: str, value: Any) -> None:
    """Alias for set_tag."""
    set_tag(key, value)

__all__ = [
    "set_tag",
    "set_attribute",
    "init",
    "patch_all",
    "get_config",
    "get_tracer",
    "get_current_span",
    "traced_span",
    "get_integration_manager",
    "BaseIntegration",
    "SDKConfig",
    "sanitize_url",
    "sanitize_sql",
    "__version__",
]
