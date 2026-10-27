# How telemetry moves through the stack

`tp_trace` observes work inside application processes. It creates spans and native metrics as a Django request passes through application code and supported dependencies, and as background jobs are published and executed across asynchronous process boundaries such as Celery workers.

The stack uses a hybrid telemetry architecture that keeps user-facing request metrics independent of trace sampling while limiting runtime overhead:

| Signal | Source | Pipeline & Storage | Sampling Behavior |
|---|---|---|---|
| **API request throughput, errors, latency** | Django native metrics (`tp_trace.metrics`) | OTLP metrics -> Collector -> Prometheus | Decoupled from trace sampling (independent of head sampling) |
| **PostgreSQL, Redis, outbound HTTP, AWS, Celery metrics** | OpenTelemetry Collector `spanmetrics` connector | OTLP spans -> Collector `spanmetrics` -> Prometheus | Derived from exported spans (low Python overhead) |
| **Request, dependency, and internal waterfall traces** | `tp_trace` distributed tracing | OTLP traces -> Collector -> Tempo | Subject to SDK trace sampling (`sample_rate`) |

```text
Django web app / Celery worker
  ├─ tp_trace metrics (OTLP) ────────┐
  │  (calls_total, duration)         ▼
  │                           OpenTelemetry Collector
  │                             ├─ Prometheus metrics (Native + spanmetrics)
  │                             └─ Tempo traces (sampled waterfall)
  └─ tp_trace spans (OTLP) ──────────┘
     (Django, Postgres, Redis, Celery)
```

### Multi-worker metric aggregation
In multi-worker WSGI environments (such as Gunicorn or uWSGI), each worker process maintains its own independent in-memory meter and counter streams. To prevent counter stream collisions when multiple workers push cumulative counters to Prometheus, `worker_id` (`<host>:<pid>` or `<OTEL_SERVICE_INSTANCE_ID>:<pid>`) is attached as a bounded label on native metric datapoints, while `service.instance.id` is maintained on the OpenTelemetry Resource. Queries in Grafana aggregate across worker streams using `sum by (...) (rate(calls_total[5m]))` without grouping by `worker_id`.

Django HTTP `SPAN_KIND_SERVER` spans are excluded from the Collector's `spanmetrics` connector pipeline (`filter/spanmetrics_drop_non_dependencies`), ensuring that request metrics are emitted exclusively through the native SDK metrics pipeline without duplicate connector metrics. In addition, `SPAN_KIND_INTERNAL` spans are blocked from `spanmetrics` to ensure framework implementation details never generate RED metrics.

### Process boundary and single metrics pipeline
Asynchronous task executions across Celery workers cross process and machine boundaries. tp_trace injects W3C tracecontext headers into the task payload upon publication (`celery.apply_async`) and extracts them in the consumer worker (`celery.run`), connecting producer and consumer spans within a unified distributed trace.

All RED metrics (rate, error, duration) for Celery tasks use the downstream OpenTelemetry Collector pipeline as PostgreSQL and Redis spans. Workers do not establish independent metric instruments or parallel counter registries, avoiding thread and fork hazards in ephemeral worker processes.

This separation keeps application instrumentation small while centralising telemetry routing, storage, and access control.
