# tp_trace

`tp_trace` is a production Python APM library built on OpenTelemetry. It provides distributed tracing and native RED (Rate, Errors, Duration) metrics instrumentation for Django, PostgreSQL, Redis, Celery, and HTTP clients, paired with a complete local development observability stack.

---

## Repository Structure

```text
tp_trace/
├── src/tp_trace/       # Core Python SDK and framework integrations
├── config/             # Shared canonical configuration (Collector, Tempo, Prometheus)
├── dev/                # Local development stack (docker-compose.yml and dev configs)
│   └── sample-app/     # Instrumented Django sample application and traffic generator
├── setup/              # Standalone Tempo trace store deployment (Docker Compose)
├── ansible/            # Ansible playbooks and roles for fleet Collector deployment
├── grafana/            # Canonical Grafana dashboards and datasource provisioning
├── docs/               # Core documentation
│   ├── 01-basics.md    # Observability concepts (traces, metrics, context propagation)
│   ├── 02-architecture.md # End-to-end lifecycle from tp_trace.init() to Grafana
│   └── 03-integration.md  # SDK setup, Docker Tempo setup, and Ansible Collector setup
└── tests/              # SDK test suite and e2e validation probes
```

---

## Local Setup

### Prerequisites

- Docker with Docker Compose support
- Python 3.8+ (for running the host SDK; Python 3.10+ required for `make lint`)

### Start the Local Observability Stack

From the repository root:

```bash
# 1. Copy local environment defaults
cp .env.example .env

# 2. Set up host virtual environment and install development tools
make install

# 3. Validate shared configuration files
make check-config
make check-collector-config

# 4. Build images and start the local services
make rebuild
```

> **Note:** Root Makefile commands target `dev/docker-compose.yml` automatically. If you modify application dependencies or Dockerfiles, run `make rebuild`. For everyday restarts without rebuilding images, use `make up`.

---

## How to Work With It

### Service Endpoints

Once `make rebuild` or `make up` completes, local services are accessible at:

| Service | Host URL / Port | Notes |
| --- | --- | --- |
| **Sample Django App** | <http://localhost:8002/api/> | Instrumented Django application |
| **Grafana** | <http://localhost:3001> | Anonymous admin access enabled |
| **Prometheus** | <http://localhost:9090> | TSDB metrics engine |
| **Tempo Query API** | <http://localhost:3200> | Trace store query API |
| **Tempo OTLP/gRPC** | `localhost:4327` | Traces ingress |
| **Collector OTLP** | `localhost:4318` (HTTP) / `4317` (gRPC) | Application telemetry ingress |
| **Collector Health** | <http://localhost:13133/> | Health probe endpoint |
| **MinIO Console** | <http://localhost:9003> | S3-compatible trace object storage |

### Generate and Inspect Telemetry

1. **Send synthetic traffic** to the sample application:
   ```bash
   # Run standard traffic generator workload
   make traffic

   # Or run specific workload flags
   make traffic ARGS='--count 20 --redis --http'
   ```

2. **Run end-to-end smoke verification**:
   ```bash
   make smoke
   ```
   `make smoke` verifies that spans and metrics have reached Prometheus and Tempo storage.

3. **View Dashboards in Grafana**:
   - Open <http://localhost:3001>.
   - Navigate to **Dashboards > TP Trace Dashboards**.
   - Filter by Project: `otel-sample`, Cluster: `Dummy`.
   - Inspect request rates, error ratios, latency distributions, and trace waterfalls.

### Managing the Stack

```bash
# Check container status
make ps

# View service logs
make logs SERVICE=sample-app
make logs SERVICE=otel-collector

# Restart a specific service (e.g. after changing collector config)
make restart SERVICE=otel-collector

# Stop services (preserves database and trace volumes)
make down

# Complete teardown (removes all volumes and persistent data)
make nuke
```

---

## Shared Telemetry Configuration (`config/`)

To prevent drift between development and production, canonical service configurations reside in `config/`:

| File | Shared Usage |
| --- | --- |
| `config/otel-collector.yaml` | Mounted by local Compose; copied by Ansible to production application servers |
| `config/tempo.yaml` | Mounted by local Compose; mounted by standalone Tempo in `setup/` |
| `config/prometheus.yml` | Mounted by local Compose; reference scrape configuration for production Prometheus |

The Collector uses `${env:VAR_NAME}` substitutions for endpoints and identities. Local Compose provides environment variables through `.env` and `dev/docker-compose.yml`, while Ansible injects inventory variables during fleet deployment.

To validate configuration syntax across all environments:

```bash
make check-config
make check-collector-config
docker compose -f setup/docker-compose.yml config --quiet
```

---

## Documentation

### Working on tp_trace

1. [Observability Basics](docs/01-basics.md) introduces the telemetry concepts
   used throughout the codebase.
2. [Architecture](docs/02-architecture.md) follows telemetry from the SDK to
   Grafana and points to the code and configuration at each stage.

### Using tp_trace in an application

1. [Integrate tp_trace into a Django app](docs/03-integration.md) to configure
   the SDK and verify that your application emits telemetry.
2. [Investigate application services in Grafana](grafana/dashboards/README.md)
   to diagnose slow requests, error spikes, and missing telemetry.

For observability infrastructure, see the [Collector deployment guide](ansible/README.md)
and [standalone Tempo setup](setup/README.md).
