# Local Development Environment

Run the full local observability development environment including the sample Django application, PostgreSQL, Redis, OpenTelemetry Collector, Tempo, Prometheus, and Grafana.

## Starting the Stack

From the repository root (the root Compose wrapper has been removed):

```bash
# First run, or after Dockerfile/dependency changes:
make rebuild
# Subsequent starts using existing images:
make up
```

Or from `dev/`, run `docker compose up -d --build` on the first run.
For subsequent starts:

```bash
docker compose up -d
```

Root Makefile commands explicitly select `dev/docker-compose.yml` and load a root
`.env` when present. From `dev/`, add `--env-file ../.env` to Compose commands
if using that file. Project, network, and volume names are unchanged. The sample
application and traffic generator are now in `dev/sample-app/`.

## Shared Configuration

Collector, Tempo, and Prometheus settings now live in the repository-root
[`config/`](../config/README.md). Local Compose and deployment consumers use
these canonical files. Only addresses, bindings, and identity labels differ
in local Collector environment values; tuning defaults match production.

Run `make check-collector-config` from the root to validate the shared Collector
YAML using the pinned image without starting dependencies. For the initial
mount/environment migration, run `make up`; later Collector file edits can be
reloaded with `make restart SERVICE=otel-collector`.

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

From the repository root, the sample app and Celery worker read their OTLP/HTTP endpoint from
`TP_TRACE_ENDPOINT`. Point them at a collector running elsewhere (for example the
Ansible deployment) before starting the stack:

```bash
TP_TRACE_ENDPOINT=http://collector.example.com:4318 make up
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

From the repository root:

```bash
make down
```

To clean all persistent volume data:

```bash
make nuke
```
