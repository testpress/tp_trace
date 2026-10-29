# Design decisions and trade-offs

The stack uses OpenTelemetry and OTLP so application instrumentation is not tied to a single observability vendor. Tempo provides trace storage, Prometheus provides metrics, and Grafana provides the investigation interface.

The project favours traces as the detailed diagnostic signal. Metrics provide the fast overview: error rate, throughput, and latency identify where to look; a trace explains the work performed by one request.

Trace sampling happens in the SDK before export to reduce trace storage and transport cost. To prevent trace sampling from distorting golden signals, RED metrics (throughput, error rate, duration) for both root HTTP requests and downstream dependencies (PostgreSQL, Redis, Celery, outbound HTTP) are recorded natively by the SDK's `RedSpanProcessor` for 100% of operations. Trace ratio rejections return `Decision.RECORD_ONLY` instead of `Decision.DROP`, enabling in-memory span attribute capture and metrics computation before `SampledOnlySpanProcessor` drops unsampled spans from network export. This trades near-zero recording overhead at low sample rates for running SQL/URL sanitization and attribute enrichment across all calls, in addition to the ~1.0–11.4µs processor overhead measured by `test_red_span_processor_overhead_benchmark`. INTERNAL spans are strictly excluded as they represent internal framework waterfall detail.

SQL is sanitised to avoid exporting literal values. Redis command and endpoint data are exposed by default because the team owns the infrastructure; masking options are available for deployments with different privacy boundaries.
