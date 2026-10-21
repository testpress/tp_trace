# Changelog

All notable changes to `tp_trace` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.5] - 2026-10-21

### Performance
- Changed the default SDK metrics export interval from 5 seconds to 60 seconds. `TP_TRACE_METRICS_EXPORT_INTERVAL_MILLIS` and the explicit configuration argument can override it.
- Reduced the HTTP duration histogram from 11 to 8 explicit boundaries (12 to 9 bucket series including `+Inf`).
- Optimized Grafana queries to focus on active telemetry and avoid duplicate request counts.

### Changed
- Improved operation filtering and latency visualizations in the Grafana dashboards.
- Reorganized the documentation around setup, deployment, and investigation workflows.

---

## [0.1.4] - 2026-10-15

### Fixed
- **Django AppRegistry & Early Import Safety**:
  - Deferred patching of `django.contrib.auth` and third-party cache backends (`django_redis.cache.RedisCache`, `django_redis.client.default.DefaultClient`) using `when_imported()`.
  - Deferred Django REST Framework (`rest_framework.views`) view patching if not already loaded in `sys.modules`.
  - Eliminates `AppRegistryNotReady` errors when instrumenting Django before `django.setup()` or during ASGI/WSGI module bootstrapping.
- **Unintended Database Queries on Public Endpoints**:
  - Guarded user attribute extraction in `_set_user()` by verifying `hasattr(request, "_cached_user")` before inspecting `request.user.is_authenticated`.
  - Prevents evaluating `SimpleLazyObject` on unauthenticated/anonymous requests, preventing redundant `SELECT ... FROM auth_user` SQL queries on public endpoints.
- **Thread-Safe Middleware Instrumentation**:
  - Replaced mutable monkeypatching of Django's `load_middleware` with a one-time patch on `django.core.handlers.base.import_string`.
  - Protected middleware wrapping with an execution-gated `ContextVar` (`_active_middleware_integration`), eliminating race conditions during concurrent worker thread initialization.
- **Empty-State Dashboard Resilience**:
  - Added `or vector(0)` fallbacks to Prometheus expressions across Service Catalog, Service Overview, and Operation Details Grafana dashboards, preventing NaN/broken visualization when metric streams are idle.

### Performance
- **Redis Memory Allocation Bounds**:
  - Added argument-level string length truncation in `_stringify_cmd_arg` before string decoding and command concatenation.
  - Bounds memory allocation when logging large cached objects or multi-megabyte payloads in Redis commands (`SET`, `MSET`).

### Added
- **AWS Integration Test Coverage**:
  - Added `botocore` to development test dependencies and extended automated regression tests covering Boto3/S3 client instrumentation.

---

## [0.1.3] - 2026-10-14

### Fixed
- **Django Response Serialization & Cache Pickling**:
  - Removed unpicklable OpenTelemetry `Span` references (`request._tp_span` and `response._tp_parent_span`) containing thread locks (`_thread.RLock`).
  - Stored response parent spans out-of-band in a `weakref.WeakKeyDictionary` (`_RESPONSE_PARENT_SPANS`).
  - Resolves `TypeError: cannot pickle '_thread.RLock' object` when caching HTTP responses with Django's `cache_page`, `UpdateCacheMiddleware`, and `django_redis`.
- **PostgreSQL Connection Access Guard**:
  - Guarded database connection wrapping behind `django.apps.apps.ready`.
  - Filtered connections using `connections.all(initialized_only=True)` to prevent premature connection instantiation and dummy settings assignment before Django finishes initializing.

---

## [0.1.2] - 2026-10-14

### Fixed
- **Tempo TraceQL Query Timeout**:
  - Replaced unindexed regex matching (`span.normalized.service =~ ...`) and disjunctive OR conditions (`kind = server || kind = client`) with indexed exact matches (`span.normalized.service = "${service:raw}"`) in the Operation Details dashboard waterfall panel.
  - Reduced query execution latency from >50s timeouts (`context deadline exceeded`) down to under 0.5s for 3-hour to 24-hour search windows.
- **Datasource & Data Proxy Timeout**:
  - Configured `timeout: 120` in Grafana Tempo datasource provisioning (`datasources.yaml`).
  - Added `GF_DATAPROXY_TIMEOUT=120` to Grafana container environment in `docker-compose.yml`.
- **Collector Metric Cardinality & Deduplication**:
  - Excluded intrinsic `span.name` from the OpenTelemetry Collector's `spanmetrics` connector (`exclude_dimensions: [span.name]`), eliminating duplicate `span_name` metric series that mirrored `normalized_operation`.
  - Guarded `normalized.operation` OTTL transformation to preserve existing SDK-normalized operations from being overwritten.
- **Service Catalog Active Filtering**:
  - Filtered out inactive services with zero throughput (`0 req/s` and `0 Total Calls`) from the Service Catalog dashboard overview table.
- **Table Visual Polish**:
  - Moved the `Status Code` column to the far right in the Operation Details trace table with subtle text coloring instead of high-saturation badge highlights.

