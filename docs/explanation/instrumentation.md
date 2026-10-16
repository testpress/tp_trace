# How instrumentation works

The SDK instruments supported libraries at runtime. `tp_trace.init()` builds an OpenTelemetry tracer provider, configures export, and applies integrations for libraries that are installed in the application environment.

Each integration wraps a stable library boundary. Django request handling creates the root span; database, cache, HTTP, and AWS calls create child spans. The parent-child structure produces the waterfall shown in Tempo.

Instrumentation is designed to stay out of the application's control flow. Failures while creating or exporting telemetry are absorbed, while application exceptions continue to propagate normally. This means an unavailable Collector can lose telemetry but does not make an application request fail.

The SDK also uses context propagation. A downstream HTTP request receives a W3C `traceparent` header, allowing another instrumented service to continue the same trace.
