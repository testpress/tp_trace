"""Configuration handling for tp_trace SDK."""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
import math

from tp_trace.version import __version__

logger = logging.getLogger("tp_trace.config")

# Deprecated inputs that name the *project* after a *service*. Warned once each so
# operators learn the canonical name without log spam.
_warned_project_aliases: set = set()

_TRUE_VALUES = {"1", "true", "yes", "on", "t", "y"}
_FALSE_VALUES = {"0", "false", "no", "off", "f", "n"}


def _str_to_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if not s:
        return default
    if s in _TRUE_VALUES:
        return True
    if s in _FALSE_VALUES:
        return False
    raise ValueError(
        f"tp_trace: cannot parse {val!r} as boolean; "
        f"accepted values: {sorted(_TRUE_VALUES | _FALSE_VALUES)}"
    )


def _parse_rate(val: Any, field_name: str = "sample_rate") -> float:
    if val is None:
        return 1.0
    try:
        r = float(val)
    except (TypeError, ValueError):
        raise ValueError(f"tp_trace: {field_name}={val!r} is not a valid number")
    if math.isnan(r) or math.isinf(r):
        raise ValueError(f"tp_trace: {field_name}={val!r} must be finite")
    if not (0.0 <= r <= 1.0):
        raise ValueError(f"tp_trace: {field_name}={val!r} is outside valid range [0.0, 1.0]")
    return r


_QUERY_MODES = {"expose", "redact", "strip"}


def _parse_query_mode(val: Any) -> str:
    """Parse TP_TRACE_QUERY_MODE: expose (raw), redact (credential keys -> REDACTED), strip (drop query)."""
    if val is None:
        return "expose"
    mode = str(val).strip().lower()
    if not mode:
        return "expose"
    if mode not in _QUERY_MODES:
        raise ValueError(
            f"tp_trace: cannot parse {val!r} as query_mode; "
            f"accepted values: {sorted(_QUERY_MODES)}"
        )
    return mode


def _parse_cmd_max_length(val: Any) -> int:
    if val is None:
        return 1000
    try:
        n = int(val)
    except (TypeError, ValueError):
        raise ValueError(f"tp_trace: redis_cmd_max_length={val!r} is not a valid integer")
    if n <= 0:
        raise ValueError(f"tp_trace: redis_cmd_max_length={val!r} must be a positive integer")
    return n


def _parse_endpoint_sample_rules(val: Any) -> Dict[str, float]:
    """Parse and validate endpoint-specific sampling rules.

    Accepts:
    - Dict[str, float]: mapping of route patterns to sampling rates in [0.0, 1.0].
    - str: comma-separated 'pattern=rate' pairs (e.g. '/api/checkout/*=1.0,/health=0.0').
    - None: returns empty dict.

    Raises:
    - ValueError: if any pattern is empty, rate is invalid/out-of-bounds, or format is malformed.
    """
    if val is None:
        return {}

    rules: Dict[str, float] = {}

    if isinstance(val, str):
        val = val.strip()
        if not val:
            return {}
        items = [item.strip() for item in val.split(",") if item.strip()]
        for item in items:
            if "=" not in item:
                raise ValueError(
                    f"tp_trace: invalid endpoint sample rule {item!r}; expected format 'pattern=rate'"
                )
            pattern, rate_str = item.split("=", 1)
            pattern = pattern.strip()
            if not pattern:
                raise ValueError(
                    f"tp_trace: invalid endpoint sample rule {item!r}; pattern cannot be empty"
                )
            rate_str = rate_str.strip()
            if not rate_str:
                raise ValueError(
                    f"tp_trace: invalid endpoint sample rule {item!r}; rate cannot be empty"
                )
            rate = _parse_rate(rate_str, field_name=f"endpoint_sample_rules[{pattern!r}]")
            rules[pattern] = rate
        return rules

    if isinstance(val, dict):
        for pattern, rate_val in val.items():
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError(
                    f"tp_trace: invalid endpoint pattern {pattern!r}; pattern must be a non-empty string"
                )
            pattern_clean = pattern.strip()
            rate = _parse_rate(rate_val, field_name=f"endpoint_sample_rules[{pattern_clean!r}]")
            rules[pattern_clean] = rate
        return rules

    raise ValueError(
        f"tp_trace: endpoint_sample_rules must be a dict or comma-separated string, got {type(val).__name__}"
    )


