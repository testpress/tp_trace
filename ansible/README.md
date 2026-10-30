# OpenTelemetry Collector Ansible Automation

Ansible playbooks and roles to deploy, configure, and secure the OpenTelemetry Collector sidecar across Testpress application servers.

## Layout

```text
ansible.cfg
inventories/production/
  hosts.yml
  group_vars/
    app_servers.yml
playbooks/otel-collector/
  site.yml
  install.yml
  configure.yml
roles/otel-collector/
  defaults/main.yml
  handlers/main.yml
  tasks/
    main.yml
    install.yml
    configure.yml
    validate.yml
    verify.yml
  templates/
    otel-collector-config.yaml.j2
```

## How It Works

1. **Host-Level Sidecar Deployment:**
   The collector runs in Docker with `network_mode: host` on each application server. Applications (Django, Celery, etc.) push telemetry locally to `127.0.0.1:4317` (gRPC) or `127.0.0.1:4318` (HTTP).

2. **Local Batching & Memory Limiting:**
   Each collector instance buffers spans and metrics locally using a memory limiter and batch processor. This prevents bursty traffic from impacting app performance or dropping spans.

3. **Central Observability Forwarding:**
   - **Traces:** Exported via OTLP gRPC to Tempo on the Observability Host (`otel_collector_tempo_endpoint`, e.g. `staging1:4327`).
   - **Metrics:** Native RED and dependency metrics are exported via Prometheus Remote Write (`otel_collector_prometheus_remote_write_endpoint`, e.g. `staging1:9090`).
   - **Internal Collector Health:** Scraped locally on port `8888` and remote-written with instance tags.

4. **Firewall & iptables Integration:**
   Following the exact pattern used in the `pgbouncer` role:
   - **Package setup:** `iptables-persistent` is preseeded with debconf and installed in `tasks/install.yml`.
   - **Application Servers (`INPUT` chain):** Allows incoming OTLP traffic on ports `4317` and `4318` from localhost and allow-listed fleet servers (`otel_collector_effective_allowed_hosts`).
   - **Observability Host (`DOCKER-USER` & `INPUT` chains):** On the designated observability host (`otel_collector_observability_host`), container-forwarded push traffic for Tempo (`4327`) and Prometheus (`9090`) is allowed from the fleet (`otel_collector_effective_allowed_hosts`).
   - **Baseline Policy Compatibility:** Like `pgbouncer`, this role manages allow-list `ACCEPT` rules without imposing terminal `DROP` rules, staying compatible with the host's existing baseline firewall policy.
   - **Persistence:** Any iptables changes trigger the `Save iptables rules` handler (`netfilter-persistent save`).

## Variable Model & Parameters

Collector defaults live in `roles/otel-collector/defaults/main.yml`, with environment overrides in `inventories/production/group_vars/app_servers.yml`.

### Collector Endpoints & Observability Host

`otel_collector_tempo_endpoint`
: Target Tempo OTLP gRPC push endpoint on the Observability Host (e.g. `65.21.236.56:4327`).

`otel_collector_prometheus_remote_write_endpoint`
: Target Prometheus Remote Write HTTP endpoint on the Observability Host (e.g. `http://65.21.236.56:9090/api/v1/write`).

`otel_collector_observability_host`
: Inventory hostname of the server running Tempo and Prometheus (e.g. `staging1`). Set to empty string `""` to disable observability host push rules.

### Firewall & iptables Parameters

`otel_collector_firewall_enabled`
: Whether to manage iptables allow rules (default: `true`).

`otel_collector_otlp_ingress_ports`
: OTLP ingress ports allowed on application servers (default: `[4317, 4318]`).

`otel_collector_allowed_hosts_common`
: Shared source IP or CIDR list allowed to connect to OTLP ports and push to the Observability Host.

`otel_collector_allowed_hosts_extra`
: Group- or host-specific source IP or CIDR list appended to the common list (default: `[]`).

`otel_collector_observability_ingress_ports`
: Ingress ports allow-listed on the Observability Host for the collector fleet (default: `[4327, 9090]`).

### Network & Container Resources

`otel_collector_version`
: Version tag for `otel/opentelemetry-collector-contrib` image (default: `"0.153.0"`).

`otel_collector_container_name`
: Docker container name (default: `"tp-otel-collector"`).

`otel_collector_network_mode`
: Docker network mode (default: `"host"`).

`otel_collector_container_cpu_limit` / `otel_collector_container_memory_limit`
: Docker container resource constraints (default: `0.5` CPU, `"512m"` memory).

`otel_collector_gomemlimit` / `otel_collector_gogc`
: Go runtime memory parameters (default: `"380MiB"`, `"80"`).

`otel_collector_memory_limit_mib` / `otel_collector_memory_spike_limit_mib`
: Collector memory limiter processor thresholds (default: `400` MiB hard limit, `80` MiB spike buffer).

`otel_collector_batch_timeout` / `otel_collector_batch_send_size` / `otel_collector_batch_max_size`
: Batch processor flush parameters (default: `"500ms"`, `256`, `512`).

`otel_collector_internal_metrics_bind` / `otel_collector_internal_metrics_port`
: Address and port for internal Prometheus metrics scrape (default: `"0.0.0.0"`, `8888`).

## Playbook Usage

Run commands from the `ansible/` directory.

Pass `-k` (`--ask-pass`) for SSH password prompt and `-K` (`--ask-become-pass`) for sudo password prompt when running against hosts that require password authentication.

### Full Deployment (Install + Configure + Firewall)

```bash
ansible-playbook playbooks/otel-collector/site.yml -k -K
```

### Configuration & Firewall Update Only

Re-renders collector configuration, restarts container if config changed, updates iptables allow rules, and validates endpoints:

```bash
ansible-playbook playbooks/otel-collector/configure.yml -k -K
```

### Target a Single Application Server

When targeting a specific application server, always include `staging1` in the `--limit` pattern so that `staging1`'s firewall opens ingress for Tempo (`4327`) and Prometheus (`9090`) to accept the application server's incoming telemetry:

```bash
ansible-playbook playbooks/otel-collector/configure.yml --limit "staging1,bpala_classes_app1" -k -K
```

For dry-run previews:

```bash
ansible-playbook playbooks/otel-collector/configure.yml --limit "staging1,bpala_classes_app1" -k -K --check --diff
```

### Add a New Application Server

1. Add the server entry under `app_servers` in `inventories/production/hosts.yml` with its `ansible_host`.
2. Add the server's `IP/32` to `otel_collector_allowed_hosts_common` (or `otel_collector_allowed_hosts_extra`) in `inventories/production/group_vars/app_servers.yml`.
3. Run `site.yml` targeting both `staging1` and the new server to install Docker, deploy the collector, and allow-list the new server across both OTLP and Observability push ports:
   ```bash
   ansible-playbook playbooks/otel-collector/site.yml --limit "staging1,<new_server>" -k -K
   ```
   Or roll out across the entire fleet:
   ```bash
   ansible-playbook playbooks/otel-collector/site.yml -k -K
   ```
