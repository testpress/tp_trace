# Integrations

## Activation

`tp_trace.init()` auto-detects installed integration libraries by default. Missing target libraries are skipped. The `integrations` mapping enables or disables canonical names; `auto_patch=False` defers patching until `tp_trace.patch_all()` is called. Repeated initialization retains the existing configuration.

```python
import tp_trace

tp_trace.init(
    project_name="checkout",
    cluster_name="backend",
    integrations={"django": True, "postgres": True, "redis": False},
)
```

## Supported Libraries

| Key | Target | Instrumented work and limits |
|---|---|---|
| `django` | Django | Incoming request, middleware, view, template, cache, and authentication work. Current declared minimum: Django 4.2. |
| `postgres` | Django database seams and raw psycopg2 | Query spans and database metadata. Django's database seams can cover its configured drivers; raw psycopg3 outside Django is not instrumented by this integration. |
| `redis` | Redis Python client | Commands and pipelines, with endpoint and command masking controls. |
| `requests` | `requests` | Outgoing HTTP spans and distributed propagation through the upstream instrumentor. This is not a general HTTP client integration. |
| `boto` | Botocore/Boto3 | AWS API client operations through botocore instrumentation. |
| `celery` | Celery | Producer and consumer task spans, propagated context, and bounded task-name handling. Enabled by default when its dependencies are installed. |

Aliases include `postgresql`, `psycopg2`, and `psycopg` for `postgres`; `http` and `urllib3` for `requests`; and `boto3`, `botocore`, `aws`, and `s3` for `boto`. Aliases select an integration; they do not extend its target-library coverage.

## Django

Initialize before request handling so startup hooks can apply instrumentation. Django request completion supplies native request metrics even when its trace is not exported. Template and cache instrumentation have independent switches. Default template exclusions include Django form templates, debug toolbar templates, and widget templates.

PostgreSQL tracing also observes Django database wrapper seams. Sanitized SQL and database metadata can appear on spans; potential repeated-query findings describe duplicate query patterns, not a proof of an application N+1 bug. Request-level fingerprint tracking is bounded. Metric findings do not contain SQL text.

## Celery

Initialize telemetry in the worker process as well as the producer application. Message context extraction remains active independently of the incoming HTTP extraction switch. Producer spans measure sending work and consumer spans measure task execution; counting both does not equal a count of unique tasks.

## Coexistence and Lifecycle

Several integrations use upstream OpenTelemetry instrumentors and check whether those are already active. This avoids some duplicate patching, but does not provide universal provider or hook composition. Treat an application's existing telemetry initialization as an ownership boundary and verify the actual span tree before enabling both systems.

The manager supports uninstrumentation for lifecycle management. Partial uninstrumentation failures can leave an integration active; runtime toggling should not be assumed to undo every patch successfully.

## Data Handling

Query text passes through SQL sanitization. Redis command and endpoint details are exposed by default. URL userinfo is removed, while query strings follow `query_mode`. Review [configuration](05-configuration.md) before sending telemetry outside the application environment. Keep user IDs, query text, and arbitrary request values out of metric dimensions.