def _detect_django_project_name() -> Optional[str]:
    """Try to auto-detect project name from Django settings."""
    try:
        from django.conf import settings
        # Check common Django settings for project/app name
        return (
            getattr(settings, "TP_TRACE_PROJECT_NAME", None)
            or getattr(settings, "TP_DOG_PROJECT_NAME", None)
            or getattr(settings, "OTEL_PROJECT_NAME", None)
            or getattr(settings, "PROJECT_NAME", None)
        )
    except Exception:
        return None


def _detect_django_env() -> Optional[str]:
    """Try to auto-detect environment from Django settings."""
    try:
        from django.conf import settings
        return (
            getattr(settings, "TP_TRACE_ENVIRONMENT", None)
            or getattr(settings, "TP_DOG_ENVIRONMENT", None)
            or getattr(settings, "ENVIRONMENT", None)
            or getattr(settings, "ENV", None)
        )
    except Exception:
        return None


def _detect_django_cluster_name() -> Optional[str]:
    """Try to auto-detect cluster name from Django settings."""
    try:
        from django.conf import settings
        return (
            getattr(settings, "TP_TRACE_CLUSTER_NAME", None)
            or getattr(settings, "TP_TRACE_CLUSTER", None)
            or getattr(settings, "TP_DOG_CLUSTER_NAME", None)
            or getattr(settings, "TP_DOG_CLUSTER", None)
            or getattr(settings, "OTEL_CLUSTER_NAME", None)
            or getattr(settings, "CLUSTER_NAME", None)
        )
    except Exception:
        return None


def _parse_headers(headers_str: Optional[str]) -> Dict[str, str]:
    if not headers_str:
        return {}
    headers = {}
    for item in headers_str.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            headers[k.strip()] = v.strip()
    return headers


DEFAULT_EXCLUDE_PATTERNS = [
    "django/forms/*",
    "debug_toolbar/*",
    "*/widgets/*",
]


