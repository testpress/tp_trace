# Configuration Reference

This reference describes `tp_trace.init()` configuration as resolved by
[`SDKConfig.from_env_and_kwargs()`](../src/tp_trace/config.py). Defaults below
refer to resolved initialization, which can differ from constructing `SDKConfig()`
directly. Repeated `init()` calls return the existing provider and do not reload settings.

## Identity and Transport

| Argument | Environment sources, in priority order | Default |
|---|---|---|
| `project_name`, then `project` | `TP_TRACE_PROJECT_NAME`, `TP_TRACE_PROJECT`, `TP_TRACE_SERVICE_NAME`, `OTEL_SERVICE_NAME`, `OTEL_PROJECT_NAME` | Django settings detection, then `unknown-project` |
| `cluster_name` | `TP_TRACE_CLUSTER_NAME`, `TP_TRACE_CLUSTER`, `OTEL_CLUSTER_NAME` | Django settings detection, then `unknown-cluster` |
| `environment` | `TP_TRACE_ENV`, `TP_TRACE_ENVIRONMENT`, `OTEL_ENVIRONMENT`, `DEPLOYMENT_ENVIRONMENT`, `ENVIRONMENT`, `ENV` | Django settings detection, then `development` |
| `version` | `TP_TRACE_VERSION`, `OTEL_SERVICE_VERSION`, `SERVICE_VERSION` | SDK version |
| `endpoint` | `TP_TRACE_ENDPOINT`, `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` |
| `traces_endpoint` | `TP_TRACE_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Base endpoint plus `/v1/traces` |
| `metrics_endpoint` | `TP_TRACE_METRICS_ENDPOINT`, `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | Base endpoint plus `/v1/metrics` |
| `headers` | `TP_TRACE_HEADERS`, `OTEL_EXPORTER_OTLP_HEADERS` | Empty mapping |
| `tags` | `TP_TRACE_TAGS`, `OTEL_RESOURCE_ATTRIBUTES` | Empty mapping |
| `resource_attributes` | No direct environment source | Empty mapping |

Explicit identity and endpoint arguments precede environment values. Legacy
`service_name=` and `service=` arguments follow `project` and precede environment
sources; they refer to project identity and generate deprecation warnings.

Django project detection checks `TP_TRACE_PROJECT_NAME`, `OTEL_PROJECT_NAME`, then
`PROJECT_NAME`. Environment detection checks `TP_TRACE_ENVIRONMENT`, `ENVIRONMENT`,
then `ENV`. Cluster detection checks `TP_TRACE_CLUSTER_NAME`, `TP_TRACE_CLUSTER`,
`OTEL_CLUSTER_NAME`, then `CLUSTER_NAME` in Django settings.

Headers use comma-separated `key=value` pairs in the environment. Explicit headers
merge over environment headers. Explicit tags replace environment tags; otherwise
the first available tag environment source is parsed as comma-separated pairs.
Tags are attached to resources and spans. `resource_attributes` are applied after
the generated resource identity and tags, so they can override those attributes.
Unknown extra initialization keywords can become static tags; misspelled settings
therefore do not necessarily fail validation.

Signal-specific endpoints are complete URLs. The base endpoint is normally the
Collector HTTP base URL; the SDK appends the signal path unless the matching
suffix is already present. Do not use a trace-specific URL as the shared base
for metrics. The SDK exporters use OTLP/HTTP, not OTLP/gRPC.

## Sampling and Lifecycle

| Argument | Environment sources | Default |
|---|---|---|
| `sample_rate` | `TP_TRACE_SAMPLE_RATE`, `OTEL_TRACES_SAMPLER_ARG`, `SAMPLE_RATE` | `1.0` |
| `ignore_endpoints` | `TP_TRACE_IGNORE_ENDPOINTS` | Empty list |
| `endpoint_sample_rules` | `TP_TRACE_ENDPOINT_SAMPLE_RULES` | Empty mapping |
| `disabled` | `TP_TRACE_DISABLED`, `OTEL_SDK_DISABLED` | `False` |
| `debug` | `TP_TRACE_DEBUG`; otherwise `OTEL_LOG_LEVEL=debug` | `False` |
| `auto_patch` | `TP_TRACE_AUTO_PATCH` when no explicit value is supplied | `True` |
| `integrations` | No direct environment source | Auto-detect installed built-ins |
| `extract_trace_context` | `TP_TRACE_EXTRACT_TRACE_CONTEXT`; otherwise `OTEL_PROPAGATORS=none`, `off`, or `0` disables HTTP extraction | `True` |

`init()` supplies `auto_patch=True` by default, so that explicit argument takes
precedence over the environment switch during normal initialization. Use
`auto_patch=False` when controlling patching through this API.

