# Design decisions and trade-offs

The stack uses OpenTelemetry and OTLP so application instrumentation is not tied to a single observability vendor. Tempo provides trace storage, Prometheus provides metrics, and Grafana provides the investigation interface.

The project favours traces as the detailed diagnostic signal. Metrics provide the fast overview: error rate, throughput, and latency identify where to look; a trace explains the work performed by one request.

Trace sampling happens in the SDK before export to reduce trace storage and transport cost. To prevent trace sampling from distorting golden signals, root API request RED metrics (throughput, error rate, duration) are recorded natively by the SDK for 100% of requests. Downstream dependency metrics (database queries, Redis calls, Celery tasks) are derived from exported spans by the Collector to minimize application runtime overhead.

SQL is sanitised to avoid exporting literal values. Redis command and endpoint data are exposed by default because the team owns the infrastructure; masking options are available for deployments with different privacy boundaries.