@dataclass
class SDKConfig:
    """Configuration options for tp_trace SDK."""

    project_name: str = "unknown-project"
    cluster_name: Optional[str] = None
    environment: str = "development"
    version: str = __version__
    endpoint: str = "http://localhost:4318"
    traces_endpoint: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    sample_rate: float = 1.0
    disabled: bool = False
    debug: bool = False
    resource_attributes: Dict[str, Any] = field(default_factory=dict)
    integrations: Dict[str, bool] = field(default_factory=dict)
    trace_nested_templates: bool = True
    template_enabled: bool = True
    cache_enabled: bool = True
    template_exclude: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS))
    tags: Dict[str, Any] = field(default_factory=dict)
    on_request_span: Optional[Callable] = None
    ignore_endpoints: List[str] = field(default_factory=list)
    endpoint_sample_rules: Dict[str, float] = field(default_factory=dict)
    trusted_proxies: List[str] = field(default_factory=list)
    db_role_map: Dict[str, str] = field(default_factory=dict)
    auto_patch: bool = True
    extract_trace_context: Any = True
    metrics_endpoint: Optional[str] = None
    metrics_enabled: bool = True
    metrics_export_interval_millis: int = 5000
    # Masking -> exposure toggles. Defaults expose infrastructure details
    # (Redis endpoint, Redis command contents, URL query strings), matching
    # dd-trace. Deployments that do NOT own the infra can restore masking.
    redis_mask_endpoint: bool = False
    redis_mask_command: bool = False
    redis_cmd_max_length: int = 1000
    query_mode: str = "expose"

    @classmethod
    def from_env_and_kwargs(
        cls,
        project: Optional[str] = None,
        project_name: Optional[str] = None,
        cluster_name: Optional[str] = None,
        tags: Optional[Dict[str, Any]] = None,
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
        auto_patch: Optional[bool] = None,
        extract_trace_context: Optional[Any] = None,
        ignore_endpoints: Optional[List[str]] = None,
        endpoint_sample_rules: Optional[Dict[str, float]] = None,
        metrics_endpoint: Optional[str] = None,
        metrics_enabled: Optional[bool] = None,
        metrics_export_interval_millis: Optional[int] = None,
        redis_mask_endpoint: Optional[bool] = None,
        redis_mask_command: Optional[bool] = None,
        redis_cmd_max_length: Optional[int] = None,
        query_mode: Optional[str] = None,
        **extra: Any,
    ) -> "SDKConfig":
        """Build SDKConfig by prioritizing explicit kwargs over environment variables."""

        # 1. Project Name (auto-detect from Django settings if available)
        # Canonical vocabulary: ``project`` = the instrumented application;
        # ``service`` = a running component (django/postgres/redis). The
        # ``service``/``service_name`` sources below are legacy aliases for the
        # project and only kept for backwards compatibility.
        for _alias in (
            ("service_name=", extra.get("service_name")),
            ("service=", extra.get("service")),
            ("TP_TRACE_SERVICE_NAME", os.getenv("TP_TRACE_SERVICE_NAME")),
            ("TP_DOG_SERVICE_NAME", os.getenv("TP_DOG_SERVICE_NAME")),
            ("OTEL_SERVICE_NAME", os.getenv("OTEL_SERVICE_NAME")),
        ):
            _name, _value = _alias
            if _value and _name not in _warned_project_aliases:
                _warned_project_aliases.add(_name)
                logger.warning(
                    "tp_trace: '%s' sets the project identity, not a running service. "
                    "Use project_name / TP_TRACE_PROJECT_NAME instead.",
                    _name,
                )
        resolved_project = (
            project_name
            or project
            or extra.get("service_name")
            or extra.get("service")
            or os.getenv("TP_TRACE_PROJECT_NAME")
            or os.getenv("TP_TRACE_PROJECT")
            or os.getenv("TP_TRACE_SERVICE_NAME")
            or os.getenv("TP_DOG_PROJECT_NAME")
            or os.getenv("TP_DOG_PROJECT")
            or os.getenv("TP_DOG_SERVICE_NAME")
            or os.getenv("OTEL_SERVICE_NAME")
            or os.getenv("OTEL_PROJECT_NAME")
            or _detect_django_project_name()
            or "unknown-project"
        )

        # 2. Cluster Name
        resolved_cluster = (
            cluster_name
            or os.getenv("TP_TRACE_CLUSTER_NAME")
            or os.getenv("TP_TRACE_CLUSTER")
            or os.getenv("TP_DOG_CLUSTER_NAME")
            or os.getenv("TP_DOG_CLUSTER")
            or os.getenv("OTEL_CLUSTER_NAME")
            or _detect_django_cluster_name()
            or "unknown-cluster"
        )

        # 2. Environment (auto-detect from Django settings if available)
        resolved_env = (
            environment
            or os.getenv("TP_TRACE_ENV")
            or os.getenv("TP_TRACE_ENVIRONMENT")
            or os.getenv("TP_DOG_ENV")
            or os.getenv("TP_DOG_ENVIRONMENT")
            or os.getenv("OTEL_ENVIRONMENT")
            or os.getenv("DEPLOYMENT_ENVIRONMENT")
            or os.getenv("ENVIRONMENT")
            or os.getenv("ENV")
            or _detect_django_env()
            or "development"
        )

        # 3. Version
        resolved_version = (
            version
            or os.getenv("TP_TRACE_VERSION")
            or os.getenv("TP_DOG_VERSION")
            or os.getenv("OTEL_SERVICE_VERSION")
            or os.getenv("SERVICE_VERSION")
            or __version__
        )

        # 4. Endpoints & Headers
        resolved_endpoint = (
            endpoint
            or os.getenv("TP_TRACE_ENDPOINT")
            or os.getenv("TP_DOG_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
            or "http://localhost:4318"
        )

        resolved_traces_endpoint = (
            traces_endpoint
            or os.getenv("TP_TRACE_TRACES_ENDPOINT")
            or os.getenv("TP_DOG_TRACES_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or None
        )

        resolved_metrics_endpoint = (
            metrics_endpoint
            or extra.get("metrics_endpoint")
            or os.getenv("TP_TRACE_METRICS_ENDPOINT")
            or os.getenv("TP_DOG_METRICS_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
            or None
        )

        if metrics_enabled is not None:
            resolved_metrics_enabled = bool(metrics_enabled)
        elif "metrics_enabled" in extra:
            resolved_metrics_enabled = bool(extra["metrics_enabled"])
        else:
            resolved_metrics_enabled = _str_to_bool(
                os.getenv("TP_TRACE_METRICS_ENABLED")
                or os.getenv("TP_DOG_METRICS_ENABLED"),
                default=True,
            )

        try:
            raw_interval = (
                metrics_export_interval_millis
                if metrics_export_interval_millis is not None
                else extra.get("metrics_export_interval_millis")
                or os.getenv("TP_TRACE_METRICS_EXPORT_INTERVAL_MILLIS")
                or os.getenv("TP_DOG_METRICS_EXPORT_INTERVAL_MILLIS")
                or os.getenv("OTEL_METRIC_EXPORT_INTERVAL")
            )
            resolved_metrics_interval = int(raw_interval) if raw_interval is not None else 5000
        except Exception:
            resolved_metrics_interval = 5000

        env_headers = _parse_headers(
            os.getenv("TP_TRACE_HEADERS")
            or os.getenv("TP_DOG_HEADERS")
            or os.getenv("OTEL_EXPORTER_OTLP_HEADERS")
        )
        merged_headers = dict(env_headers)
        if headers:
            merged_headers.update(headers)

        # 5. Sample rate
        raw_rate = (
            sample_rate
            if sample_rate is not None
            else os.getenv("TP_TRACE_SAMPLE_RATE")
            or os.getenv("TP_DOG_SAMPLE_RATE")
            or os.getenv("OTEL_TRACES_SAMPLER_ARG")
            or os.getenv("SAMPLE_RATE")
        )
        resolved_sample_rate = _parse_rate(raw_rate, field_name="sample_rate") if raw_rate is not None else 1.0

        # 6. Disabled
        if disabled is not None:
            resolved_disabled = bool(disabled)
        else:
            resolved_disabled = _str_to_bool(
                os.getenv("TP_TRACE_DISABLED")
                or os.getenv("TP_DOG_DISABLED")
                or os.getenv("OTEL_SDK_DISABLED"),
                default=False,
            )

        # 7. Debug
        if debug is not None:
            resolved_debug = bool(debug)
        else:
            resolved_debug = _str_to_bool(
                os.getenv("TP_TRACE_DEBUG")
                or os.getenv("TP_DOG_DEBUG")
                or os.getenv("OTEL_LOG_LEVEL") == "debug",
                default=False,
            )

        # 8. Extra resource attributes & integrations
        res_attrs = dict(resource_attributes or {})
        integs = dict(integrations or {})

        # 9. Trace nested templates (default True matching Datadog APM behavior)
        if "trace_nested_templates" in extra and extra["trace_nested_templates"] is not None:
            resolved_trace_nested = _str_to_bool(extra["trace_nested_templates"], default=True)
        else:
            resolved_trace_nested = _str_to_bool(
                os.getenv("TP_TRACE_DJANGO_TRACE_NESTED_TEMPLATES")
                or os.getenv("TP_DOG_DJANGO_TRACE_NESTED_TEMPLATES")
                or os.getenv("OTEL_PYTHON_DJANGO_TRACE_NESTED_TEMPLATES"),
                default=True,
            )

        # 10. Template instrumentation config (enabled + exclude patterns)
        default_exclude = list(DEFAULT_EXCLUDE_PATTERNS)
        tmpl_config = extra.get("template_instrumentation") or extra.get("TEMPLATE_INSTRUMENTATION")
        if isinstance(tmpl_config, dict):
            resolved_template_enabled = _str_to_bool(tmpl_config.get("enabled", True), default=True)
            resolved_template_exclude = tmpl_config.get("exclude", default_exclude)
        else:
            resolved_template_enabled = _str_to_bool(extra.get("template_enabled", True), default=True)
            if "template_exclude" in extra and extra["template_exclude"] is not None:
                resolved_template_exclude = extra["template_exclude"]
            elif "TP_TRACE_TEMPLATE_EXCLUDE" in os.environ:
                resolved_template_exclude = [
                    p.strip() for p in os.environ["TP_TRACE_TEMPLATE_EXCLUDE"].split(",") if p.strip()
                ]
            elif "TP_DOG_TEMPLATE_EXCLUDE" in os.environ:
                resolved_template_exclude = [
                    p.strip() for p in os.environ["TP_DOG_TEMPLATE_EXCLUDE"].split(",") if p.strip()
                ]
            else:
                resolved_template_exclude = default_exclude

        # 10b. Cache instrumentation config
        if "cache_enabled" in extra and extra["cache_enabled"] is not None:
            resolved_cache_enabled = _str_to_bool(extra["cache_enabled"], default=True)
        elif "TP_TRACE_CACHE_ENABLED" in os.environ:
            resolved_cache_enabled = _str_to_bool(os.environ["TP_TRACE_CACHE_ENABLED"], default=True)
        elif "TP_DOG_CACHE_ENABLED" in os.environ:
            resolved_cache_enabled = _str_to_bool(os.environ["TP_DOG_CACHE_ENABLED"], default=True)
        else:
            resolved_cache_enabled = True

        # 11. Static tags / key-value metadata (applied to all spans and resources)
        # Explicit kwargs tags override env vars entirely (consistent with other settings)
        kwarg_tags = tags if tags is not None else extra.get("tags")
        if kwarg_tags is not None:
            resolved_tags = dict(kwarg_tags)
        else:
            resolved_tags = {}
            env_tags_str = os.getenv("TP_TRACE_TAGS") or os.getenv("TP_DOG_TAGS") or os.getenv("OTEL_RESOURCE_ATTRIBUTES")
            if env_tags_str:
                for item in env_tags_str.split(","):
                    item = item.strip()
                    if "=" in item:
                        k, v = item.split("=", 1)
                        resolved_tags[k.strip()] = v.strip()

        # Merge custom arbitrary extra kwargs (e.g. server_location="us-east-1", team="core")
        KNOWN_EXTRA_KEYS = {
            "trace_nested_templates", "template_instrumentation", "TEMPLATE_INSTRUMENTATION",
            "template_enabled", "template_exclude", "tags",
            # Retired knobs, still listed so a stale caller cannot turn them into
            # a resource tag via the custom-extra-kwargs merge below.
            "db_two_tier_spans",
            "on_request_span", "ignore_endpoints", "IGNORE_ENDPOINTS",
            "endpoint_sample_rules", "endpoint_rules", "sample_rules", "ENDPOINT_SAMPLE_RULES",
            "service", "service_name", "cluster",
            "db_role_map", "DB_ROLE_MAP", "trusted_proxies", "TRUSTED_PROXIES",
            "cache_enabled", "CACHE_ENABLED", "auto_patch", "AUTO_PATCH",
            "extract_trace_context", "EXTRACT_TRACE_CONTEXT",
        }
        for k, v in extra.items():
            if k not in KNOWN_EXTRA_KEYS and not k.startswith("_"):
                resolved_tags[k] = v

        # 12. Per-request span callback
        resolved_on_request_span = extra.get("on_request_span")

        # 13. Endpoint sampling rules & ignores
        kwarg_ignores = None
        for candidate in (
            ignore_endpoints,
            extra.get("ignore_endpoints"),
            extra.get("IGNORE_ENDPOINTS"),
        ):
            if candidate is not None:
                kwarg_ignores = candidate
                break

        if kwarg_ignores is not None:
            if isinstance(kwarg_ignores, str):
                resolved_ignores = [p.strip() for p in kwarg_ignores.split(",") if p.strip()]
            else:
                resolved_ignores = list(kwarg_ignores)
        elif "TP_TRACE_IGNORE_ENDPOINTS" in os.environ:
            resolved_ignores = [p.strip() for p in os.environ["TP_TRACE_IGNORE_ENDPOINTS"].split(",") if p.strip()]
        elif "TP_DOG_IGNORE_ENDPOINTS" in os.environ:
            resolved_ignores = [p.strip() for p in os.environ["TP_DOG_IGNORE_ENDPOINTS"].split(",") if p.strip()]
        else:
            resolved_ignores = []

        kwarg_rules = None
        for candidate in (
            endpoint_sample_rules,
            extra.get("endpoint_sample_rules"),
            extra.get("endpoint_rules"),
            extra.get("sample_rules"),
            extra.get("ENDPOINT_SAMPLE_RULES"),
        ):
            if candidate is not None:
                kwarg_rules = candidate
                break

        if kwarg_rules is not None:
            resolved_rules = _parse_endpoint_sample_rules(kwarg_rules)
        elif "TP_TRACE_ENDPOINT_SAMPLE_RULES" in os.environ:
            resolved_rules = _parse_endpoint_sample_rules(os.environ["TP_TRACE_ENDPOINT_SAMPLE_RULES"])
        elif "TP_DOG_ENDPOINT_SAMPLE_RULES" in os.environ:
            resolved_rules = _parse_endpoint_sample_rules(os.environ["TP_DOG_ENDPOINT_SAMPLE_RULES"])
        else:
            resolved_rules = {}

        kwarg_proxies = None
        for candidate in (
            extra.get("trusted_proxies"),
            extra.get("TRUSTED_PROXIES"),
        ):
            if candidate is not None:
                kwarg_proxies = candidate
                break

        if kwarg_proxies is not None:
            if isinstance(kwarg_proxies, str):
                resolved_trusted_proxies = [p.strip() for p in kwarg_proxies.split(",") if p.strip()]
            else:
                resolved_trusted_proxies = list(kwarg_proxies)
        elif "TP_TRACE_TRUSTED_PROXIES" in os.environ:
            resolved_trusted_proxies = [p.strip() for p in os.environ["TP_TRACE_TRUSTED_PROXIES"].split(",") if p.strip()]
        elif "TP_DOG_TRUSTED_PROXIES" in os.environ:
            resolved_trusted_proxies = [p.strip() for p in os.environ["TP_DOG_TRUSTED_PROXIES"].split(",") if p.strip()]
        else:
            resolved_trusted_proxies = []

        kwarg_db_role_map = None
        for candidate in (
            extra.get("db_role_map"),
            extra.get("DB_ROLE_MAP"),
        ):
            if candidate is not None:
                kwarg_db_role_map = candidate
                break

        if kwarg_db_role_map is not None:
            resolved_db_role_map = dict(kwarg_db_role_map)
        elif "TP_TRACE_DB_ROLE_MAP" in os.environ:
            resolved_db_role_map = {}
            for item in os.environ["TP_TRACE_DB_ROLE_MAP"].split(","):
                item = item.strip()
                if "=" in item:
                    a, r = item.split("=", 1)
                    resolved_db_role_map[a.strip()] = r.strip()
        elif "TP_DOG_DB_ROLE_MAP" in os.environ:
            resolved_db_role_map = {}
            for item in os.environ["TP_DOG_DB_ROLE_MAP"].split(","):
                item = item.strip()
                if "=" in item:
                    a, r = item.split("=", 1)
                    resolved_db_role_map[a.strip()] = r.strip()
        else:
            resolved_db_role_map = {}

        # 14. Auto-patch
        if auto_patch is not None:
            resolved_auto_patch = bool(auto_patch)
        elif "auto_patch" in extra and extra["auto_patch"] is not None:
            resolved_auto_patch = _str_to_bool(extra["auto_patch"], default=True)
        elif "TP_TRACE_AUTO_PATCH" in os.environ:
            resolved_auto_patch = _str_to_bool(os.environ["TP_TRACE_AUTO_PATCH"], default=True)
        elif "TP_DOG_AUTO_PATCH" in os.environ:
            resolved_auto_patch = _str_to_bool(os.environ["TP_DOG_AUTO_PATCH"], default=True)
        else:
            resolved_auto_patch = True

        # 15. Extract trace context (W3C propagation from incoming HTTP requests)
        kwarg_extract = (
            extract_trace_context
            if extract_trace_context is not None
            else (extra.get("extract_trace_context") if "extract_trace_context" in extra else extra.get("EXTRACT_TRACE_CONTEXT"))
        )
        if kwarg_extract is not None:
            if callable(kwarg_extract):
                resolved_extract = kwarg_extract
            else:
                resolved_extract = _str_to_bool(kwarg_extract, default=True)
        elif "TP_TRACE_EXTRACT_TRACE_CONTEXT" in os.environ:
            resolved_extract = _str_to_bool(os.environ["TP_TRACE_EXTRACT_TRACE_CONTEXT"], default=True)
        elif "TP_DOG_EXTRACT_TRACE_CONTEXT" in os.environ:
            resolved_extract = _str_to_bool(os.environ["TP_DOG_EXTRACT_TRACE_CONTEXT"], default=True)
        elif "OTEL_PROPAGATORS" in os.environ and os.environ["OTEL_PROPAGATORS"].strip().lower() in ("none", "off", "0"):
            resolved_extract = False
        else:
            resolved_extract = True

        # 16. Masking -> exposure toggles (defaults expose, matching dd-trace)
        if redis_mask_endpoint is not None:
            resolved_redis_mask_endpoint = bool(redis_mask_endpoint)
        elif "TP_TRACE_REDIS_MASK_ENDPOINT" in os.environ:
            resolved_redis_mask_endpoint = _str_to_bool(os.environ["TP_TRACE_REDIS_MASK_ENDPOINT"], default=False)
        elif "TP_DOG_REDIS_MASK_ENDPOINT" in os.environ:
            resolved_redis_mask_endpoint = _str_to_bool(os.environ["TP_DOG_REDIS_MASK_ENDPOINT"], default=False)
        else:
            resolved_redis_mask_endpoint = False

        if redis_mask_command is not None:
            resolved_redis_mask_command = bool(redis_mask_command)
        elif "TP_TRACE_REDIS_MASK_COMMAND" in os.environ:
            resolved_redis_mask_command = _str_to_bool(os.environ["TP_TRACE_REDIS_MASK_COMMAND"], default=False)
        elif "TP_DOG_REDIS_MASK_COMMAND" in os.environ:
            resolved_redis_mask_command = _str_to_bool(os.environ["TP_DOG_REDIS_MASK_COMMAND"], default=False)
        else:
            resolved_redis_mask_command = False

        raw_cmd_max_length = (
            redis_cmd_max_length
            if redis_cmd_max_length is not None
            else os.getenv("TP_TRACE_REDIS_CMD_MAX_LENGTH")
            or os.getenv("TP_DOG_REDIS_CMD_MAX_LENGTH")
        )
        resolved_cmd_max_length = _parse_cmd_max_length(raw_cmd_max_length)

        raw_query_mode = (
            query_mode
            if query_mode is not None
            else os.getenv("TP_TRACE_QUERY_MODE")
            or os.getenv("TP_DOG_QUERY_MODE")
        )
        resolved_query_mode = _parse_query_mode(raw_query_mode)

        return cls(
            project_name=resolved_project,
            cluster_name=resolved_cluster,
            environment=resolved_env,
            version=resolved_version,
            endpoint=resolved_endpoint,
            traces_endpoint=resolved_traces_endpoint,
            headers=merged_headers,
            sample_rate=resolved_sample_rate,
            disabled=resolved_disabled,
            debug=resolved_debug,
            resource_attributes=res_attrs,
            integrations=integs,
            trace_nested_templates=resolved_trace_nested,
            template_enabled=resolved_template_enabled,
            cache_enabled=resolved_cache_enabled,
            template_exclude=resolved_template_exclude,
            tags=resolved_tags,
            on_request_span=resolved_on_request_span,
            ignore_endpoints=resolved_ignores,
            endpoint_sample_rules=resolved_rules,
            trusted_proxies=resolved_trusted_proxies,
            db_role_map=resolved_db_role_map,
            auto_patch=resolved_auto_patch,
            extract_trace_context=resolved_extract,
            metrics_endpoint=resolved_metrics_endpoint,
            metrics_enabled=resolved_metrics_enabled,
            metrics_export_interval_millis=resolved_metrics_interval,
            redis_mask_endpoint=resolved_redis_mask_endpoint,
            redis_mask_command=resolved_redis_mask_command,
            redis_cmd_max_length=resolved_cmd_max_length,
            query_mode=resolved_query_mode,
        )
