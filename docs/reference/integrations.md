# Supported integrations reference

| Integration key | Detected library | Primary spans |
|---|---|---|
| `django` | Django 4.2+ | `django.request`, middleware, views, templates, cache, authentication |
| `postgres` | Django database connections and `psycopg2` | PostgreSQL queries and PgBouncer-aware queries |
| `redis` | `redis` / `django_redis` | Redis commands, pipelines, Django cache operations |
| `requests` | `requests` | Outbound HTTP requests with W3C context propagation |
| `boto` | `boto3` / Botocore | AWS API operations, including S3 |
| `celery` | `celery` 5.0+ / `opentelemetry-instrumentation-celery` 0.54b1+ | `celery.apply_async` (producer), `celery.run` (consumer) |

Installed integrations, including Celery, are enabled by default when auto-patching is enabled. The `integrations` mapping controls their enabled state:

```python
tp_trace.init(integrations={"celery": False, "redis": False, "boto": False})
```

## Celery integration details

### Activation and configuration
Celery is automatically instrumented by `tp_trace.init()` when Celery and its OTel instrumentation are installed and auto-patching is enabled. Disable it with `tp_trace.init(integrations={"celery": False})`, or disable all auto-patching with `auto_patch=False`.

Enabling Celery adds producer and consumer spans (normally two spans per task before sampling) and injects W3C trace context into published message headers. Celery RED metrics are derived from those exported spans; no metric names, labels, or histogram buckets change.

Tested versions:
- Floor: Python 3.8 with Celery 5.5.3, `opentelemetry-instrumentation-celery` 0.54b1, and OTel SDK 1.33.1.
- Development: Python 3.11 with Celery 5.6.3, `opentelemetry-instrumentation-celery` 0.65b0, and OTel SDK 1.44.0.

### Span attributes and topology
- **Producer**: Span name `🟣 <task_name>.apply_async` (fallback `🟣 celery.apply_async`), kind `PRODUCER`, `normalized.service="celery"`, `normalized.operation="celery.apply_async.<admitted_name or unknown>"`.
- **Consumer**: Span name `🟣 celery.run`, kind `CONSUMER`, `normalized.service="celery"`, `normalized.operation="celery.run.<admitted_name or unknown>"`.
- Attributes on spans include `celery.task_name`, `celery.task_id`, `celery.queue`, `celery.worker`, `celery.origin`, `celery.task_state`, and boolean `error`.
- HTTP route attributes (`http.route`, `http.method`, `django.route`) are suppressed on all Celery spans to prevent accidental metric collision.
- W3C traceparent headers are injected during publish and extracted upon execution, linking the web request, producer span, and consumer span in a single trace with direct parent relationships.

### Cardinality bounds
To protect metric storage from high-cardinality task operations, tp_trace observes already-finalized Celery registries at activation and other registries after Celery finishes their normal finalization. Instrumentation does not finalize applications, load task modules, or create a default Celery app. At most 256 names (sorted within each observed registry, length <= 200) are admitted across the entire activation lifecycle; later registries and locally registered task objects encountered during publication or execution fill remaining slots without replacing admitted names. Arbitrary unregistered sender strings are never admitted. Unknown or excess task names use the operation label `unknown` (`celery.apply_async.unknown` and `celery.run.unknown`). The full original name is preserved on the span in `celery.task_name`.

### Metrics and RED pipeline
Celery metrics are derived exclusively downstream by the OpenTelemetry Collector's spanmetrics connector from exported spans. Upstream Celery Flower histograms (`flower.task.runtime.seconds`) are suppressed to avoid unnecessary process overhead. RED metrics are subject to sampling and reflect execution attempts rather than deduplicated business tasks.

### Sampling and Coexistence
- Sampling rules evaluate upstream span names before normalization (`apply_async/<task>` and `run/<task>`).
- If upstream `CeleryInstrumentor().instrument()` is called before `tp_trace.init()`, `CeleryIntegration` safely skips without overwriting or interfering with the existing instrumentation. Calling upstream instrumentation *after* `tp_trace.init()` is unsupported.

## Data handling

PostgreSQL statements are sanitised before export. Redis endpoint and command details are exposed by default; use the Redis masking options in the [configuration reference](configuration.md) when that data must not leave the application environment. URL userinfo is always removed. `TP_TRACE_QUERY_MODE` controls URL query-string handling.
