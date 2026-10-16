# Deployment reference

| Component | Managed from | Endpoint / port |
|---|---|---|
| Application SDK | Application deployment | Sends OTLP/HTTP to the configured Collector endpoint. |
| Collector and Prometheus | [testpress-ansible](https://github.com/testpress/testpress-ansible) | Deployment-specific endpoint; application and collector IPs require allow-listing. |
| Tempo | `setup/` | Host `4327` → OTLP/gRPC `4317`; host `4328` → OTLP/HTTP `4318`; `3200` query API. |
| Grafana | repository root `docker-compose.yml` | `3000`. |

Prometheus uses the platform push method. It does not scrape each application instance.
