# Observability basics

Observability helps answer why a system behaved in a particular way. Traces, metrics, and logs are complementary signals.

A trace describes one request across its operations. Each span records one unit of work, such as handling a Django view or executing a database query. Parent and child spans show which work belongs to the same request and where time was spent.

Metrics aggregate many requests. They make it practical to notice changes in throughput, error rate, and latency over time. A metric can signal that a route is slow; a trace shows the exact request and downstream work responsible.

OpenTelemetry standardises how applications create and export telemetry. OTLP is the transport format used by this SDK and the Collector.
