# Architectural Blueprint & Implementation Plan: 100% Sampling-Independent RED Metrics

**Status:** Ready for Review  
**Context:** Datadog Replacement for Testpress (`testpress_python`)  
**Scope:** `src/tp_trace/` (sampler, red processor, metrics, tracing), `dev/otel-collector/` and `ansible/` configs, dashboards, and automated verification suites.

---

## 1. Executive Summary: How Datadog Does It vs. How We Do It

### How Datadog Achieves 100% Golden Signals Under 0.01% Sampling
In Datadog APM, **trace sampling and metric generation are strictly decoupled**. The cardinal rule is: **Aggregate Before You Drop**.

Forensic inspection of [`../dd-trace-py`](file:///Users/bala/workspace/datadog-replacement/dd-trace-py) and [`datadog-agent`](file:///Users/bala/workspace/datadog-replacement/dd-trace-py/datadog-agent) reveals the exact mechanism:
1. **In the Host Process (`ddtrace`)**:
   * Spans are **always created and recorded** for 100% of operations (HTTP, DB queries, Redis, Celery).
   * Entrypoints and dependency spans are stamped with `_dd.top_level = 1` in [`ddtrace/_trace/processor/__init__.py:212`](file:///Users/bala/workspace/datadog-replacement/dd-trace-py/ddtrace/_trace/processor/__init__.py#L212).
   * The sampler assigns priority (`USER_KEEP` vs `USER_REJECT`), but does **not** discard spans in memory (unless explicitly configured with `discard=True`).
2. **In the Daemon (`datadog-agent/pkg/trace/agent/agent.go:592-597`)**:
   ```go
   // Step 1: 100% of spans are fed into the Stats Concentrator FIRST
   if !p.ClientComputedStats {
       statsInput.Traces = append(statsInput.Traces, *pt.Clone())
   }

   // Step 2: Sampling and dropping happens AFTER stats are aggregated
   keep, numEvents := a.sample(now, ts, pt)
   if !keep && len(pt.TraceChunk.Spans) == 0 {
       p.RemoveChunk(i) // Dropped from trace export, but RED stats are already captured!
       continue
   }
   ```
3. **In Serverless / Client-Side Mode (`DD_TRACE_STATS_COMPUTATION_ENABLED=True`)**:
   * When enabled, `NativeWriter` in `libdatadog` computes stats **directly inside the client process** before discarding unsampled spans, sending pre-aggregated 10-second stats buckets + sampled traces.

### Why `tp_trace` Lost Dependency RED Metrics
In OpenTelemetry and `tp_trace`:
* **Django Request Metrics (100% Unsampled)**: Handled natively in Python by [`request.py:780`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/integrations/django/request.py#L780) via `tp_trace.metrics`.
* **Dependency Metrics (Postgres, Redis, Requests, Celery)**: Were delegated to the Collector's `spanmetrics` connector.
* **The Glitch**: At `sample_rate = 0.01` (1%) or `0.0001` (0.01%), `tp_trace.sampler` evaluated `Decision.DROP` at the root span. In OpenTelemetry SDK, `Decision.DROP` creates no-op `NonRecordingSpan`s for all child spans. They were never recorded and never exported. As a result, the Collector's `spanmetrics` saw only 1 out of 100 (or 10,000) queries, causing dependency metrics to collapse to 1% of reality.

### Clarification on the Weight Formula (`weight = 1/p` vs `1.0`)
A previous draft suggested weighting spans by `1/p`. That was a misunderstanding of Datadog's `weight.go`:
* Datadog uses `weight = 1.0 / sample_rate` **only** when stats are reconstructed downstream from an already-sampled trace stream (the Horvitz-Thompson estimator).
* When stats are aggregated from **100% of spans in-process**, every span represents exactly 1 real execution. **The weight is strictly `1.0`**. (Multiplying by $1/p$ on 100% of spans would cause a disastrous 100x–10,000x overcounting inflation!).

---

## 2. Target Architecture: In-SDK Client-Side RED Engine

To match Datadog without flooding the local Collector with 100% HTTP trace serialization overhead, we adopt **Datadog's Client-Side Stats Architecture**:

```text
Host App Call (Postgres / Redis / HTTP / Celery / Django)
   │
   ▼
tp_trace Custom Sampler
   ├── ignore_endpoints ────────► Decision.DROP (No span, no RED)
   │
   ├── ratio keep (e.g. 1%) ────► Decision.RECORD_AND_SAMPLE (sampled=True)
   │                                   │
   └── ratio reject (e.g. 99%) ─► Decision.RECORD_ONLY (sampled=False)
                                       │
                                       ▼
                             Span.end() triggers processors
                                       │
            ┌──────────────────────────┴──────────────────────────┐
            ▼                                                     ▼
   RedSpanProcessor (weight=1.0)                        SampledOnlySpanProcessor Gate
   - Extracts RED for CLIENT/PRODUCER/CONSUMER spans    - Checks span.context.trace_flags.sampled
   - Slices by route, db.system, peer.service           - IF False: Discard from memory!
   - Increments native calls_total & duration           - IF True: Forward to BatchSpanProcessor
            │                                                     │
            ▼                                                     ▼
   OTLP Native Metrics (100% Traffic)                   OTLP Traces (Sampled 1% Waterfalls)
            │                                                     │
            ▼                                                     ▼
   Prometheus (calls_total, duration)                   Grafana Tempo (Trace Storage)
```

---

## 3. Core Invariants

| # | Invariant | Verification Method |
|---|---|---|
| **I1** | **Exact Count Parity**: 100% of real requests, DB queries, and Redis calls are counted in Prometheus, even at `sample_rate = 0.0001` or `0.0`. | Unit tests comparing known call counts to `calls_total` under 0% and 1% sampling. |
| **I2** | **Weight Invariant**: Every observed span is incremented with unit weight (`1.0`). No artificial scaling factors. | Counter equality tests. |
| **I3** | **Trace Storage Invariant**: Tempo receives **only** sampled traces (`trace_flags.sampled == True`). Unsampled spans never reach the network. | Tempo query assertions and mock exporter stream inspection. |
| **I4** | **Single Owner Invariant**: Each metric series has exactly one owner: Django SERVER spans by `request.py`, CLIENT spans by `RedSpanProcessor`, Celery workers by `RedSpanProcessor`. Zero double-counting. | Collector filter assertion; synthetic traffic reconciliation. |
| **I5** | **Zero Host Degradation**: Span recording and metric updates degrade gracefully without crashing application threads or slowing down high-throughput hot paths. | Exception-injection tests in span processors. |
| **I6** | **Strict Cardinality Caps**: Route, database, and client dimension combinations are capped with a sentinel fallback (`__other__`) to protect Prometheus RAM. | Synthetic cardinality overflow test. |

---

## 4. Component Specification

### 4.1 Sampler Updates (`src/tp_trace/sampler.py`)
1. **Decision Mapping**:
   * When `ignore_endpoints` matches: return `Decision.DROP`.
   * When ratio sampler keeps: return `Decision.RECORD_AND_SAMPLE`.
   * When ratio sampler rejects: return `Decision.RECORD_ONLY`.
2. **Context & Child Inheritance**:
   * Configure `ParentBased` delegates:
     * `local_parent_sampled` $\to$ `RECORD_AND_SAMPLE`
     * `local_parent_not_sampled` $\to$ `RECORD_ONLY` (currently defaults to `DROP`, which severed child spans!).
     * `remote_parent_sampled` $\to$ `RECORD_AND_SAMPLE`
     * `remote_parent_not_sampled` $\to$ `RECORD_ONLY`
   * This guarantees that when a Django root span is `RECORD_ONLY`, all child DB and cache spans also become `RECORD_ONLY` instead of being dropped!

### 4.2 New RED Span Processor (`src/tp_trace/red.py`)
Create a new module containing two processors:

#### A. `RedSpanProcessor(SpanProcessor)`
* **`on_end(span: ReadableSpan) -> None`**:
  1. **Eligibility Check**:
     * Must be `SpanKind.CLIENT`, `SpanKind.PRODUCER`, or `SpanKind.CONSUMER`.
     * Skip `SpanKind.INTERNAL` (internal framework details).
     * Skip Django `SpanKind.SERVER` spans (already recorded by `request.py` to preserve rich response context and avoid double-counting).
  2. **Extraction & Normalization**:
     * Extract `service = span.attributes.get("normalized.service") or span.attributes.get("db.system") or span.attributes.get("peer.service")`.
     * Extract `operation = span.attributes.get("normalized.operation") or span.attributes.get("db.operation") or span.name`.
     * Extract `route = span.attributes.get("http.route") or "__unmatched__"`.
     * Extract duration in milliseconds from `(span.end_time - span.start_time) / 1e6`.
     * Extract status: `status = "error"` if `span.status.status_code == StatusCode.ERROR` or `span.attributes.get("error")` is True else `"ok"`.
  3. **Record via Metrics Recorder**:
     * Call `metrics_recorder.record_dependency_span(service, operation, route, status, duration_ms)`.
  4. **Fail-Safe**: Entire block is wrapped in `attempt()` to satisfy telemetry safety invariants.

#### B. `SampledOnlySpanProcessor(SpanProcessor)`
* Wraps the real exporting `BatchSpanProcessor`.
* **`on_end(span: ReadableSpan) -> None`**:
  * Check `if span.context.trace_flags.sampled: self._delegate.on_end(span)`
  * Unsampled (`RECORD_ONLY`) spans are dropped immediately from memory. They are never queued in the batch processor and never serialized over HTTP.

### 4.3 Native Metrics Extension (`src/tp_trace/metrics.py`)
Add dependency metric instruments and recording logic:
* Instruments: Reuses `calls_total` counter and `duration_milliseconds` histogram.
* Slicing dimensions:
  * `project_name`
  * `cluster_name`
  * `normalized_service` (`postgres`, `redis`, `requests`, `boto`, `celery`)
  * `normalized_operation` (`SELECT api_product`, `redis.get`, `HTTP GET`, etc.)
  * `http.route` (propagated from the pre-resolved route context)
  * `db.instance` / `peer.service` (`slave1db`, `slave2db`, `defaultdb`)
  * `status_code` / `error`
  * `worker_id` (`<prefix>:<pid>`)
* Cardinality Guard:
  * Route and operation combinations are bounded with an LRU/set cap (default 1024). Over-limit dimensions map to `"__other__"`.

### 4.4 SDK Wiring (`src/tp_trace/__init__.py`)
Register span processors in strict order:
1. `RouteEnrichingSpanProcessor`: Enrich span with `http.route` at `on_start`.
2. `RedSpanProcessor`: Aggregate RED metrics at `on_end`.
3. `SampledOnlySpanProcessor(BatchSpanProcessor(SafeSpanExporter(...)))`: Export only sampled spans to Tempo.

### 4.5 Collector Pipeline Updates (`dev/` and `ansible/`)
Because the SDK now natively emits RED metrics for all dependency spans:
* Update `dev/otel-collector/config.yaml` and `ansible/roles/otel-collector/templates/otel-collector-config.yaml.j2`:
  * Drop dependency spans from the collector's `spanmetrics` connector pipeline:
    ```yaml
    filter/spanmetrics_drop_sdk_instrumented:
      error_mode: ignore
      traces:
        span:
          - attributes["telemetry.sdk.name"] == "tp_trace"
    ```
  * This guarantees zero duplicate series between SDK native metrics and the Collector connector.

---

## 5. Decision Matrix

| Span Origin | Span Kind | Sample Rate Decision | Span Recorded in RAM? | RED Metric Emitted? | Trace Sent to Tempo? |
|---|---|---|---|---|---|
| **Health Check** (`/health*`) | `SERVER` | Match `ignore_endpoints` | **No** (`DROP`) | **No** | **No** |
| **Django View** | `SERVER` | Kept by ratio (1%) | **Yes** (`RECORD_AND_SAMPLE`) | **Yes** (via `request.py`) | **Yes** |
| **Django View** | `SERVER` | Dropped by ratio (99%) | **Yes** (`RECORD_ONLY`) | **Yes** (via `request.py`) | **No** |
| **Postgres Query** | `CLIENT` | Parent is kept (1%) | **Yes** (`RECORD_AND_SAMPLE`) | **Yes** (via `RedSpanProcessor`) | **Yes** |
| **Postgres Query** | `CLIENT` | Parent is dropped (99%)| **Yes** (`RECORD_ONLY`) | **Yes** (via `RedSpanProcessor`) | **No** |
| **Redis Command** | `CLIENT` | Parent is dropped (99%)| **Yes** (`RECORD_ONLY`) | **Yes** (via `RedSpanProcessor`) | **No** |
| **Celery Worker** | `CONSUMER`| Dropped by ratio (99%) | **Yes** (`RECORD_ONLY`) | **Yes** (via `RedSpanProcessor`) | **No** |
| **Internal Template**| `INTERNAL`| Any | Inherited | **No** (Skipped by RED) | Only if trace is sampled |

---

## 6. Implementation Stages & Verification

### Stage 1: Sampler Delegation & Export Gate
* Update `src/tp_trace/sampler.py` to map ratio rejections to `RECORD_ONLY`.
* Add `local_parent_not_sampled` and `remote_parent_not_sampled` delegates returning `RECORD_ONLY`.
* Implement `SampledOnlySpanProcessor` in `src/tp_trace/red.py`.
* **Validation**:
  * Unit tests: assert rejected spans produce `is_recording() == True` but `trace_flags.sampled == False`.
  * Exporter tests: assert `InMemorySpanExporter` receives zero spans when sampling rate is 0.0.

### Stage 2: `RedSpanProcessor` & Metric Instrumentation
* Implement `RedSpanProcessor` and hook `metrics_recorder.record_dependency_span()`.
* Add cardinality caps for operation and route dimensions.
* Wire into `tp_trace.init()`.
* **Validation**:
  * Run 1,000 DB queries and 500 Redis commands under `sample_rate = 0.0001`.
  * Assert Prometheus `calls_total` records exactly 1,000 DB queries and 500 Redis calls.
  * Assert Tempo exporter received 0 (or 1) trace.

### Stage 3: Collector Synchronization
* Update `dev/otel-collector/config.yaml` and Ansible template `otel-collector-config.yaml.j2`.
* Verify `make check-config` and run local Docker stack.
* Run `make smoke` and `tests/e2e/compare_red_metrics.py`.

### Stage 4: Testpress Production Validation
* Verify with `testpress_python` test suite.
* Validate in production at `http://65.21.236.56:9090` that Postgres, Redis, and Celery call rates match actual production workload.
