# tp_trace sample Django application

This directory is a Django application used to exercise the SDK's integrations. It is not a self-contained observability deployment. Provision Prometheus and the Collector through the team's Ansible repository, deploy Tempo from [`setup/`](../setup/), and configure the application with the collector endpoint.

The application contains examples for PostgreSQL, replicas, PgBouncer, Redis, outbound HTTP, Boto3/MinIO, errors, slow requests, and Django templates.

Use [the first-trace tutorial](../docs/tutorials/first-trace.md) for the SDK setup and [the integration reference](../docs/reference/integrations.md) to see what each example produces.
