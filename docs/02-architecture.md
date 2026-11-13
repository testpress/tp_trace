# tp_trace Architecture

Read this when working on the SDK or telemetry pipeline. [Observability Basics](01-basics.md)
covers the terms used here.

`tp_trace` is a production Python APM library built on OpenTelemetry. It instruments your application to generate traces and metrics, routes them through an OpenTelemetry Collector, stores them in Tempo and Prometheus, and visualizes them in Grafana.

---

## Architecture Flow

![Django and Celery send spans and native metrics to the Collector, which routes traces to Tempo and metrics to Prometheus for Grafana.](images/tp-trace-architecture.png)

---

## End-to-End Lifecycle: From Request to Grafana

### 1. Spans Created (Application Layer)
When your application receives a request or executes a task:
- **Request / Task Entry**:
  - **HTTP Requests**: Django middleware intercepts incoming requests and starts a `SERVER` span (e.g. `GET /api/orders/`). It joins the caller's trace when incoming trace context is valid and extraction is allowed. Otherwise it starts a new trace.
  - **Celery Tasks**: Publishing a task creates a `PRODUCER` span and injects trace context into headers. Worker execution creates a `CONSUMER` span that continues that trace (or starts a new one if published without context).
- **Child Spans**: Downstream operations during execution automatically create child spans:
  - **PostgreSQL**: Queries and execution times (SQL parameters are sanitized).
  - **Redis**: Commands and cache lookups.
  - **HTTP (`requests`)**: Outbound API calls, injecting trace context downstream.
- **Span Completion**: Each span finishes with its duration, status code, and normalized route metadata.

### 2. SDK Processing & Export
By default, a background worker batches finished spans and sends them to the
Collector over OTLP/HTTP. The SDK can also export spans synchronously with
`export_batch=False`. Native metrics are recorded independently of trace
sampling and exported periodically over OTLP/HTTP when enabled.

The SDK can send record-only spans to the Collector. This lets the Collector
derive metrics from eligible spans even when those spans are filtered out of
the main trace export.

### 3. OpenTelemetry Collector (Buffering & Routing)

The Collector applies memory limits and removes ephemeral resource attributes.
It handles spans and metrics through separate pipelines.

- The trace pipeline samples received traces and exports retained spans to Tempo.
- The separate span-to-metrics pipeline derives RED metrics from all eligible
  spans, independently of trace retention, and exports them to Prometheus.

#### Tail Sampling

The Collector buffers spans for 3 seconds before making a trace-level decision.
Policies are combined with OR: retain SDK-sampled traces (`tp_trace.sampled == true`),
50% of error traces, or 1% of repeated-query evidence traces. SDK-sampled error
traces are therefore retained in full, increasing Tempo error evidence compared
with the previous per-span filters.

All deployed producers use tp_trace 0.4.1, which supplies `tp_trace.sampled`.
Wire sampled flags alone do not retain a trace.

All spans of a trace must reach the same Collector for a complete decision.
Ansible deploys one Collector per host, so cross-host traces can be evaluated as
separate fragments. A shared sampling tier would require trace-ID-aware routing.

The buffer holds up to 5,000 traces; overflow evicts the oldest pending traces.
This is a trace-count limit, not a memory bound. The memory limiter defaults to
400 MiB with an 80 MiB spike allowance; large traces can still cause pressure.
`expected_new_traces_per_sec: 1000` is a preallocation hint, not a throughput limit.

Decision caches hold 10,000 kept and 25,000 dropped trace IDs. Late spans inherit
cached decisions without re-evaluation. Errors arriving after a drop decision
can be lost; after cache eviction, late spans may be evaluated separately and
produce partial traces. Cache entries have no guaranteed retention duration.
Tune the decision window and caches for longer traces, accounting for memory use.

### 4. Storage (Tempo & Prometheus)
- **Grafana Tempo (Traces)**: Ingests raw spans, indexes them by `trace_id`, and stores immutable trace blocks.
- **Prometheus (Metrics)**: Ingests and indexes aggregate RED time series (Rate, Errors, Duration) for service operations and HTTP routes.

### 5. Grafana Loads Dashboards
When an engineer opens Grafana:
1. **RED Dashboards**: Grafana queries **Prometheus** to display:
   - **Rate**: Requests and database queries per second.
   - **Errors**: Error count and error percentage over time.
   - **Duration**: p50, p95, and p99 response times.
2. **Trace Drill-down**: Select an operation to find related traces, then open a
   trace ID to view the spans available in Tempo.

See the [Grafana dashboard guide](../grafana/dashboards/README.md) for investigation steps.

---

## Key Guarantees

- **Application Safety**: Telemetry errors are caught internally and never crash user requests, alter return values, or rerun code.
- **Bounded Resources**: In-memory queues and batch limits prevent unbounded memory growth.
- **Data Privacy**: SQL query literals are sanitized by default. URL query parameters default to `expose`, but can be masked by setting `TP_TRACE_QUERY_MODE=redact` (replaces sensitive parameter values with `REDACTED`) or `TP_TRACE_QUERY_MODE=strip` (removes the entire query string).

## Where to start in the code

- Follow initialization and exporter setup in [the SDK entry point](../src/tp_trace/__init__.py).
- Follow request instrumentation in [the Django integration](../src/tp_trace/integrations/django/).
- Follow head sampling decisions in [the sampler](../src/tp_trace/sampler.py)
  and native metric recording in [the metrics module](../src/tp_trace/metrics.py).
- Follow signal routing in [the Collector configuration](../config/otel-collector.yaml)
  and the queries in [the dashboard definitions](../grafana/dashboards/).

Read the [code conventions](code-conventions.md) before changing instrumentation
or telemetry contracts.
