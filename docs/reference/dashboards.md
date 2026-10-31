# Dashboards reference

The repository-root Compose file provisions Grafana. Its data sources are Prometheus and Tempo. The dashboard files are in `grafana/dashboards/`.

| Dashboard | Purpose |
|---|---|
| Service Catalog | Application and dependency health overview. |
| Needs Attention | High latency, errors, and traffic anomalies. |
| Generic Service Overview | Metrics for a selected service. |
| Generic Operation Details | Metrics and trace links for a selected operation. |
| Repeated Query Details | Affected-request count and rate for an API, plus matching sampled traces with up to five normalized repeated-query shapes, counts, and timing. |
| Collector Monitoring | Collector operational metrics when available. |

Prometheus exemplars link selected metric observations to Tempo traces.

Operational Issues and Service Catalog show **Repeated Queries** as a potential N+1 advisory. The affected-request metric is independent of trace sampling, so a positive count can appear without an example trace in the selected range. Query details are trace-only; no SQL is stored in the metric labels. See [the feature plan](../n-plus-one-apm-plan.md) for the detection and privacy contract.
