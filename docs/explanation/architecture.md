# How tracing moves through the stack

`tp_trace` observes work inside application processes. It creates spans as a Django request passes through application code and supported dependencies, and as background jobs are published and executed across asynchronous process boundaries such as Celery workers.

The SDK sends those spans to the OpenTelemetry Collector using OTLP. The Collector and Prometheus are operated through the Ansible deployment. Prometheus receives metrics through the platform push method (spanmetrics connector) rather than scraping each application process.

Tempo stores complete traces. Grafana combines Prometheus metrics with Tempo traces, so a developer can move from a slow or failing metric to the request waterfall that caused it.

```text
Django web app / Celery worker
  └─ tp_trace spans
       └─ Collector
            ├─ Prometheus metrics (spanmetrics)
            └─ Tempo traces
                 └─ Grafana investigation
```

### Process boundary and single metrics pipeline
Asynchronous task executions across Celery workers cross process and machine boundaries. tp_trace injects W3C tracecontext headers into the task payload upon publication (`celery.apply_async`) and extracts them in the consumer worker (`celery.run`), connecting producer and consumer spans within a unified distributed trace.

All RED metrics (rate, error, duration) for Celery tasks use the same downstream OpenTelemetry Collector pipeline as HTTP, PostgreSQL, and Redis spans. Workers do not establish independent metric instruments or parallel counter registries.

This separation keeps application instrumentation small while centralising telemetry routing, storage, and access control.
