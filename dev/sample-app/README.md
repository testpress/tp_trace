# tp_trace sample Django application

This directory is a Django application used to exercise the SDK's integrations. To run it with local PostgreSQL, Redis, RabbitMQ, Celery worker, the Collector, Tempo, Prometheus, and Grafana, follow the [development setup](../../README.md).

The application contains examples for PostgreSQL, replicas, PgBouncer, Redis, RabbitMQ / Celery asynchronous tasks, outbound HTTP, Boto3/MinIO, errors, slow requests, and Django templates.

## Choosing the Collector endpoint

To send the sample app and worker telemetry to a different Collector, follow [Environment configuration](../../docs/03-integration.md#environment-configuration) and [Stack configuration](../README.md#stack-configuration).

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

See [integration and deployment](../../docs/03-integration.md) for application setup and SDK settings.
