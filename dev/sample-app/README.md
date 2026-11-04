# tp_trace sample Django application

This directory is a comprehensive e-commerce and observability demo Django application used to exercise the `tp_trace` APM SDK integrations and verify OpenTelemetry traces, RED metrics, flamegraphs, and error diagnostics.

To run it with local PostgreSQL instances (Primary + 3 Replicas + PgBouncer), Redis, RabbitMQ, Celery workers, MinIO S3, the Collector, Tempo, Prometheus, and Grafana, follow the [development setup](../../README.md).

## Architecture & Endpoints

The sample application covers multiple real-world services and failure modes:

### 1. Interactive Testing Dashboard
- Web UI: `http://localhost:8002/`
- Real-time tester with endpoint categories, documentation, payload presets, and one-click execution.

### 2. E-Commerce Multi-Tier Domain
- `POST /api/orders/checkout/`: Realistic multi-step checkout workflow with:
  - Database row-level locking (`select_for_update`) and transactions on Primary
  - Replica stock verification on `slave1`
  - Downstream payment gateway HTTP call
  - Cross-database audit logging on `slave2`
  - Cache update in Redis
  - Asynchronous background fulfillment dispatch via Celery
  - Custom OpenTelemetry span attributes (`ecommerce.order_id`, `ecommerce.total_amount`, `customer.tier`)
- `GET /api/orders/` & `GET /api/orders/<id>/`: Order querying with `prefetch_related` and status filters (`?status=completed`).
- `POST /api/orders/<id>/cancel/`: Restocks inventory in transaction, writes audit log on `slave3`, and evicts cache.
- `GET /api/orders/<id>/invoice/`: Generates invoice JSON/PDF and saves/checks S3 MinIO storage.
- `GET /api/customers/` & `GET /api/customers/<id>/stats/`: Customer profiles, tiers (`bronze`, `silver`, `gold`, `platinum`), and lifetime spend calculations.
- `GET /api/categories/` & `GET /api/categories/<id>/products/`: Category hierarchy with cached product lookups.
- `GET /api/products/low-stock/`: Query products below inventory thresholds.
- `GET /api/products/<id>/reviews/` & `POST /api/products/<id>/reviews/`: Product review submissions with rating aggregations.

### 3. Analytics & APM Flamegraphs
- `GET /api/analytics/sales-summary/`: Heavy multi-table join and aggregation over Orders, OrderItems, Products, and Categories.
- `GET /api/analytics/inventory-health/`: Stock valuation and distribution analytics.
- `GET /api/analytics/slow-report/`: Artificial database `pg_sleep` and CPU load generator for slow query detection and flamegraph drilldowns (`?delay=1.5&complexity=high`).

### 4. Advanced Redis & Cache Patterns
- `GET /api/cache/stampede-demo/`: Cache-aside pattern with Redis mutex lock to prevent cache stampedes under high concurrency.
- `POST /api/cache/pipeline/`: Redis pipelining with multi-key batch writes (`mset`, `lpush`, `ltrim`).
- `POST /api/cache/invalidate/`: Pattern-based cache invalidation.

### 5. Celery Asynchronous Workflows
- `POST /api/celery/order-fulfillment/`: Background fulfillment worker with shipping call and audit logging.
- `POST /api/celery/sales-report/`: Background analytical report worker generating CSVs uploaded directly to MinIO S3.
- `POST /api/celery/flaky/`: Flaky task demonstrating Celery retry spans and error tracking.
- `GET /api/celery/status/<task_id>/`: Query background task state and execution results.
- `POST /api/celery/publish/`: Classic demo task (double value / simulated failure).

### 6. Chaos & APM Detection Scenarios
- `GET /api/chaos/handled-exception/`: Caught exception with `span.record_exception()`, returning HTTP 200 with diagnostics.
- `GET /api/chaos/validation-error/`: HTTP 400 Bad Request with field validation errors.
- `GET /api/chaos/unauthorized/`: HTTP 401 Unauthorized or 403 Forbidden with security headers.
- `GET /api/chaos/downstream-timeout/`: Ultra-low timeout downstream call triggering HTTP 504 Gateway Timeout.
- `GET /api/chaos/db-timeout/`: Sets PostgreSQL `statement_timeout` to trigger query timeouts.
- `GET /api/chaos/deadlock-simulation/`: Simulates transaction conflict and rollback (HTTP 409).

### 7. Distributed Databases & Multi-DB Operations
- `GET /api/db/reconciliation/`: Cross-checks inventory and order state across `default`, `slave1`, `slave2`, and `slave3`.
- `POST /api/inventory/batch-rebalance/`: Synchronizes stock levels across all 4 database replicas.
- `POST /api/products/sync-all-dbs/`: Omni-write inserting and updating across all 4 databases in a single request.
- `POST /api/products/multi-db-transfer/`: Primary read -> Slave1 update -> Slave2 audit -> Slave3 sync log.

## Generating Realistic Traffic

Use `dev/sample-app/generate_traffic.sh` to generate diverse traffic:

```bash
# Full multi-tier traffic cycle
bash dev/sample-app/generate_traffic.sh --all --count 100

# High-cardinality bursts with dynamic routes, SQL entropy, multi-tenancy, and errors
bash dev/sample-app/generate_traffic.sh --burst --burst-size 50 --count 200

# Target specific subsystems
bash dev/sample-app/generate_traffic.sh --ecommerce --count 50
bash dev/sample-app/generate_traffic.sh --analytics --count 20
bash dev/sample-app/generate_traffic.sh --chaos --count 20

# Latency and flamegraph testing
bash dev/sample-app/generate_traffic.sh --slow-all
```
