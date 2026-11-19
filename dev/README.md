# Local Development Stack

Follow the root [local setup guide](../README.md#local-setup) for prerequisites,
startup, traffic generation, telemetry checks, and shutdown.

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Local stack definition |
| `sample-app/` | Django application, Celery worker, Dockerfile, and traffic generator |
| `.observability-infra/` | Upstream service configs synced from `testpress/observability-infra` |
| `../config/otel-collector.yaml` | Shared Collector pipelines and tuning defaults |
| `host-app.env.example` | SDK environment example for a host-run application |

This directory contains local development infrastructure. Collector fleet deployment
files remain in `ansible/`. Observability server infrastructure is managed in `observability-infra`.

## Stack configuration

Shared service configuration lives in [`config/`](../config/). Local overrides belong in the root `.env`. Run `make up` after changing container environment values; use `make restart SERVICE=otel-collector` to reload edits to its existing mounted configuration.

### VM Collector connectivity

For application → VM Collector → local storage, point `TP_TRACE_ENDPOINT` at `http://<vm-host>:4318`. The VM Collector sends traces to `<development-host>:4327` and metrics to `http://<development-host>:9090/api/v1/write`. The development host must be reachable from the VM.

For deployment settings, see [Collector deployment](../ansible/README.md).
