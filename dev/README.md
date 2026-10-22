# Local Development Environment

Run the full local observability development environment including the sample Django application, PostgreSQL, Redis, OpenTelemetry Collector, Tempo, Prometheus, and Grafana.

## Starting the Stack

From the repository root ("home"):

```bash
docker compose up -d
```

Or from the `dev/` directory:

```bash
docker compose up -d
```

## Available Services

| Service | Host Port | Description |
|---|---|---|
| Sample Django App | `8002` | [http://localhost:8002/](http://localhost:8002/) (API & Admin) |
| Grafana | `3001` | [http://localhost:3001/](http://localhost:3001/) (Anonymous Admin login) |
| Prometheus | `9090` | [http://localhost:9090/](http://localhost:9090/) (Metrics & TSDB) |
| Tempo | `3200` | [http://localhost:3200/ready](http://localhost:3200/ready) (Readiness endpoint) |
| OTel Collector | `4317` / `4318` | OTLP gRPC (`4317`) and HTTP (`4318`) trace receiver |
| MinIO (S3 API) | `9002` | [http://localhost:9002/](http://localhost:9002/) (S3-compatible object storage) |
| MinIO Console | `9003` | [http://localhost:9003/](http://localhost:9003/) (MinIO web console) |

Port overrides can be specified via environment variables before running `docker compose`:
- `APP_PORT`: Django sample app port (default `8002`)
- `GRAFANA_PORT`: Grafana port (default `3001`)
- `MINIO_PORT`: MinIO S3 API port (default `9002`)
- `MINIO_CONSOLE_PORT`: MinIO web console port (default `9003`)

## Pre-provisioned Dashboards

Grafana automatically provisions all dashboards from `grafana/dashboards/` into the **TP Trace Dashboards** folder:
- **Service Catalog** (`service_catalog.json`): Active services inventory, health status, and anomaly detection.
- **Service Overview** (`Supporting Dashboards/service_overview.json`): Golden signal KPIs, error rate, throughput, and latency percentiles.
- **Operation Details** (`Supporting Dashboards/operation_details.json`): Endpoint deep-dive with distributed flamegraph waterfall.
- **Application Errors** (`Supporting Dashboards/service_errors.json`): Error rates and failure analysis.
- **Operational Issues** (`Supporting Dashboards/operational_issues.json`): Application anomalies and incident investigation.
- **Collector Health** (`Supporting Dashboards/collector_monitoring.json`): Collector resource metrics, batch processing, and queues.

## Stopping the Stack

```bash
docker compose down
```

To clean all persistent volume data:

```bash
docker compose down -v
```
