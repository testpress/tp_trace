# How tracing moves through the stack

`tp_trace` observes work inside the application process. It creates spans as a Django request passes through application code and supported dependencies.

The SDK sends those spans to the OpenTelemetry Collector using OTLP. The Collector and Prometheus are operated through the Ansible deployment. Prometheus receives metrics through the platform push method rather than scraping each application process.

Tempo stores complete traces. Grafana combines Prometheus metrics with Tempo traces, so a developer can move from a slow or failing metric to the request waterfall that caused it.

```text
Django application
  └─ tp_trace spans
       └─ Collector
            ├─ Prometheus metrics
            └─ Tempo traces
                 └─ Grafana investigation
```

This separation keeps application instrumentation small while centralising telemetry routing, storage, and access control.
