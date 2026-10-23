# tp_trace

`tp_trace` adds OpenTelemetry tracing to Django applications. It creates a request trace and, when the relevant libraries are installed, records Django, PostgreSQL, Redis, `requests`, and Boto3 work beneath it.

The SDK exports OTLP traces to the team's telemetry endpoint. Tempo stores the traces and Grafana is where developers inspect them.

## Start here

- [Run the local development stack](dev/README.md)
- [Verify the Collector fix with local A/B traffic](sample-app/README.md#verify-the-collector-memorycardinality-fix-locally)
- [Tutorial: see your first trace](docs/tutorials/first-trace.md)
- [How-to: configure tp_trace](docs/how-to/configure-tp-trace.md)
- [How-to: investigate a slow or failing request](docs/how-to/investigate-a-request.md)

## Reference

- [Configuration](docs/reference/configuration.md)
- [Supported integrations](docs/reference/integrations.md)
- [Dashboards](docs/reference/dashboards.md)
- [Deployment endpoints](docs/reference/deployment.md)

## Background

- [How tracing moves through the stack](docs/explanation/architecture.md)
- [How instrumentation works](docs/explanation/instrumentation.md)
- [Design decisions and trade-offs](docs/explanation/design-decisions.md)
- [Observability basics](docs/explanation/observability-basics.md)

## Requirements

Python 3.8 or later is required. The Django integration supports Django 4.2 or later. Each optional integration is enabled only when its library is installed.

## Contributing

- [Code conventions](docs/code-conventions.md)
- [Agent instructions and maintenance skills](AGENTS.md)
