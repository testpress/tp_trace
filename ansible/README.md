# Deploy the OpenTelemetry Collector

Run these commands from the `ansible/` directory. The default inventory is
`inventories/production/hosts.yml`.

## Requirements

On the Debian/Ubuntu machine running Ansible:

```bash
sudo apt update
sudo apt install -y ansible sshpass
```

Check `ansible --version`: the playbook requires Ansible Core 2.13 or later.
The full `ansible` package includes the required `community.docker` collection.
`sshpass` is used for SSH password authentication (`-k`).

Target server prerequisites: SSH (`openssh-server`), `sudo`, `python3`,
`python3-apt`, and `debconf-utils` (when firewall management is enabled).

`site.yml` installs Docker if missing, `python3-docker`, and
`iptables-persistent` when firewall management is enabled. The configured Docker
users (default: `ubuntu`) must already exist.

Add `-k -K` to the commands below if SSH and sudo require passwords.

## Add a server

1. Add its name and `ansible_host` IP under the appropriate group in
   `inventories/production/hosts.yml`.
2. Add its `IP/32` to `otel_collector_allowed_hosts_common` in
   `inventories/production/group_vars/app_servers.yml`.
3. Deploy to **both the new server and `staging1`**:

   ```bash
   ansible-playbook playbooks/otel-collector/site.yml --limit 'new_server,staging1'
   ```

Including `staging1` adds the new server's firewall access to Prometheus (9090)
and Tempo (4327). Limiting the run to the new server alone does not update
staging's firewall. Existing firewall rules are preserved.

For example, deploy the TPStreams group with:

```bash
ansible-playbook playbooks/otel-collector/site.yml --limit 'tpstreams,staging1'
```

The playbook deploys the Collector. Install and initialize `tp_trace` in the
application separately to send telemetry to it.

## Update an existing server

To apply Collector configuration changes without running package or firewall tasks:

```bash
ansible-playbook playbooks/otel-collector/configure.yml --limit 'server_name'
```

Add `--check --diff` to preview changes. To run a full deployment on every
inventory server:

```bash
ansible-playbook playbooks/otel-collector/site.yml
```

## Cluster structure

Groups and servers in `inventories/production/hosts.yml`:

```text
app_servers
├── testpress
│   ├── cluster1
│   │   ├── cluster1_app1
│   │   ├── cluster1_app2
│   │   └── cluster1_app3
│   ├── cluster2
│   │   ├── cluster2_app1
│   │   └── cluster2_app2
│   ├── bpala_classes
│   │   └── bpala_classes_app1
│   ├── testpress_staging
│   │   └── staging1
│   └── celery
└── tpstreams
    └── tpstreams_staging
```

Use a group or server name with `--limit`. Include `staging1` when adding servers.

## Configuration files

| File | Purpose |
|---|---|
| `inventories/production/hosts.yml` | Server addresses and groups: `cluster1`, `cluster2`, `bpala_classes`, `testpress_staging`, `tpstreams` |
| `inventories/production/group_vars/app_servers.yml` | Shared endpoints and firewall allowlist |
| `inventories/production/group_vars/<group>.yml` | Group overrides |
| `roles/otel-collector/defaults/main.yml` | Container, port, and resource defaults |
| `../config/otel-collector.yaml` | Shared Collector pipelines and tuning defaults |
