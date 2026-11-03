# Integration and Deployment

This guide covers how to integrate `tp_trace` into your application and connect it to the observability pipeline.

---

## 1. Application SDK Setup

`tp_trace` supports Python 3.8+ and Django 4.2+.

### Installation

```bash
pip install tp_trace
```

### Environment configuration

Configure SDK behavior using environment variables:

```bash
# Required: Application identity
TP_TRACE_PROJECT_NAME=my_app
TP_TRACE_CLUSTER_NAME=cluster1

# Destination: Base HTTP URL of the local OpenTelemetry Collector
TP_TRACE_ENDPOINT=http://127.0.0.1:4318

# Trace sampling: Ratio of root traces to retain (0.0 = 0%, 1.0 = 100%)
TP_TRACE_SAMPLE_RATE=0.1
```

### Process Initialization

Initialize `tp_trace` once during Django startup in `settings.py`:

```python
# settings.py
import tp_trace

tp_trace.init()
```

This single initialization instruments Django web requests and automatically covers Celery workers that import Django settings.

### Supported Integrations

The SDK automatically detects and instruments installed libraries during `init()`:
- **Django**: View dispatch, middleware, and route templates.
- **PostgreSQL (`psycopg2` / `psycopg3`)**: SQL queries with sanitized parameters.
- **Redis**: Cache lookups and command execution times.
- **HTTP Client (`requests`)**: Outbound HTTP calls with W3C trace header injection.
- **Celery**: Task dispatch headers and worker execution tracing.

---

## 2. Trace Storage & Observability Infrastructure

In production, `tp_trace` sends traces to **Tempo** and metrics to **Prometheus**, which are queried by **Grafana**:
- **Tempo**: Can be deployed using the standalone Compose setup in `setup/` (see below).
- **Prometheus & Grafana**: `setup/docker-compose.yml` deploys only Tempo. In a production environment, Prometheus (accepting Remote Write) and Grafana must be provided separately by your infrastructure, or you can run the full all-in-one local stack from [`dev/`](../dev/README.md) as described in [Local Setup](../README.md#local-setup).

### Run Standalone Tempo with Docker Compose

From the repository's `setup/` directory on your observability host:

```bash
cd setup
docker compose up -d
```

- **Ingress Port**: `4327` (mapped to container port `4317` for OTLP/gRPC trace ingestion from Collectors).
- **Query Port**: `3200` (used by Grafana as the Tempo datasource HTTP URL).
- **Readiness Check**:
  ```bash
  curl -f http://localhost:3200/ready
  ```

---

## 3. Collector Deployment with Ansible

The OpenTelemetry Collector runs as a local sidecar on each application server, receiving spans locally over `127.0.0.1:4318` and forwarding them across the network to Tempo and Prometheus.

### Deploy the Collector Fleet

Playbooks in `ansible/` install Docker, start the Collector container, and configure firewall rules:

```bash
cd ansible
# Full install and deployment across the fleet:
ansible-playbook playbooks/otel-collector/site.yml -k -K

# Or update collector configuration only:
ansible-playbook playbooks/otel-collector/configure.yml -k -K
```

For fleet inventory setup, variable overrides, and firewall details, see [ansible/README.md](../ansible/README.md).

---

## 4. Verification

1. **Send a request** to your application:
   ```bash
   curl -i http://localhost:8002/api/
   ```

2. **Verify in Grafana**:
   - Open Grafana (`http://localhost:3001` locally).
   - In **Dashboards**, select your project name to inspect request rates, error rates, and response latency.
   - In **Explore > Tempo**, query by trace ID or service name to view the visual span waterfall.
