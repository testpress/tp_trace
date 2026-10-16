# Supported integrations reference

| Integration key | Detected library | Primary spans |
|---|---|---|
| `django` | Django 4.2+ | `django.request`, middleware, views, templates, cache, authentication |
| `postgres` | Django database connections and `psycopg2` | PostgreSQL queries and PgBouncer-aware queries |
| `redis` | `redis` / `django_redis` | Redis commands, pipelines, Django cache operations |
| `requests` | `requests` | Outbound HTTP requests with W3C context propagation |
| `boto` | `boto3` / Botocore | AWS API operations, including S3 |

All installed integrations are enabled by default. The `integrations` mapping controls their enabled state:

```python
tp_trace.init(integrations={"redis": False, "boto": False})
```

## Data handling

PostgreSQL statements are sanitised before export. Redis endpoint and command details are exposed by default; use the Redis masking options in the [configuration reference](configuration.md) when that data must not leave the application environment. URL userinfo is always removed. `TP_TRACE_QUERY_MODE` controls URL query-string handling.
