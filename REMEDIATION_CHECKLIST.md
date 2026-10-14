# tp_trace Remediation & Quality Checklist

Tracking document for resolving bugs, security issues, protocol deviations, infrastructure configuration defects, and code quality enhancements in the `tp_trace` repository.

---

## 📊 Summary & Progress Tracker

| Category | Total | Pending | Ignored / Design | Completed |
| :--- | :---: | :---: | :---: | :---: |
| [Priority 0: Critical Bugs & Security](#priority-0-critical-bugs-security--data-integrity) | 5 | 0 | 2 | 3 |
| [Priority 1: Semantic Conventions & Concurrency](#priority-1-semantic-conventions-cardinality--concurrency) | 8 | 0 | 1 | 7 |
| [Priority 2: Dashboards & Helper Scripts](#priority-2-dashboard-navigation-links--scripts) | 5 | 0 | 0 | 5 |
| [Priority 3: Architecture & Code Quality](#priority-3-code-architecture-hygiene--testing) | 6 | 0 | 1 | 5 |
| **Total** | **24** | **0** | **4** | **20** |

---

## Priority 0: Critical Bugs, Security & Data Integrity

- [x] **P0-1: Redact sensitive credentials in Redis command formatting**
  - **File**: [`src/tp_trace/integrations/redis/integration.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/integrations/redis/integration.py)
  - **Status**: Completed (`b74d7cc`)
  - **Resolution**: Redacts sensitive arguments in `AUTH`, `CONFIG SET requirepass/masterauth`, `HELLO`, `MIGRATE`, and `ACL SETUSER` with unit tests in `tests/test_redis.py`.

- [x] **P0-2: Fix silent `TypeError` on `BoundedAttributes` in `SafeSpanExporter`**
  - **File**: [`src/tp_trace/exporter.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/exporter.py)
  - **Status**: Completed (`a0f8713`)
  - **Resolution**: Targets `span._attributes._dict` to normalize ended `ReadableSpan` objects safely without throwing `TypeError`.

- [x] **P0-3: Fix ignored `TP_TRACE_QUERY_MODE` on Django server spans**
  - **File**: [`src/tp_trace/integrations/django/request.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/integrations/django/request.py)
  - **Status**: Completed (`ce21d53`)
  - **Resolution**: Passes `mode=query_mode` into `sanitize_url(raw_url, mode=...)` on server spans, validated with unit test in `tests/test_django.py`.

- [~] **P0-4: Replace hardcoded external IP address (`65.21.236.56`) and local domain (`tempo.tracenest.orb.local`)**
  - **Status**: Ignored (per user instruction — production environment does not use this setup).

- [~] **P0-5: Fix datasource UID mismatch in Grafana provisioning (`datasources.yaml`)**
  - **Status**: Ignored (per user instruction — production environment does not use this setup).

---

## Priority 1: Semantic Conventions, Cardinality & Concurrency

- [~] **P1-1: Align server span `http.route` with OpenTelemetry Semantic Conventions**
  - **Status**: Preserved by Design.
  - **Resolution**: In `tp_trace`, `http.route` intentionally preserves the full request URL for rich Tempo flamegraphs while `http.route_template` holds the parameterized template. The collector's `transform/spanmetrics_route` OTTL processor replaces `http.route` with `http.route_template` before generating metric dimensions, preventing cardinality explosion in Prometheus without losing trace fidelity.

- [x] **P1-2: Model Redis with the OpenTelemetry database semantic conventions (no `cache.*` namespace)**
  - **Files**:
    - [`src/tp_trace/integrations/redis/integration.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/integrations/redis/integration.py)
    - [`src/tp_trace/integrations/django/cache.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/integrations/django/cache.py)
  - **Status**: Completed (`b74d7cc`)
  - **Resolution**: Emits OTel standard `db.system`/`db.system.name="redis"`, `db.operation`/`db.operation.name`, and `db.statement`/`db.query.text`. Removed non-standard `cache.*` attributes.

- [x] **P1-3a: Set `normalized.service="requests"` and `normalized.operation` on outgoing HTTP calls**
  - **File**: [`src/tp_trace/integrations/requests/client.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/integrations/requests/client.py)
  - **Status**: Completed (`a145ffb`)
  - **Resolution**: Sets `normalized.service = "requests"` and `normalized.operation = f"HTTP {method}"` on client spans.

- [x] **P1-3b: Fix mutable default in `ContextVar("tp_trace.tags")`**
  - **File**: [`src/tp_trace/route_context.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/route_context.py)
  - **Status**: Completed (`a145ffb`)
  - **Resolution**: Initialized with `default=None` and allocates fresh dictionary on demand.

- [x] **P1-4: Add token return and reset in `set_request_tag` and `reset_request_tags`**
  - **File**: [`src/tp_trace/route_context.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/route_context.py)
  - **Status**: Completed (`a145ffb`)
  - **Resolution**: Returns `Token` from `set_request_tag` and resets via token in `reset_request_tags` for scoped nested concurrency.

- [x] **P1-5: Prevent `_with_request_route` from injecting `http.route` into database/cache spans**
  - **File**: [`src/tp_trace/tracing.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/tracing.py)
  - **Status**: Completed (`a145ffb`)
  - **Resolution**: Guards against injecting `http.route` onto `CLIENT`, `INTERNAL`, or `db.system` spans.

- [x] **P1-6: Fix memory address collision bug in `_CALLBACK_ARITY_CACHE`**
  - **File**: [`src/tp_trace/integrations/django/request.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/integrations/django/request.py)
  - **Status**: Completed (`a145ffb`)
  - **Resolution**: Memoized using `functools.lru_cache(maxsize=256)` on callback callable.

- [x] **P1-7: Prevent dropping of `cluster_name` on OTel Resource creation**
  - **File**: [`src/tp_trace/__init__.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/__init__.py)
  - **Status**: Completed (`a145ffb`)
  - **Resolution**: Drops `None` attributes so OpenTelemetry Resource creation never warns or drops attributes.

---

## Priority 2: Dashboard Navigation, Links & Scripts

- [x] **P2-1: Fix Requests dashboard link in Service Catalog**
  - **File**: [`docker/grafana/dashboards/service_catalog.json`](file:///Users/bala/workspace/datadog-replacement/tp_trace/docker/grafana/dashboards/service_catalog.json)
  - **Status**: Completed (`a80528a`)
  - **Resolution**: Corrected PromQL `label_replace` expression to set `var-service=requests&` for Requests.

- [x] **P2-2: Add missing `var-operation` parameter to alert cards**
  - **File**: [`docker/grafana/dashboards/service_catalog.json`](file:///Users/bala/workspace/datadog-replacement/tp_trace/docker/grafana/dashboards/service_catalog.json)
  - **Status**: Completed (verified present in handlebars template: `{{#if http_route}}&var-operation={{http_method}}%20{{http_route}}{{/if}}`).

- [x] **P2-3: Fix broken dashboard UIDs in `loadtest_s3.py`**
  - **File**: [`loadtest_s3.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/loadtest_s3.py)
  - **Status**: Completed (`a80528a`)
  - **Resolution**: Updated URLs to `/d/generic-service-overview` and `/d/generic-operation-details`.

- [x] **P2-4: Fix service filter name in `query_slave2.sh`**
  - **File**: [`query_slave2.sh`](file:///Users/bala/workspace/datadog-replacement/tp_trace/query_slave2.sh)
  - **Status**: Completed (`a80528a`)
  - **Resolution**: Changed query parameter from `var-service=postgresql` to `var-service=postgres`.

- [x] **P2-5: Update banner title in `generate_all_traffic.sh`**
  - **File**: [`generate_all_traffic.sh`](file:///Users/bala/workspace/datadog-replacement/tp_trace/generate_all_traffic.sh)
  - **Status**: Completed (`a80528a`)
  - **Resolution**: Updated banner to `TP_TRACE APM — UNIVERSAL MULTI-APP TRAFFIC`.

---

## Priority 3: Code Architecture, Hygiene & Testing

- [x] **P3-1: Remove unused `opentelemetry-instrumentation-django` from dependencies**
  - **File**: [`pyproject.toml`](file:///Users/bala/workspace/datadog-replacement/tp_trace/pyproject.toml)
  - **Status**: Completed (`006274f`)
  - **Resolution**: Removed unused `opentelemetry-instrumentation-django>=0.44b0`.

- [x] **P3-2: Prevent instance dict pollution in CBV method monkeypatching**
  - **File**: [`src/tp_trace/integrations/django/view.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/integrations/django/view.py)
  - **Status**: Completed (`006274f`)
  - **Resolution**: Checks `__dict__` membership prior to patching and calls `delattr` in `finally` if not originally on the instance.

- [x] **P3-3: Fix module docstring placement in `middleware.py`**
  - **File**: [`src/tp_trace/integrations/django/middleware.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/src/tp_trace/integrations/django/middleware.py)
  - **Status**: Completed (`006274f`)
  - **Resolution**: Moved docstring to line 1 of the module.

- [x] **P3-4: Consolidate duplicate config seams across integration modules**
  - **Files**: `request.py`, `view.py`, `template.py`, `middleware.py`, `cursor.py`
  - **Status**: Completed (`006274f`)
  - **Resolution**: Standardized on `from tp_trace.config_access import get_config as _get_config, set_config`.

- [~] **P3-5: Restore commented-out services in `docker-compose.yml`**
  - **Status**: Ignored (per user instruction — local setup only runs Grafana pointing to remote services).

- [x] **P3-6: Add real span exporter integration tests**
  - **File**: [`tests/test_init.py`](file:///Users/bala/workspace/datadog-replacement/tp_trace/tests/test_init.py)
  - **Status**: Completed (`a0f8713`)
  - **Resolution**: Added `test_safe_span_exporter_normalizes_real_readable_spans` verifying real OTel `TracerProvider` and `ReadableSpan` flow through `SafeSpanExporter`.
