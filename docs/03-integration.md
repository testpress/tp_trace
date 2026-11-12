# Integrate tp_trace into a Django app

Use this guide to instrument a Django app and verify its telemetry in Grafana.
You need a reachable OpenTelemetry Collector endpoint and access to the Grafana
instance that receives your application telemetry.

---

## Configure the SDK

`tp_trace` supports Python 3.8+ and Django 4.2+.

### Installation

```bash
pip install tp_trace
```

### Environment configuration

Configure SDK behavior using environment variables:

```bash
# Application identity for grouping telemetry in Grafana
export TP_TRACE_CLUSTER_NAME=cluster1
export TP_TRACE_ENVIRONMENT=production

# Destination: Base HTTP URL of the local OpenTelemetry Collector
export TP_TRACE_OTEL_COLLECTOR_ENDPOINT=http://127.0.0.1:4318

# Default head sampling ratio for root traces
export TP_TRACE_SAMPLE_RATE=0.1
```

### Process Initialization

Initialize `tp_trace` once during Django startup in `settings.py`:

```python
# settings.py
import tp_trace

tp_trace.init(
    project="testpress",
    cluster_name=TP_TRACE_CLUSTER_NAME,
    environment=TP_TRACE_ENVIRONMENT,
    endpoint=TP_TRACE_OTEL_COLLECTOR_ENDPOINT,
    sample_rate=TP_TRACE_SAMPLE_RATE,
    extract_trace_context=False,
)
```

**Parameters:**
- `project`: Application identifier attached as `service.name` to all emitted spans and metrics.
- `cluster_name`: Identifies the server cluster or host pool running the app instance.
- `environment`: Deployment stage (e.g., `production`, `staging`).
- `endpoint`: Base HTTP URL of the OpenTelemetry Collector OTLP receiver.
- `sample_rate`: Head sampling ratio for root traces (e.g., `0.1` samples 10% of traces).
- `extract_trace_context`: Set to `False` so your Django app acts as the start (root) of its own traces instead of adopting trace IDs sent by outside callers.
  - **Why set to `False`?** External callers (browsers, mobile apps, third parties) often send `traceparent` headers with arbitrary IDs or sampling flags. Setting this to `False` ensures your backend enforces its own sampling rules, prevents missing or orphan spans in Grafana/Tempo, and starts a fresh trace for each incoming request.
  - **Does this break Celery?** No. Internal context propagation is preserved, so Celery background tasks dispatched by a web request still link to that request's trace.

This single initialization instruments Django web requests and automatically covers Celery workers that import Django settings.

### Supported Integrations

The SDK automatically detects and instruments installed libraries during `init()`:
- **Django**: View dispatch, middleware, and route templates.
- **PostgreSQL**: Django database queries with either psycopg2 or psycopg3, and direct psycopg2 calls. SQL query literals are sanitized.
- **Redis**: Cache lookups and command execution times.
- **HTTP Client (`requests`)**: Outbound HTTP calls with W3C trace header injection.
- **Celery**: Task dispatch headers and worker execution tracing.

---

## Local Development & Testing

When developing and testing your own application locally, you do not need the entire development stack (which includes sample databases, Redis, RabbitMQ, and the sample app). Instead, start only the core telemetry pipeline:

```bash
make core-up
```

This spins up only the lightweight core services:
- **OpenTelemetry Collector**: Ingests traces and metrics at `http://localhost:4318` (HTTP) and `localhost:4317` (gRPC).
- **Prometheus**: Metrics storage at `http://localhost:9090`.
- **Tempo**: Trace storage and query API at `http://localhost:3200`.

Point your app's `TP_TRACE_OTEL_COLLECTOR_ENDPOINT` to `http://127.0.0.1:4318`, run your Django application locally, and telemetry will immediately flow to the local collector, Prometheus, and Tempo.

> **Note:** If you also want the pre-configured local Grafana instance (`http://localhost:3001`) to view dashboards and trace waterfalls, you can start the full local stack using `make up`.

---

## Verify application telemetry

1. **Send a request** to an instrumented endpoint in your app:
   ```bash
   curl -i http://localhost:8000/your-endpoint/
   ```

2. **Verify in Grafana**:
   - Open Grafana (`http://localhost:3001` locally).
   - Open **Service Catalog**, select a time range that includes the request,
     and select your service to inspect request rates, errors, and latency.
   - In **Explore > Tempo**, query by trace ID or service name to view the visual span waterfall.

See the [Grafana dashboard guide](../grafana/dashboards/README.md) for investigation steps.
