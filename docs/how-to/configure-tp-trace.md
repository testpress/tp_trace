# Configure tp_trace

Configure the SDK at process startup, before Django begins serving requests.

```python
import tp_trace

tp_trace.init(
    project_name="orders-api",
    environment="production",
    endpoint="http://collector.internal:4318",
    sample_rate=1.0,
    integrations={"redis": False},
)
```

Use environment variables when deployment configuration should not live in application code:

```bash
export TP_TRACE_PROJECT_NAME=orders-api
export TP_TRACE_ENVIRONMENT=production
export TP_TRACE_ENDPOINT=http://collector.internal:4318
export TP_TRACE_SAMPLE_RATE=0.25
```

Restart the application process after changing environment variables.

To stop tracing on one instance, set `TP_TRACE_DISABLED=true` and restart the process. To retain a route while sampling other traffic, use `TP_TRACE_ENDPOINT_SAMPLE_RULES`:

```bash
export TP_TRACE_ENDPOINT_SAMPLE_RULES='/api/checkout/*=1.0,/health=0.0'
```

See the [configuration reference](../reference/configuration.md) for all options and defaults.
