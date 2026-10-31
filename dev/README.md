# Local Development Stack

Follow the root [local setup guide](../README.md#local-setup) for prerequisites,
startup, traffic generation, telemetry checks, and shutdown.

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Local stack definition |
| `sample-app/` | Django application, Celery worker, Dockerfile, and traffic generator |
| `../config/otel-collector.yaml` | Shared Collector pipelines and tuning defaults |
| `../config/prometheus.yml` | Canonical Prometheus scrape configuration |
| `../config/tempo.yaml` | Shared Tempo storage and query configuration |
| `host-app.env.example` | SDK environment example for a host-run application |

This directory contains local development infrastructure. Deployment files remain
in `setup/` and `ansible/`; see [environment boundaries](../docs/06-environments.md).
