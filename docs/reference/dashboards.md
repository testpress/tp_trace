# Dashboards reference

The repository-root Compose file provisions Grafana. Its data sources are Prometheus and Tempo. The dashboard files are in `grafana/dashboards/`.

| Dashboard | Purpose |
|---|---|
| Service Catalog | Application and dependency health overview. |
| Needs Attention | High latency, errors, and traffic anomalies. |
| Generic Service Overview | Metrics for a selected service. |
| Generic Operation Details | Metrics and trace links for a selected operation. |
| Collector Monitoring | Collector operational metrics when available. |

Prometheus exemplars link selected metric observations to Tempo traces.
