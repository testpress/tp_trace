# Grafana Dashboards & Navigation Guide

This guide covers the Grafana APM dashboards in the `tp_trace` Git Sync source,
explaining how each is structured, how to navigate between them, and how to
execute end-to-end incident triage.

> **Why only 4?** The suite originally shipped 8 dashboards — two generic
> (Service Catalog, Needs Attention) plus six component-specific ones (Django
> Overview/Endpoint, PostgreSQL Overview/Query, Redis Overview/Command). The six
> component-specific dashboards were consolidated into two **generic** dashboards
> driven by the `normalized.service` / `normalized.operation` attributes that the
> SDK emits on spans. A service is
> now a *value of a variable*, not a dashboard. Django, PostgreSQL, PgBouncer,
> and Redis all work through the same two panels, and a newly instrumented
> service appears in the catalog with no dashboard work at all.

---

## 1. Dashboard Navigation Map

```text
                      ┌─────────────────────────────────────────┐
                      │          Needs Attention                │
                      │   tp_trace-needs-attention             │
                      │  (Triage: severity counters + issues)   │
                      └────────────────────┬────────────────────┘
                                           │
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │           Service Catalog               │
                      │        tp_trace-project-catalog        │
                      │  (auto-detected services, health,      │
                      │   throughput, % time by downstream)     │
                      └────────────────────┬────────────────────┘
                                           │  one row per service,
                                           │  link carries ?var-service=…
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │       Generic Service Overview          │
                      │          generic-service-overview       │
                      │  ($service drives the whole dashboard:  │
                      │   RPS, error %, P50/P95/P99,           │
                      │   operations table)                     │
                      └────────────────────┬────────────────────┘
                                           │  one row per operation,
                                           │  link carries ?var-operation=…
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │      Generic Operation Details          │
                      │         generic-operation-details       │
                      │  (hits/errors by status, latency,       │
                      │   TraceQL trace list)                   │
                      └────────────────────┬────────────────────┘
                                           │
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │              Grafana Tempo              │
                      │  (Trace Waterfall & Flamegraph Engine)  │
                      └─────────────────────────────────────────┘
```

---

## 2. Dashboard Catalog & Details

### 1. Needs Attention — Operational Issues Overview

* **UID**: `tp_trace-needs-attention` (v1003) · 5 panels (3 stat, 2 dynamic-text)
* **Source**: [`operational_issues.json`](../docker/grafana/dashboards/Supporting Dashboards/operational_issues.json)
* **Purpose**: Primary incident triage board. Surfaces any service or operation currently failing thresholds.
* **Key Panels**:
  * **Critical / Warning / Info counters** — three stat panels, each counting
    how many issues currently match that severity band. Each is a link that sets
    `var-severity` and filters the issue table below.
  * **Detected Operational Issues table** — a DynamicText panel rendering an
    HTML table from 10 instant PromQL targets. Each issue card shows severity,
    headline, description, affected operation, baseline, deviation, and
    contribution.
  * When no issues are present the panel **auto-collapses** to a thin
    "All Systems Healthy" banner.

**Issue detection rules** (evaluated as instant queries at panel refresh):

| Severity | Condition |
| :--- | :--- |
| 🟡 High latency — PostgreSQL | P95 > 2× the 30m P50, Django P95 > 250ms, and PostgreSQL accounts for > 30% of Django duration |
| 🟡 High latency — Redis | Same shape, Redis accounting for > 20% |
| 🟡 High latency — Django internal | Residual (Django minus downstream) > 60%, plus the P95/P50 and 250ms conditions |
| 🔴 Request failures | Error ratio > 2% per `(http_route, http_method)` |
| 🔴 Database failures | Error ratio > 1% for `db_system=~"postgresql\|postgres"` |
| 🔴 Cache failures | Error ratio > 1% for `db_system="redis"` |
| ℹ️ Traffic surge | Current 5m rate > 200% of the 7-day median baseline, and absolute rate > 20 |

> **Caveat**: the traffic-surge rule compares against a
> `quantile_over_time(0.5, …[7d:1h])` baseline, so it needs **7 days** of
> Prometheus history. It cannot fire on a freshly provisioned Prometheus.
> `resource/drop_ephemeral` in the Collector also strips `service.instance.id`
> and `process.pid`, so gunicorn worker restarts do not fork new series.

