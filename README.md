# tp_trace

`tp_trace` adds OpenTelemetry tracing and native request/dependency metrics to
Python applications. This repository includes a local Django application and an
observability stack for exercising the integrations.

## Local Setup

Install Docker with Compose support. For SDK development on the host, use Python
3.8 or later; lint/type checks require Python 3.10 or later. The sample image uses
Python 3.8 and its own pinned application dependencies.

From the repository root:

```bash
cp .env.example .env
make install
make check-config
make check-collector-config
make rebuild
```

Skip the copy if `.env` already exists. `make install` creates `.venv` and installs
the SDK with development dependencies. `make rebuild` builds the sample image and
starts the local stack. Later, `make up` starts services using existing images;
repeat `make rebuild` after dependency or Dockerfile changes.

The sample application is in `dev/sample-app/`. Collector, Tempo, and Prometheus
configuration lives in `config/`; local and deployment entry points use those
canonical files. The only local Compose definition
is `dev/docker-compose.yml`; Makefile commands select it explicitly. There is no
root Compose file. To operate Compose directly from the root:

```bash
docker compose --env-file .env -f dev/docker-compose.yml ps
```

From `dev/`, use `docker compose --env-file ../.env ps`. Root Makefile commands
also work without a `.env` file, using Compose defaults. Exported environment
variables override file values. Port overrides belong in the root `.env`.

## Open the Services

| Service | Default host URL or port |
|---|---|
| Sample Django application | <http://localhost:8002/api/> |
| Grafana | <http://localhost:3001> |
| Prometheus | <http://localhost:9090> |
| Tempo query API | <http://localhost:3200> |
| Tempo OTLP/gRPC for VM Collectors | `4327` |
| Collector OTLP/HTTP and OTLP/gRPC | `4318` and `4317` |
| Collector health | <http://localhost:13133/> |
| MinIO API and console | `9002` and `9003` |

The development stack exposes services on `0.0.0.0` by default and enables
anonymous Grafana Admin access. Set `DEV_BIND_ADDRESS=127.0.0.1` in `.env` to
restrict published ports to this machine. These are local development settings,
not a production deployment template.

## Generate and Inspect Telemetry

Send a finite workload to the default local application:

```bash
make traffic
# Choose a workload explicitly:
make traffic ARGS='--count 20 --redis --http'
make smoke
```

For a custom application port, pass its address to the traffic generator:

```bash
BASE_URL=http://localhost:8005 make traffic
```

`make smoke` checks ingestion through to storage, including application metrics
and traces. A healthy container alone is insufficient. Metrics normally export
every 60 seconds, so allow time for a new workload to appear. In Grafana, open
**TP Trace Dashboards**, select the project and cluster, and inspect request rates
and operation traces. The sample defaults to project `otel-sample`, cluster
`Dummy`, and a 1% global trace sampling rate with selected endpoint overrides.
A metric datapoint therefore does not guarantee a matching exported trace.
If a trace panel is empty, expand its time range before assuming ingestion failed.
For a deterministic trace check, send a request to an endpoint with a 100% rule,
such as `/api/multi-db/`, and inspect the response's `X-Trace-ID` in Tempo.

## Configure SDK Export

The sample application and Celery worker read `TP_TRACE_ENDPOINT` from the
Compose environment. To send their telemetry to a reachable deployed Collector:

```bash
TP_TRACE_ENDPOINT=http://collector.example.com:4318 make up
```

Set `TP_TRACE_ENDPOINT` in the root `.env` to keep this override for later
`make up` calls. Recreate affected services with `make up`; `make restart` alone
does not reload container environment values.

Grafana continues to query the local stores. If the VM Collector forwards to
these stores, the local dashboards show its data; otherwise, use the destination
environment's dashboards. See [VM connectivity](docs/06-environments.md#vm-collector-connectivity)
for the addresses and ports on each connection. For SDK settings, sampling, masking, and integration controls,
see [configuration](docs/05-configuration.md) and [integrations](docs/04-integrations.md).

## Operate the Stack

```bash
make ps
make logs SERVICE=sample-app
make logs SERVICE=otel-collector
make restart SERVICE=otel-collector
make down
```

`make down` preserves named data volumes. `make nuke` deletes local stack volumes
and all their persistent data; use it only for an intentional reset. The Compose
project remains `tp-trace-dev`, preserving existing local volume identities after
repository restructuring. Fixed container and network names mean separate
worktrees cannot run independent copies of this stack concurrently without
additional overrides.

## Troubleshooting

Check `make ps` and service logs first. For host port conflicts, change the
corresponding value in `.env` and reapply with `make up`. For missing telemetry,
check the application's endpoint, Collector export logs, destination storage,
and dashboard project/cluster filters. Prometheus remote-write traffic uses
port `9090`; an unreachable destination can cause timeouts and dropped exports.
For the first rollout of the shared config paths and environment values, use
`make up` to recreate affected containers while preserving volumes. Subsequent
edits to the mounted Collector file reload with `make restart SERVICE=otel-collector`.

## Further Reading

- [Developer documentation index](docs/README.md)
- [Observability concepts](docs/01-observability-basics.md)
- [Architecture](docs/02-architecture.md)
- [Local and production configuration](docs/06-environments.md)
- [Collector deployment with Ansible](ansible/README.md)
- [Tempo deployment](setup/README.md)