---

## [0.1.1] - 2026-10-12

### Added
- **PgBouncer Connection Timing**: Added `🔹 pgbouncer.connect` client-to-PgBouncer connection establishment span wrapping `get_new_connection()`, measuring genuine TCP/SSL handshake and authentication latency.
- **PgBouncer Query Tagging**: Explicitly tagged all database queries routed through PgBouncer with `db.connection.pool: "pgbouncer"`, `net.peer.port: 6432`, and `server.port: 6432`.
- **Query Parameter Visibility in Tempo**:
  - `http.target` in incoming Django requests now retains full query parameters (`/path?query`), allowing direct search and display in Tempo headers.
  - Added `http.target` and `url.query` attributes to outgoing `requests` client spans.
- **Unmasked Redis Commands & Data**:
  - Replaced OpenTelemetry's default argument masking (`COMMAND ? ?`) with full-argument stringification (`_tp_trace_format_command_args`). Commands, keys, and values are exposed unmasked in `db.statement`, `redis.raw_command`, and `redis.command`.
  - Added unmasked Redis statement generation to Django `django-redis` cache spans (`django_redis.cache.*`).
  - Exposed real Redis connection endpoints (`net.peer.name` and `net.peer.port`) by default.
- **Dashboard Command & Statement Table**: Added a dedicated `Command / Statement` column to Service Overview and Operation Details dashboard tables for inspecting SQL and Redis queries.

### Changed
- **Route Normalization & Multi-Tenant Clean Grouping**:
  - Root span name (`span.name`) and `http.route` strictly use canonical low-cardinality route templates (`/api/courses/<pk>/`), ensuring consistent grouping across multiple client subdomains and preventing metric cardinality explosion.
  - Concrete request targets (`/path?query`) and query parameters are captured under `http.target` and `url.query` as span attributes for trace-level inspection.
- **Service Overview & Operation Details Dashboards**:
  - Removed the redundant "Recent Distributed Traces & Flamegraph Waterfalls" panel from the Service Overview dashboard, keeping it focused on service golden signals and the Operations Summary table.
  - Updated the Operation Details dashboard trace table to display "Full URL" (`url.full`), "Target Route (with Query)" (`http.target`), and "Query Params" (`url.query`), providing the exact full URL and query parameters for individual traces upon drilldown.

### Fixed
- **PgBouncer Flamegraph Double-Counting**: Removed synthetic outer `pgbouncer.pool_wait` span that previously wrapped PostgreSQL queries, ensuring exactly one query span per SQL statement and eliminating artificial duration nesting in Tempo flamegraphs.
- **Low-RPS Dashboard Rendering**: Resolved issues where low-throughput services (< 1 RPS / 3 traces) failed to display dots and dynamic axes in Grafana APM charts.
- **DRF Import Order**: Deferred Django REST Framework imports in the Django integration to eliminate startup deadlocks and circular imports.
- **Tempo Port Collision**: Updated Tempo docker host mapping to port `4328` to avoid port conflict with standard OpenTelemetry Collector on `4318`.

---

## [0.1.0] - 2026-10-09

### Added
- **Core SDK & Packaging**: Initial release of `tp_trace` (renamed from `tp_dog`), a high-performance OpenTelemetry Python Observability SDK.
- **Django Integration**:
  - Inbound WSGI request lifecycle tracing (`traced_get_response`).
  - Automatic URL route normalization (`http.route`) and view name resolution (`django.view`).
  - Template rendering spans with nesting configuration (`trace_nested_templates`).
  - Institute multi-tenant tagging (`institute.subdomain`).
  - W3C distributed trace context propagation (`traceparent`, `baggage`).
- **PostgreSQL Integration**:
  - Database query tracing via Django native `execute_wrappers` and DB cursor wrappers.
  - Multi-database support with database instance tagging (`db.name`, `db.instance`).
  - Query operation normalization and SQL statement sanitization.
- **Redis Integration**:
  - Official OpenTelemetry `RedisInstrumentor` integration for `redis-py` and `redis.asyncio`.
  - Re-entrancy guard to suppress downstream driver spans during high-level Django cache operations.
- **HTTP Client Integration (`requests`)**:
  - Outbound HTTP call tracing with `RequestsInstrumentor`.
  - W3C traceparent header injection.
  - Peer service and server address resolution.
- **AWS S3 / Boto3 Integration**:
  - Botocore instrumentation for AWS S3 and cloud storage API calls.
- **Metrics & Sampling**:
  - RED metric generation (calls, error counts, latency histograms) compatible with Prometheus.
  - Ratio-based head sampling and endpoint-specific sample rate rules (`endpoint_sample_rules`).
  - Route ignoring rules (`ignore_endpoints`).
- **Grafana APM Dashboards**:
  - Service Catalog dashboard with Watchdog anomaly detection cards.
  - Needs Attention incident overview.
  - Service Overview, Operation Details, and Service Errors dashboards.