---

### 2. TP Trace — Application Performance Overview
 
* **UID**: `tp_trace-project-catalog` (v1003) · 5 panels (1 dynamic-text, 1 table, 2 timeseries)
* **Source**: [`service_catalog.json`](../docker/grafana/dashboards/service_catalog.json)
* **Purpose**: Single-pane-of-glass overview across every instrumented service.
* **Key Panels**:
  * **Active Issues & Anomaly Detection** — the same A–G detection logic as
    Needs Attention, rendered as a card grid. Collapses when healthy.
  * **Installed Services** — a table that **derives the service list from
    telemetry** rather than hardcoding it. Each service is detected by its span
    signature and given a synthetic `Dashboard` link target:
    * `django` — spans named `django.request`
    * `postgresql` / the `db_instance` value — `db_system="postgresql"`
    * `pgbouncer` — `server_address="pgbouncer"` or `server_port="6432"`
    * `redis` — `db_system="redis"`
    * `requests` — `HTTP.*` / `🌐.*` spans, or verb-shaped spans with no `db_system`
  * Columns: Throughput (ops/s), Error Rate %, P95 Latency, Total Calls, and a
    synthesized **Status** (Healthy / Degraded / No data).
  * **Throughput by Service** — RPS split across Django, PostgreSQL, Redis, and
    external HTTP.
  * **% Time Spent by Downstream Service** — a 0–100% stacked breakdown of where
    request time goes: PostgreSQL, Redis, external HTTP, and Django internal
    logic. A floor of 1.5% of Django total is applied to the residual to avoid
    division blowups.

Adding a new instrumented service requires no dashboard change — it appears here
automatically.

---

### 3. Generic Service Overview

* **UID**: `generic-service-overview` (v1005) · 5 panels (3 timeseries, 1 table, 1 row)
* **Source**: [`service_overview.json`](../docker/grafana/dashboards/Supporting Dashboards/service_overview.json)
* **Purpose**: Per-service health for **any** service. This single dashboard
  replaced the Django / PostgreSQL / Redis overview dashboards.
* **Key Panels**:
  * **Requests Throughput** — `sum(rate(apm_calls_total{…}))` filtered by
    `normalized_service`.
  * **Error Rate Over Time** — percentage of calls with `error="true"`.
  * **Latency Percentiles** — P50 / P95 / P99 via `histogram_quantile()` over
    `apm_duration_milliseconds_bucket`, with exemplars attached.
  * **Operations Summary** — one row per `normalized_operation` with RPS, error
    %, and P50/P95/P99. Each row links to Generic Operation Details. Rows with
    RPS = 0 are filtered out.

`$service` is populated from `label_values(apm_calls_total{…}, normalized_service)`
and defaults to `django`. A `normalized_service!="github.com"` filter excludes
one off-nominal value that would otherwise appear as a service.

---

### 4. Generic Operation Details

* **UID**: `generic-operation-details` (v1011) · 6 panels (3 timeseries, 1 table, 2 rows)
* **Source**: [`operation_details.json`](../docker/grafana/dashboards/Supporting Dashboards/operation_details.json)
* **Purpose**: Deep-dive on a single operation within a single service. This
  replaced the Django Endpoint / PostgreSQL Query / Redis Command dashboards.
* **Key Panels**:
  * **Requests and Errors** — hits vs errors over time.
  * **Errors by Status Code** — error volume split by `http_status_code`.
  * **Latency** — P50 / P90 / P95 / P99, reported in **seconds**.
  * **Recent Traces & Flamegraph Waterfall** — a Tempo table panel running the
    only TraceQL query in the stack:

    ```traceql
    { span.normalized.service =~ `${service:regex}` && span.normalized.operation =~ `${operation:regex}` }
    ```

    The `traceID` column links to the Tempo flamegraph for that trace.

Because Tempo stores the *same* `normalized.service` / `normalized.operation`
attributes the metrics use, metrics and traces share one identity vocabulary —
that is what makes this join work.

> **Caveat**: the Latency panel reports **seconds** (divides by 1000) while the
> adjacent `duration` column override is set to **ms**. Units are inconsistent
> between the two panels in this dashboard.

---

## 3. Operational Workflows & Triage Playbooks

