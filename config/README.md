# Shared Telemetry Configuration

| File | Consumers |
|---|---|
| `otel-collector.yaml` | Local Compose mounts it; Ansible copies the same file to deployed hosts |
| `tempo.yaml` | Local and deployment Compose mount the same file |
| `prometheus.yml` | Local Compose mounts it; externally managed production Prometheus must adopt it separately |

The Collector uses native `${env:NAME}` substitution. Pipeline and tuning defaults
live only in its YAML. Local Compose supplies destination addresses, listener
bindings, and identity labels. Ansible maps existing `otel_collector_*` variables
to environment values; explicit tuning overrides remain supported. Deploy the
Ansible role from the full repository checkout so it can find this directory.

Required Collector values are `OTEL_COLLECTOR_TEMPO_ENDPOINT`,
`OTEL_COLLECTOR_PROMETHEUS_REMOTE_WRITE_ENDPOINT`, `OTEL_COLLECTOR_INSTANCE`, and
`OTEL_COLLECTOR_ALIAS`. Other substitutions have defaults in the YAML. The health-check extension is
disabled by default (`OTEL_COLLECTOR_EXTENSIONS=[]`), so Ansible deployments do
not open port 13133. Local Compose explicitly enables it with
`OTEL_COLLECTOR_EXTENSIONS=[health_check]` and binds to `0.0.0.0:13133` for the
existing development health probe. Merely defining the extension does not start it.

Local now uses the previous production tuning: 400/80 MiB memory/spike limits,
500 ms batching with 256/512-item trigger/maximum sizes, and a Tempo queue of
5,000 batches with eight consumers. Former local values were 256/64 MiB,
1-second batching with 512/1,024 items, and a 1,000-batch queue with four consumers.
This changes local buffering capacity and cadence, not metric labels or sampling.
Tempo settings and persistent volume identities are unchanged. Local Compose
publishes Tempo OTLP/gRPC on host port `4327`, matching deployment Compose. A
Collector running in a VM can use `otel_collector_tempo_endpoint` set to the
development host's reachable address on port `4327` (for example,
`192.168.1.3:4327`). The in-stack Collector continues to use `tempo:4317`.
After adding this port mapping, reapply Tempo with
`docker compose -f dev/docker-compose.yml up -d --no-deps tempo` from the root;
restarting alone does not apply a new port mapping. If using a root `.env`, add
`--env-file .env` before `-f`. `DEV_BIND_ADDRESS` must permit the VM connection.

From the repository root, validate with:

```bash
make check-config
make check-collector-config
docker compose -f setup/docker-compose.yml config --quiet
```

Use `make up` for the first local rollout of the changed mounts and environment.
Use the Ansible configure playbook separately for production. Shared source does
not automatically update running hosts. Production Prometheus is not deployed
by this repository; its canonical scrape file requires `localhost:9090` and
`tempo:3200` to resolve in that environment.
