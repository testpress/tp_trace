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

Configuration is resolved once during `SDKConfig` setup (`service_instance_id` option, `TP_TRACE_SERVICE_INSTANCE_ID`, or `OTEL_SERVICE_INSTANCE_ID`, defaulting to hostname); metric recording uses this pre-resolved prefix without reading environment variables. The identity cache is indexed by PID and resolved prefix (and per recorder), ensuring that distinct prefixes in the same process do not clash. Cache entries are stored lock-free, and forked children refresh without inheriting an initialization mutex. This setting changes the `worker_id` prefix on native metric datapoints (`<prefix>:<pid>`); it does not change the OpenTelemetry Resource's `service.instance.id`.

The PID namespace bounds the possible identities for a fixed host prefix, but does not eliminate series churn. Recycled workers normally receive new PIDs, and changing container hostnames also creates new series. For 25 routes, one method, two status combinations, one tenant, and 12 workers, the two native RED instruments produce approximately 7,200 active series: 600 counters and 600 histograms with nine buckets (eight explicit boundaries plus `+Inf`), `_sum`, and `_count`. Retained series from replaced workers add to storage cost until retention expires. This estimate excludes other instruments and additional attribute combinations.

Django HTTP `SPAN_KIND_SERVER` spans are excluded from the Collector's `spanmetrics` connector pipeline (`filter/spanmetrics_drop_non_dependencies`), ensuring that request metrics are emitted exclusively through the native SDK metrics pipeline without duplicate connector metrics. In addition, `SPAN_KIND_INTERNAL` spans are blocked from `spanmetrics` to ensure framework implementation details never generate RED metrics.

### Interpreting native and sampled metrics

Native Django RED metrics describe all observed requests. Dependency spanmetrics describe only exported spans, so their traffic and accumulated duration depend on trace sampling and per-route sampling rules. Dividing sampled dependency duration by unsampled request duration understates dependency shares; subtracting it from request duration overstates application compute time. The catalog therefore shows sampled dependency P95 latency instead of a request-time breakdown. Latency warnings compare each source's P95 against its own recent median and an absolute latency threshold, without assigning a root cause from duration shares.

Where dashboards retain historical Django spanmetrics as a fallback, they aggregate each source to matching output labels before choosing native data. Native request volume also supplies a zero error numerator when no native error series exists, so historical errors cannot replace an error-free native observation.

The Locust comparison tool is an approximate reconciliation aid. It uses reset-aware per-stream increases at a fixed evaluation timestamp and waits for ingestion according to the supplied export interval. Export boundaries, new series, worker exits before export, other traffic, and differences between client and server observations still prevent an exact-count assertion. Its ordinary observation mode keeps Locust run totals distinct from Prometheus window estimates. Exact native recording under disabled trace sampling is covered separately with in-memory metric readers.

### Process boundary and single metrics pipeline
Asynchronous task executions across Celery workers cross process and machine boundaries. tp_trace injects W3C tracecontext headers into the task payload upon publication (`celery.apply_async`) and extracts them in the consumer worker (`celery.run`), connecting producer and consumer spans within a unified distributed trace.

All RED metrics (rate, error, duration) for Celery tasks use the downstream OpenTelemetry Collector pipeline as PostgreSQL and Redis spans. Workers do not establish independent metric instruments or parallel counter registries, avoiding thread and fork hazards in ephemeral worker processes.

This separation keeps application instrumentation small while centralising telemetry routing, storage, and access control.
