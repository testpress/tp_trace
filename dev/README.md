# Local Development Environment

Run the full local observability development environment including the sample Django application, PostgreSQL, Redis, OpenTelemetry Collector, Tempo, Prometheus, and Grafana.

## Starting the Stack

Optionally copy `.env.example` to `.env` in this directory to override ports and the bind address.

From the repository root:

```bash
make rebuild   # first start, or after dependency or Dockerfile changes
make up        # later starts, using existing images
```

Or run Compose directly. From the repository root:

```bash
docker compose -f dev/docker-compose.yml up -d
```

Or from the `dev/` directory:

```bash
docker compose up -d
```

The `docker compose` commands in the rest of this document assume the `dev/` directory.

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
- `TP_TRACE_ENDPOINT`: OTLP/HTTP endpoint used by the sample app and Celery worker (default `http://otel-collector:4318`)

## Using a Deployed Collector

The sample app and Celery worker read their OTLP/HTTP endpoint from
`TP_TRACE_ENDPOINT`. Point them at a collector running elsewhere (for example the
Ansible deployment) before starting the stack:

```bash
TP_TRACE_ENDPOINT=http://<collector-host>:4318 docker compose up -d
```

The collector pushes metrics to Prometheus over TCP `9090`. The target host must
allow that port from the collector host; a filtered port does not fail fast. The
collector blocks for its `30s` exporter timeout and then logs
`Permanent error: context deadline exceeded` followed by
`Exporting failed. Dropping data.` — the affected metrics are lost, and this
exporter does not retry an expired request. Check connectivity from the
collector host:

```bash
nc -vz <prometheus-host> 9090
curl -sv --max-time 5 http://<prometheus-host>:9090/-/ready
```

The local stack gates the Collector on the Prometheus readiness check, so the
first internal metrics push does not race Prometheus startup.

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