### Workflow 1: Triage an Incident (Spike to Root Cause)

```text
[1. Needs Attention]
        │  Engineer sees a Critical counter, e.g. error rate > 2% on /api/orders/
        ▼
[2. Generic Service Overview]
        │  $service=django. Operations table shows /api/orders/ is the outlier.
        ▼
[3. Generic Operation Details]
        │  Errors split by status code; P95 climbing. Trace list at the bottom.
        ▼
[4. Open Trace / Exemplar]
        ▼
[5. Tempo Trace Waterfall]
        │  Locates failed span: 🌐 HTTP GET https://inventory.internal/stock (504)
        ▼
[6. Root Cause Isolated]
```

### Workflow 2: Click-to-Trace via Prometheus Exemplars

When viewing any Latency or Duration panel in Grafana:
1. Look for **blue diamonds / dots** hovering above the metric line.
2. Hover over a dot to see the attached metadata (`trace_id`, `duration`).
3. Click the `trace_id` link in the popup.
4. Grafana opens the **Tempo trace waterfall**, showing the exact execution tree
   for that measurement.

```text
Latency Graph (Prometheus)
  │
  ├───────◆ (Exemplar: trace_id=4bf92f3577b34da6…) ──► [ Click ]
  │                                                          │
  └──────────────────────────────────────────────────────────┼───────────────► Tempo Waterfall
                                                                                ├── django.request
                                                                                └── 🐘 SELECT * FROM items (Slow)
```

Exemplars require exemplar storage to be enabled in the existing Prometheus
deployment and an `exemplarTraceIdDestinations` mapping in the existing Grafana
Prometheus datasource that points to the Tempo datasource.

### Workflow 3: Diagnosing Slow Database Calls (PgBouncer vs Engine)

Pooler wait and engine execution are **not** separate spans — one span covers the
whole round trip. Separate them analytically:

1. Open **Generic Service Overview** with `$service=postgresql` (or `pgbouncer`).
2. Compare the two:
   * If **PgBouncer** latency is high while the `postgresql` service shows normal
     latency, the bottleneck is **pool contention**, not the database engine.
   * If `postgresql` latency is high, the **query itself** is the problem
     (missing index, table scan).
3. Switch `$operation` in **Generic Operation Details** to the slow sanitized
   SQL, and use the TraceQL trace list to see which Django views trigger it.

Pooler-routed spans carry `db.connection.pool="pgbouncer"`.
Tempo span names are prefixed `🔹` for pooled and `🟢` for direct PostgreSQL queries (and `🔸` for Redis).

---

## 4. Common Variables & Dashboard Controls

| Variable | Where | Description | Example Values |
| :--- | :--- | :--- | :--- |
| **`$project`** | all | Selects the target service/application | `otel-sample`, `django-lite-app` |
| **`$cluster`** | all | Selects the environment/cluster | `production`, `Dummy`, `done` |
| **`$service`** | generic ×2 | Selects a `normalized_service` | `django`, `postgresql`, `pgbouncer`, `redis`, `requests` |
| **`$operation`** | generic ×2 | Selects a `normalized_operation` | `GET /api/products/`, `🐘 SELECT api_product` |
| **`$severity`** | Needs Attention | Filters the issue table. Set by the severity counters; applied **client-side** in the panel's Handlebars helper, not in PromQL. | `All`, `Critical`, `Warning`, `Info` |
| **`$Filters`** | all | Ad-hoc filter bar for custom label matchers | `error = true`, `http_status_code = 500` |
| **Time Range** | all | Time window for PromQL aggregation | `Last 15 minutes`, `Last 1 hour` |
---

## 5. Summary Cheat Sheet

| I want to… | Open |
| :--- | :--- |
| Triage an incident | [`operational_issues`](../docker/grafana/dashboards/Supporting Dashboards/operational_issues.json) |
| See overall service health | [`service_catalog`](../docker/grafana/dashboards/service_catalog.json) |
| Drill into any service | [`service_overview`](../docker/grafana/dashboards/Supporting Dashboards/service_overview.json) |
| Drill into any operation | [`operation_details`](../docker/grafana/dashboards/Supporting Dashboards/operation_details.json) |
| Inspect a raw trace | Grafana **Explore** → Tempo datasource |
