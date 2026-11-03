# tp_trace Architecture

`tp_trace` is a production Python APM library built on OpenTelemetry. It instruments your application to generate traces and metrics, routes them through an OpenTelemetry Collector, stores them in Tempo and Prometheus, and visualizes them in Grafana.

---

## Architecture Flow

```text
[ Django / Celery App ]
    │  (1. Request starts: Root server span created)
    │  (2. DB, Redis, HTTP calls: Child spans created)
    ▼
[ tp_trace SDK ]
    │  (3. Spans buffered and batched asynchronously)
    ▼  OTLP / HTTP (:4318)
[ OpenTelemetry Collector ]
    │
    ├─────────────────────────────┐
    │ Traces (OTLP gRPC :4327)    │ Metrics (Remote Write :9090)
    ▼                             ▼
[ Grafana Tempo ]             [ Prometheus ]
  Stores trace waterfalls       Stores RED time-series
    │                             │
    └──────────────┬──────────────┘
                   ▼
          [ Grafana (:3001) ]
           - RED Service Dashboards
           - Trace Waterfall Views
```

---

## End-to-End Lifecycle: From Request to Grafana

### 1. Spans Created (Application Layer)
When your application receives a request or executes a task:
- **Request / Task Entry**:
  - **HTTP Requests**: Django middleware intercepts incoming requests and starts a `SERVER` span (e.g. `GET /api/orders/`). If incoming trace headers (`traceparent`) exist, it joins the caller's trace; otherwise, it starts a new trace.
  - **Celery Tasks**: Publishing a task creates a `PRODUCER` span and injects trace context into headers. Worker execution creates a `CONSUMER` span that continues that trace (or starts a new one if published without context).
- **Child Spans**: Downstream operations during execution automatically create child spans:
  - **PostgreSQL**: Queries and execution times (SQL parameters are sanitized).
  - **Redis**: Commands and cache lookups.
  - **HTTP (`requests`)**: Outbound API calls, injecting trace context downstream.
- **Span Completion**: Each span finishes with its duration, status code, and normalized route metadata.

### 2. SDK Processing & Export
- Finished spans are placed into an in-memory buffer.
- A background worker batches spans and sends them asynchronously to the OpenTelemetry Collector via OTLP/HTTP (`POST http://<collector>:4318/v1/traces`).
- By default, export runs asynchronously in a background batch worker to minimize runtime impact. (Synchronous export is also supported for test environments or batch jobs). While instrumentation adds minimal CPU overhead, telemetry processing does not block request handling under default asynchronous export.

### 3. OpenTelemetry Collector (Buffering & Routing)
The local Collector acts as a fleet safety buffer and router:
- **Safety**: Applies memory limits to protect host resources and drops ephemeral container/process IDs to keep metrics clean.
- **Routing**:
  - Forwards traces to **Tempo** via OTLP/gRPC.
  - Forwards metrics to **Prometheus** via Prometheus Remote Write.

### 4. Storage (Tempo & Prometheus)
- **Grafana Tempo (Traces)**: Ingests raw spans, indexes them by `trace_id`, and stores immutable trace blocks.
- **Prometheus (Metrics)**: Ingests and indexes aggregate RED time series (Rate, Errors, Duration) for service operations and HTTP routes.

### 5. Grafana Loads Dashboards
When an engineer opens Grafana:
1. **RED Dashboards**: Grafana queries **Prometheus** to display:
   - **Rate**: Requests and database queries per second.
   - **Errors**: Error count and error percentage over time.
   - **Duration**: p50, p95, and p99 response times.
2. **Trace Drill-down**: Clicking a specific trace ID or latency exemplar queries **Tempo**:
   - Grafana renders the full **Trace Waterfall**, showing exact parent-child timings across Django, PostgreSQL, Redis, and Celery so bottlenecks are instantly visible.

---

## Key Guarantees

- **Application Safety**: Telemetry errors are caught internally and never crash user requests, alter return values, or rerun code.
- **Bounded Resources**: In-memory queues and batch limits prevent unbounded memory growth.
- **Data Privacy**: SQL query literals are sanitized by default. URL query parameters default to `expose`, but can be masked by setting `TP_TRACE_QUERY_MODE=redact` (replaces values with `?`) or `TP_TRACE_QUERY_MODE=strip` (removes the entire query string).
