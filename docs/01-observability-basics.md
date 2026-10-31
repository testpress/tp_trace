# Observability Basics

## Observability

Observability is the ability to understand a system's behavior from the signals it emits. Monitoring checks known conditions, such as an error-rate threshold. Investigating an unexpected slowdown often requires connecting measurements, events, and individual requests.

## Traces and Spans

A trace describes an operation as it travels through a system. A span represents one timed unit of work, such as an incoming request, database query, or outgoing HTTP call. Spans carry a trace ID, span ID, timing, attributes, and possibly a parent span ID.

Parent-child relationships explain which work caused other work. Overlapping spans can represent concurrency, so adding every span's duration does not necessarily give the request duration. An exported trace may be incomplete if propagation, sampling, or delivery loses part of it. See [OpenTelemetry traces](https://opentelemetry.io/docs/concepts/signals/traces/).

## Metrics

Metrics summarize numerical measurements over time. Counters represent accumulated events; gauges represent current values; histograms describe distributions through buckets, counts, and sums. Rate, errors, and duration (RED) describe workload behavior. A percentile estimated from histogram buckets is an approximation, rather than an exact measurement of one request.

## Logs

Logs describe individual events, usually with a timestamp, severity, and message or structured fields. Trace and span IDs can connect a log event to a request. A log store and a trace store serve different query needs; collecting one does not automatically collect the other.

## Context Propagation

context propagation carries identifiers between operations so spans can join the same trace. within a process, context follows execution. across http or messaging boundaries, instrumentation injects and extracts context in headers. w3c trace context uses `traceparent` and `tracestate`. baggage carries additional key-value context and needs explicit consideration of trust and data exposure.

## OpenTelemetry

OpenTelemetry provides APIs, SDKs, instrumentation, and protocols for producing and transporting telemetry. Instrumentation observes application operations; an SDK records and exports the resulting signals. OTLP is its telemetry transport protocol. OpenTelemetry itself is not a database or dashboard. See [OpenTelemetry concepts](https://opentelemetry.io/docs/concepts/).

## OpenTelemetry Collector

The Collector is a separate service that receives telemetry, processes it, and exports it to other services. A pipeline connects receivers, processors, and exporters for a particular signal. Batching can reduce network overhead; memory limits and bounded queues constrain resource use. Queues and retries do not guarantee delivery: outages can outlast their limits. See [Collector documentation](https://opentelemetry.io/docs/collector/).

## Time Series Databases

A time series database stores timestamped measurements. In Prometheus, a metric name and its complete label set identify a series. Changing a label value creates a different series. Queries can calculate rates and aggregate series across labels. Prometheus stores metrics; it does not store request span trees. See [the Prometheus data model](https://prometheus.io/docs/concepts/data_model/).

## Cardinality

Cardinality is the number of distinct values or combinations in a dataset. For metrics, each unique label combination creates a series. Ten routes across three methods and two outcomes could create 60 combinations before adding instance labels or histogram buckets.

User IDs, request IDs, raw URLs, and SQL text can produce continuously growing label sets. Route templates and bounded operation names are usually more manageable. Higher series counts increase recording, transport, storage, and query costs. A series limit protects resources by sacrificing detail, not by making unlimited dimensions free.

## Grafana Tempo

Tempo stores and queries distributed traces. Trace IDs support lookup; TraceQL supports searching trace data. Grafana provides the interface for exploring traces and correlating them with metrics. Tempo is a trace backend, while Grafana is the visualization layer. See [Tempo introduction](https://grafana.com/docs/tempo/latest/introduction/).

## Sampling

Sampling selects which traces to retain or export. Head sampling decides near the start of a trace; tail sampling decides after observing spans. Counts calculated only from sampled traces describe the retained population unless explicitly corrected. Separately recorded metrics can preserve workload totals, subject to recording and delivery failures.
