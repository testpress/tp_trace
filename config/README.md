# Shared Telemetry Configuration

| File | Consumers |
|---|---|
| `otel-collector.yaml` | Local Compose mounts it; Ansible copies the same file to deployed hosts |

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

For local startup, reloads, and VM connectivity, see the [development stack guide](../dev/README.md#stack-configuration).

From the repository root, validate with:

```bash
make check-config
make check-collector-config
```

Apply deployed Collector changes with the Ansible configure playbook. Shared source does not automatically update running hosts.
