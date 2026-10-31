# Shared Telemetry Configuration

## Source of Truth

Local and deployed services consume the canonical files in [`config/`](../config/).
There are no separate development pipeline definitions or generated YAML copies
checked into source control.

| File | Local consumer | Deployment consumer |
|---|---|---|
| `config/otel-collector.yaml` | Local Compose bind mount | Ansible copies the same bytes to the host and mounts them |
| `config/tempo.yaml` | Local Compose bind mount | `setup/docker-compose.yml` bind mount |
| `config/prometheus.yml` | Local Compose bind mount | Available for the separately managed Prometheus deployment |

Production Prometheus is not deployed by this repository's Compose file. Its
operator must mount the canonical Prometheus file to adopt the same scrape
configuration. The file scrapes `localhost:9090` and `tempo:3200`, so those names
must resolve in that deployment. This repository cannot enforce configuration
changes in an external deployment repository.

## Collector Environment Values

The shared Collector YAML uses native `${env:NAME}` substitutions. Values with
`:-default` have their fallback defined in the shared YAML itself. Both local
and production use Collector `0.153.0` by default.

The required destination and identity variables are:

- `OTEL_COLLECTOR_TEMPO_ENDPOINT`
- `OTEL_COLLECTOR_PROMETHEUS_REMOTE_WRITE_ENDPOINT`
- `OTEL_COLLECTOR_INSTANCE`
- `OTEL_COLLECTOR_ALIAS`

Local Compose sets these to service names and development identity labels. It
also sets receiver, health-check, and internal-metrics bindings to `0.0.0.0` so
other containers and published host ports can reach them. Ansible supplies the
host-specific values from its existing `otel_collector_*` variables. The shared
configuration defaults `OTEL_COLLECTOR_EXTENSIONS` to an empty list. Ansible
deployments therefore do not open a health-check listener. Local Compose
explicitly sets `[health_check]` and publishes port `13133`. Defining an extension
without enabling it in `service.extensions` does not start its listener.

## Shared Behavior and Defaults

The pipelines, resource cleanup, worker stream preservation, histogram handling,
retries, and queues have one definition. Local development now uses the previous
production tuning defaults: 400 MiB hard memory limit with an 80 MiB spike buffer;
500 ms batching with a 256-item trigger and 512-item maximum; and a Tempo export
queue of 5,000 batches with eight consumers. The delta conversion stream cap
remains 50,000, with a five-minute stale timeout.

The former local configuration used 256/64 MiB memory limits, 1-second batching
with 512/1,024-item sizes, and a 1,000-batch Tempo queue with four consumers.
Adopting production settings changes local buffering capacity and export cadence;
it does not change SDK metric names, attributes, or sampling.

Existing explicit Ansible tuning overrides remain supported. Absent overrides
are omitted from the container environment, so defaults come from the shared
YAML instead of a second defaults table. A production override can still differ
from local defaults; reproduce that override deliberately when diagnosing its
behavior. Do not add a second pipeline file to express it.

## VM Collector Connectivity

A Collector running in a VM can send traces to the development host on port
`4327`, which maps to Tempo's container port `4317`. Set
`otel_collector_tempo_endpoint` to a reachable `host:4327`, without an HTTP scheme.
The in-stack Collector uses `tempo:4317`. The development host also publishes
Prometheus on `9090` for the VM Collector's remote-write exporter.

The sample app uses OTLP/HTTP in the opposite direction: its `TP_TRACE_ENDPOINT`
can point to `http://collector.example.com:4318` in the VM. Thus the complete path
is sample app → VM Collector → development-host Tempo and Prometheus. Receiver
ports (`4317`/`4318`) and the host-published Tempo ingestion port (`4327`) serve
different connections. `DEV_BIND_ADDRESS=127.0.0.1` prevents access from a remote VM.

Use `make up` after changing port mappings or environment values. A container
restart alone does not apply those changes.

## Storage and Access

Tempo uses the same storage and query settings in both environments, including
its local filesystem backend, WAL, and seven-day block retention. Compose still
owns each environment's mounts, ports, network, container limits, and volumes.
Those deployment details are intentionally separate. Volume and project names
remain unchanged, preserving existing data identities.

Development's published ports and anonymous Grafana access are not propagated
to the deployment Compose file or Ansible firewall configuration.

## Validation

From the repository root:

```bash
make check-config
make check-collector-config
docker compose -f setup/docker-compose.yml config --quiet
```

The Collector check starts a temporary validation container without dependencies;
it does not start or replace the stack. CI runs this check against the pinned
image. Regression tests also ensure both deployment paths consume the canonical
files and preserve the native-metrics pipeline contracts.

After a shared configuration change, reapply the local services with `make up`
when mounts or environment values changed. Later edits to the same mounted
Collector file can be loaded with `make restart SERVICE=otel-collector`.
Run `make smoke` and inspect stored telemetry before deploying. Production hosts
need a separate Ansible configuration rollout; sharing a source file does not
update already-running remote services automatically.