Sampling rates must be finite numbers from 0 through 1. Ignore patterns and
endpoint rules use shell-style wildcard matching against available path/name
candidates. Ignore matches precede endpoint rules. Rules use a mapping in Python
or comma-separated `pattern=rate` entries in the environment. Upstream parent
sampling affects the result; endpoint rates do not universally override parents.
An explicit empty list or mapping clears environment ignores or rules.

The HTTP extraction option also accepts a callable in Python. It does not disable
Celery message extraction. `on_request_span` accepts a Python callback for request
span enrichment. `exporter`, `span_processor`, `export_batch`, and `metric_readers`
are initialization-time objects/options, not environment configuration. The
default trace exporter is batched; `export_batch=False` uses synchronous export.

## Metrics

| Argument | Environment sources | Default |
|---|---|---|
| `metrics_enabled` | `TP_TRACE_METRICS_ENABLED` | `True` |
| `metrics_export_interval_millis` | `TP_TRACE_METRICS_EXPORT_INTERVAL_MILLIS`, `OTEL_METRIC_EXPORT_INTERVAL` | `60000` |
| `metric_cardinality_limit` | `TP_TRACE_METRIC_CARDINALITY_LIMIT` | `5000`, clamped to at least 1 |
| `service_instance_id` | `TP_TRACE_SERVICE_INSTANCE_ID`, `OTEL_SERVICE_INSTANCE_ID` | Hostname used as worker identity prefix |

`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` is honored by the owned OTLP
metrics exporter. Without it, owned counters and histograms use delta export.
Worker identity combines the resolved prefix and PID. The Collector must preserve
that datapoint identity through delta-to-cumulative conversion.

Disabling metrics suppresses automatic periodic export; it is not a general
promise that all instrumentation and in-process aggregation disappear. Supplying
custom metric readers changes ownership and export behavior. Export intervals
also determine how soon dashboard data can appear.

## Django and Data Handling

| Argument | Environment sources | Default |
|---|---|---|
| `trace_nested_templates` | `TP_TRACE_DJANGO_TRACE_NESTED_TEMPLATES`, `OTEL_PYTHON_DJANGO_TRACE_NESTED_TEMPLATES` | `True` |
| `template_enabled` | No environment source in the current resolver | `True` |
| `template_exclude` | `TP_TRACE_TEMPLATE_EXCLUDE` | `django/forms/*`, `debug_toolbar/*`, `*/widgets/*` |
| `cache_enabled` | `TP_TRACE_CACHE_ENABLED` | `True` |
| `trusted_proxies` | `TP_TRACE_TRUSTED_PROXIES` | Empty list |
| `db_role_map` | `TP_TRACE_DB_ROLE_MAP` | Empty mapping |
| `redis_mask_endpoint` | `TP_TRACE_REDIS_MASK_ENDPOINT` | `False` |
| `redis_mask_command` | `TP_TRACE_REDIS_MASK_COMMAND` | `False` |
| `redis_cmd_max_length` | `TP_TRACE_REDIS_CMD_MAX_LENGTH` | `1000` |
| `query_mode` | `TP_TRACE_QUERY_MODE` | `expose` |
| `n_plus_one_duplicate_threshold` | `TP_TRACE_N_PLUS_ONE_DUPLICATE_THRESHOLD`, `TP_DOG_N_PLUS_ONE_DUPLICATE_THRESHOLD` | `15`, minimum 2 |
| `n_plus_one_max_fingerprints` | `TP_TRACE_N_PLUS_ONE_MAX_FINGERPRINTS`, `TP_DOG_N_PLUS_ONE_MAX_FINGERPRINTS` | `256`, minimum 1 |

`template_instrumentation` also accepts an `enabled`/`exclude` mapping; see the
resolver for its precedence over the individual template switches. List
values in the environment use comma-separated entries. Database role mapping
uses comma-separated `alias=role` pairs. Only configure trusted proxy networks
that actually terminate trusted traffic.

`query_mode` accepts `expose`, `redact`, or `strip`. Redaction masks recognized
credential keys; it does not guarantee removal of every sensitive query value.
Redis masking is opt-in. SQL sanitization is separate from these switches.
Repeated-query thresholds bound request-level detection; findings are advisory.

## Validation Behavior

Environment booleans accept `1/0`, `true/false`, `yes/no`, `on/off`, `t/f`, and
`y/n`, ignoring case. Pass actual booleans to Python arguments rather than strings:
some explicit arguments use Python truth conversion.

Invalid values do not all behave alike. Several numeric parsers fall back to
field defaults; malformed rates, query modes, and some booleans raise during
resolution. `init()` catches configuration-resolution errors and falls back to
a default `SDKConfig`, rather than retaining every other supplied setting.
Calling the resolver directly can raise. Check logs and resolved configuration
when validating a deployment.
