# Design decisions and trade-offs

The stack uses OpenTelemetry and OTLP so application instrumentation is not tied to a single observability vendor. Tempo provides trace storage, Prometheus provides metrics, and Grafana provides the investigation interface.

The project favours traces as the detailed diagnostic signal. Metrics provide the fast overview: error rate, throughput, and latency identify where to look; a trace explains the work performed by one request.

Sampling happens in the SDK before export. It reduces storage and transport cost, but metrics derived from sampled traces are less precise. Critical routes can use endpoint sampling rules to remain fully sampled.

SQL is sanitised to avoid exporting literal values. Redis command and endpoint data are exposed by default because the team owns the infrastructure; masking options are available for deployments with different privacy boundaries.
