# Configuration reference

`tp_trace.init()` accepts explicit keyword arguments. Explicit arguments take precedence over environment variables. Configuration is read from `TP_TRACE_*` environment variables, with `OTEL_*` aliases supported where noted.

| Environment variable | `init()` option | Default | Description |
|---|---|---:|---|
| `TP_TRACE_PROJECT_NAME` | `project_name` | `unknown-project` | Application identity; emitted as `service.name`. |
| `TP_TRACE_CLUSTER_NAME` | `cluster_name` | `unknown-cluster` | Cluster label. |
| `TP_TRACE_ENVIRONMENT` | `environment` | `development` | Deployment environment. |
| `TP_TRACE_VERSION` | `version` | SDK version | Application version. |
| `TP_TRACE_ENDPOINT` | `endpoint` | `http://localhost:4318` | OTLP/HTTP base endpoint. `/v1/traces` is added when absent. |
| `TP_TRACE_TRACES_ENDPOINT` | `traces_endpoint` | unset | Full OTLP traces endpoint. |
| `TP_TRACE_HEADERS` | `headers` | empty | Comma-separated `key=value` exporter headers. |
| `TP_TRACE_SAMPLE_RATE` | `sample_rate` | `1.0` | Head-sampling ratio from `0.0` to `1.0`. |
| `TP_TRACE_DISABLED` | `disabled` | `false` | Disables SDK tracing. |
| `TP_TRACE_DEBUG` | `debug` | `false` | Enables SDK debug logging. |
| `TP_TRACE_IGNORE_ENDPOINTS` | `ignore_endpoints` | empty | Comma-separated route patterns to exclude. |
| `TP_TRACE_ENDPOINT_SAMPLE_RULES` | `endpoint_sample_rules` | empty | Comma-separated `pattern=rate` rules. |
| `TP_TRACE_TAGS` | `tags` | empty | Comma-separated `key=value` tags. |
| `TP_TRACE_DB_ROLE_MAP` | `db_role_map` | empty | Comma-separated `alias=primary` or `alias=replica` mappings. |
| `TP_TRACE_AUTO_PATCH` | `auto_patch` | `true` | Enables automatic integration patching. |
| `TP_TRACE_REDIS_MASK_ENDPOINT` | `redis_mask_endpoint` | `false` | Replaces Redis host and port with `?`. |
| `TP_TRACE_REDIS_MASK_COMMAND` | `redis_mask_command` | `false` | Replaces Redis command arguments with `?`. |
| `TP_TRACE_REDIS_CMD_MAX_LENGTH` | `redis_cmd_max_length` | `1000` | Maximum Redis statement length. |
| `TP_TRACE_QUERY_MODE` | `query_mode` | `expose` | URL query handling: `expose`, `redact`, or `strip`. |
| `TP_TRACE_N_PLUS_ONE_DUPLICATE_THRESHOLD` | `n_plus_one_duplicate_threshold` | `15` | Successful executions of one normalized SQL shape needed for a repeated-query candidate; minimum `2`. |
| `TP_TRACE_N_PLUS_ONE_MAX_FINGERPRINTS` | `n_plus_one_max_fingerprints` | `256` | Maximum distinct SQL shapes tracked per Django request; minimum `1`. |
| `TP_TRACE_SERVICE_INSTANCE_ID` | `service_instance_id` | hostname | Prefix for native metric `worker_id` attribute (`<prefix>:<pid>`). Overrides the prefix for multi-process worker metric streams. Does not modify the OpenTelemetry Resource's `service.instance.id`. |

`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`, `OTEL_SERVICE_VERSION`, and `OTEL_SERVICE_INSTANCE_ID` are also recognised where applicable.

Repeated-query detection runs by default when Django request and database instrumentation are active. Invalid N+1 values fall back to the defaults. `TP_DOG_N_PLUS_ONE_DUPLICATE_THRESHOLD` and `TP_DOG_N_PLUS_ONE_MAX_FINGERPRINTS` are supported legacy aliases.
