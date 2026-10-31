# tp_trace sample Django application

This directory is a Django application used to exercise the SDK's integrations. To run it with local PostgreSQL, Redis, RabbitMQ, Celery worker, the Collector, Tempo, Prometheus, and Grafana, follow the [development setup](../../README.md).

The application contains examples for PostgreSQL, replicas, PgBouncer, Redis, RabbitMQ / Celery asynchronous tasks, outbound HTTP, Boto3/MinIO, errors, slow requests, and Django templates.

## Choosing the Collector endpoint

The app reads its OTLP/HTTP endpoint from `TP_TRACE_ENDPOINT`. The
[development stack](../../README.md) defaults to its own Collector; export a
different endpoint to send the sample app's telemetry to a deployed Collector:

From the repository root:

```bash
TP_TRACE_ENDPOINT=http://collector.example.com:4318 make up
```

The deployed Collector must be able to reach Prometheus on TCP `9090`; see the
[development README](../../README.md#configure-sdk-export) for the
failure signature and connectivity checks.

## Running Celery tasks

To trigger background Celery execution:

```bash
# Publish a successful task
curl -X POST http://localhost:8002/api/celery/publish/ \
  -H "Content-Type: application/json" \
  -d '{"value": 42, "fail": false}'

# Publish a failing task
curl -X POST http://localhost:8002/api/celery/publish/ \
  -H "Content-Type: application/json" \
  -d '{"value": 13, "fail": true}'
```

To run a standalone Celery worker locally:

```bash
celery -A api.celery_app:app worker --loglevel=info --concurrency=2
```

See the [integration reference](../../docs/04-integrations.md) for supported libraries and the [configuration reference](../../docs/05-configuration.md) for SDK settings.
