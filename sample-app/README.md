# tp_trace sample Django application

This directory is a Django application used to exercise the SDK's integrations. To run it with local PostgreSQL, Redis, RabbitMQ, Celery worker, the Collector, Tempo, Prometheus, and Grafana, follow the [development setup](../dev/README.md).

The application contains examples for PostgreSQL, replicas, PgBouncer, Redis, RabbitMQ / Celery asynchronous tasks, outbound HTTP, Boto3/MinIO, errors, slow requests, and Django templates.

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

Use [the first-trace tutorial](../docs/tutorials/first-trace.md) for the SDK setup and [the integration reference](../docs/reference/integrations.md) to see what each example produces.
