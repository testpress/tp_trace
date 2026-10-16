# TP Trace — Grafana Dashboards

## Purpose

Use the TP Trace Grafana dashboards to monitor application performance, assess service health, identify anomalies, and investigate request or database-operation failures.

For instrumentation and implementation details, see the [tp_trace repository](https://github.com/testpress/tp_trace).

## Prerequisites

- Access to the Grafana instance hosting the TP Trace dashboards.
- Access to the relevant dashboards and telemetry data.
- A service instrumented using OpenTelemetry and configured to send telemetry to the observability stack.

## Git Sync

This directory is the Git Sync source of truth. Configure the existing Grafana
instance to sync this repository and point its dashboard path at
`docker/grafana/dashboards`. Keep the `Supporting Dashboards/` directory in the
same relative location: it contains the folders and drill-down dashboards used
by the Service Catalog.

Grafana needs working Prometheus and Tempo datasources before dashboards are
synced. Verify the datasource UIDs in Grafana match the UIDs referenced by the
JSON before publishing a change; dashboard queries will otherwise show `No
data` even when telemetry is available.

## How to Monitor Application Performance

### 1. Check the Service Catalog

Open **Service Catalog** to assess the overall health and performance of
monitored services.

Use this dashboard to:

- Review detected anomalies, when available.
- Monitor requests per second (RPS), error rate, and p95 latency for each service.
- Filter metrics by the available time range and dimensions.
- Filter by subdomain where supported by Testpress.
- Review overall request throughput and latency trends.
- Check the current health status of monitored services.

#### Interpret Service Health

Service health is evaluated over a rolling five-minute window based on the error percentage.

| Status | Error percentage (5m) |
|---|---:|
| Healthy (Green) | Below 1% |
| Warning (Orange) | 1% to below 5% |
| Critical (Red) | 5% and above |
| No Data (Gray) | No data available |

Use these thresholds to identify services that require further investigation. A *No Data* status indicates missing telemetry, not necessarily a healthy service.

### 2. Investigate Service Performance

Open **Service Performance — Latency, Throughput & Errors** for the service you want to investigate.

Review the following metrics:

- Requests per second (RPS).
- Error count.
- p95 latency.
- p99 latency.

Use the operations list to identify individual endpoints or operations with high latency, elevated error counts, or unexpected traffic patterns.

### 3. Investigate Request and Database Traces

Open **Request & Database Operation Traces — Flamegraphs** for the operation you want to investigate.

An operation can represent an HTTP endpoint or a SQL query.

Use the dashboard to:

1. Review the operation's RPS, error count, and latency metrics.
2. Identify relevant traces associated with the operation.
3. Inspect trace waterfalls or flamegraphs to understand where request time is spent.
4. Examine database operations and other spans to identify potential bottlenecks.

Correlate the operation's metrics with individual traces to narrow down the source of performance issues.

### 4. Investigate Application Errors

Open **Application Errors — Error Rates & Failure Analysis** when investigating failed requests or elevated error rates.

Use this dashboard to:

- Identify failed requests.
- Review exception breakdowns by HTTP status code.
- Inspect error traces using TraceQL error trace waterfalls.
- Correlate failures with the affected services and operations.

HTTP 4xx responses are excluded from the APM error count because they generally represent client-side errors. The APM focuses on application and server-side failures.

### 5. Investigate Anomalies and Incidents

Open **Application Anomalies & Incidents — Investigation** to review detected deviations across services and operations.

Use this dashboard to:

- Identify critical- and warning-level anomalies.
- Determine which services or operations are affected.
- Investigate the associated performance metrics and traces.
- Assess whether the deviation requires further investigation.

Anomaly data is available only when the relevant detection and data sources are configured and producing results.

### 6. Check OpenTelemetry Collector Health

Open **OpenTelemetry Collector — Health, Resource Usage & Metrics** to assess the health of the telemetry collection infrastructure.

Review:

- CPU utilization.
- Memory utilization.
- Collector health and resource usage metrics.

Use this dashboard when telemetry appears incomplete or delayed, or when investigating potential collection or resource-capacity issues.

## Troubleshooting Guidance

| Symptom | What to check |
|---|---|
| Service shows No Data | Verify application instrumentation, telemetry export, and the selected time range. |
| Error rate is elevated | Open the application error dashboard and inspect relevant traces. |
| Latency is elevated | Review service-level metrics, operation-level latency, and trace waterfalls. |
| Anomalies are not displayed | Verify that anomaly detection is configured and that data is available. |
| Telemetry appears incomplete | Check Collector health, resource utilization, and telemetry ingestion. |

## Related Resources

- [tp_trace repository](https://github.com/testpress/tp_trace) — Instrumentation and implementation details.
