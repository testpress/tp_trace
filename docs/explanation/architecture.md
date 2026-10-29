# How telemetry moves through the stack

`tp_trace` observes work inside application processes. It creates spans and native metrics as a Django request passes through application code and supported dependencies, and as background jobs are published and executed across asynchronous process boundaries such as Celery workers.

The stack uses a hybrid telemetry architecture that keeps user-facing request metrics independent of trace sampling while limiting runtime overhead:

| Signal | Source | Pipeline & Storage | Sampling Behavior |
|---|---|---|---|
| **API request throughput, errors, latency** | Django native metrics (`tp_trace.metrics`) | OTLP metrics -> Collector -> Prometheus | Decoupled from trace sampling (independent of head sampling) |
| **PostgreSQL, Redis, outbound HTTP, AWS, Celery metrics** | SDK in-process processor (`RedSpanProcessor`) | OTLP metrics -> Collector -> Prometheus | Decoupled from trace sampling (100% of finished spans recorded) |
| **Request, dependency, and internal waterfall traces** | `tp_trace` distributed tracing | OTLP traces -> Collector -> Tempo | Subject to SDK trace sampling (`sample_rate`) |

```text
Django web app / Celery worker
  ├─ tp_trace metrics (OTLP) ────────┐
  │  (calls_total, duration)         ▼
  │                           OpenTelemetry Collector
  │                             ├─ Prometheus metrics (Native RED metrics)
  │                             └─ Tempo traces (sampled waterfall)
  └─ tp_trace spans (OTLP) ──────────┘
     (Django, Postgres, Redis, Celery)
```

### Multi-worker metric aggregation
In multi-worker WSGI environments (such as Gunicorn or uWSGI), each worker process maintains its own independent in-memory meter and counter streams. To prevent counter stream collisions when multiple workers push cumulative counters to Prometheus, `worker_id` (`<host>:<pid>` or `<OTEL_SERVICE_INSTANCE_ID>:<pid>`) is attached as a bounded label on native metric datapoints, while `service.instance.id` is maintained on the OpenTelemetry Resource. Queries in Grafana aggregate across worker streams using `sum by (...) (rate(calls_total[5m]))` without grouping by `worker_id`.

Configuration is resolved once during `SDKConfig` setup (`service_instance_id` option, `TP_TRACE_SERVICE_INSTANCE_ID`, or `OTEL_SERVICE_INSTANCE_ID`, defaulting to hostname); metric recording uses this pre-resolved prefix without reading environment variables. The identity cache is indexed by PID and resolved prefix (and per recorder), ensuring that distinct prefixes in the same process do not clash. Cache entries are stored lock-free, and forked children refresh without inheriting an initialization mutex. This setting changes the `worker_id` prefix on native metric datapoints (`<prefix>:<pid>`); it does not change the OpenTelemetry Resource's `service.instance.id`.

The PID namespace bounds the possible identities for a fixed host prefix, but does not eliminate series churn. Recycled workers normally receive new PIDs, and changing container hostnames also creates new series. For 25 routes, one method, two status combinations, one tenant, and 12 workers, the two native RED instruments produce approximately 7,200 active series: 600 counters and 600 histograms with nine buckets (eight explicit boundaries plus `+Inf`), `_sum`, and `_count`. Retained series from replaced workers add to storage cost until retention expires. This estimate excludes other instruments and additional attribute combinations.

### Sampling-independent RED metrics
Both Django HTTP requests and dependency operations (PostgreSQL, Redis, Celery, outbound HTTP, AWS) are recorded natively by the SDK's `RedSpanProcessor`. Unsampled spans use `Decision.RECORD_ONLY` at span creation so that attributes, timings, and status are fully captured in memory before span completion. Upon `span.end()`, `RedSpanProcessor` records 100% of finished calls into Prometheus native counters (`calls_total`) and histograms (`duration_milliseconds`). Downstream network export is gated by `SampledOnlySpanProcessor`, ensuring that only sampled traces are transmitted to the Collector and Tempo.

Because dependency RED metrics are computed directly by the SDK on 100% of finished operations, they do not depend on trace sampling rates or downstream span derivation in the Collector. Historical `apm_*` series derived by the Collector's spanmetrics connector are deprecated in favor of unified `calls_total` and `duration_milliseconds` streams.

### Interpreting native metrics
Native RED metrics describe 100% of observed requests and dependencies. Because both root requests and child dependency operations are recorded without sampling bias, duration breakdowns and error rates accurately reflect real execution volume regardless of whether trace exports are heavily sampled or disabled. Where dashboards retain historical `apm_*` expressions as a migration fallback, they evaluate native `calls_total` as the primary source before falling back to legacy series.

Windowed total request counts in dashboards combine reset-aware `increase()` with `last_over_time()` guarded by `offset $__range` and zero-reset assertions. This ensures fresh workers or routes that produce a single sample or remain flat after early requests are accurately represented without returning empty or zero values. Because the native metrics exporter pipeline (`prometheusremotewrite`) preserves active streams without premature exporter-level expiry, and individual worker lifetimes are bound to distinct PIDs, new series start at zero upon process creation. For ephemeral spanmetrics (`apm_calls_total`), series expiration in the connector causes newly arriving spans after an idle gap to initialize fresh counters at zero. An existing series that had samples prior to the window and emits only a single sample inside the window will not register an increase; this boundary condition is confined to short querying ranges relative to scrape intervals.

The Locust comparison tool is an approximate reconciliation aid. It uses reset-aware per-stream increases at a fixed evaluation timestamp and waits for ingestion according to the supplied export interval. Export boundaries, new series, worker exits before export, other traffic, and differences between client and server observations still prevent an exact-count assertion. Its ordinary observation mode keeps Locust run totals distinct from Prometheus window estimates. Exact native recording under disabled trace sampling is covered separately with in-memory metric readers.

### Process boundary and single metrics pipeline
Asynchronous task executions across Celery workers cross process and machine boundaries. tp_trace injects W3C tracecontext headers into the task payload upon publication (`celery.apply_async`) and extracts them in the consumer worker (`celery.run`), connecting producer and consumer spans within a unified distributed trace.

All RED metrics (rate, error, duration) for Celery tasks are recorded natively by `RedSpanProcessor` across both task publication (`SPAN_KIND_PRODUCER`) and task execution (`SPAN_KIND_CONSUMER`) spans. Workers push metrics directly through the SDK's native metrics pipeline, avoiding thread and fork hazards.

This separation keeps application instrumentation small while centralising telemetry routing, storage, and access control.
