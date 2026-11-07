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
export ENABLE_TP_TRACE=True
export TP_TRACE_PROJECT_NAME=my_app
export TP_TRACE_CLUSTER_NAME=cluster1

# Destination: Base HTTP URL of the local OpenTelemetry Collector
export TP_TRACE_ENDPOINT=http://127.0.0.1:4318

# Default head sampling ratio for root traces
export TP_TRACE_SAMPLE_RATE=0.1
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
- **PostgreSQL**: Django database queries with either psycopg2 or psycopg3, and direct psycopg2 calls. SQL query literals are sanitized.
- **Redis**: Cache lookups and command execution times.
- **HTTP Client (`requests`)**: Outbound HTTP calls with W3C trace header injection.
- **Celery**: Task dispatch headers and worker execution tracing.

---

For a local Collector and Grafana instance, follow the
[local stack setup](../README.md#local-setup).

## Verify application telemetry

1. **Send a request** to an instrumented endpoint in your app. For the local sample app, run:
   ```bash
   curl -i http://localhost:8002/api/
   ```

2. **Verify in Grafana**:
   - Open Grafana (`http://localhost:3001` locally).
   - Open **Service Catalog**, select a time range that includes the request,
     and select your service to inspect request rates, errors, and latency.
   - In **Explore > Tempo**, query by trace ID or service name to view the visual span waterfall.

See the [Grafana dashboard guide](../grafana/dashboards/README.md) for investigation steps.
