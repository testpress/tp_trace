# Deploy Tempo

Use this directory to run the Tempo trace store. This is a deployment guide for operators who already have Docker Compose available on the observability host.

## Before you start

Allow inbound traffic from the Collector host only:

- `4327/tcp` for OTLP/gRPC traces
- `4328/tcp` for OTLP/HTTP traces
- `3200/tcp` for Grafana and authorised operators

## Start Tempo

From this directory, run:

```bash
docker compose up -d
```

Verify that Tempo is ready:

```bash
curl -f http://localhost:3200/ready
```

Tempo receives OTLP on container ports `4317` and `4318`; Docker publishes them as host ports `4327` and `4328`. Configure the Collector to send to the published port appropriate to its transport.

For the full endpoint list, see [deployment reference](../docs/reference/deployment.md).
