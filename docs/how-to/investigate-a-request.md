# Investigate a slow or failing request

1. Open Grafana and select the application dashboard.
2. Filter by the affected project, route, and time range.
3. Open a high-latency or error data point, then open its linked Tempo trace.
4. Read the trace waterfall from the root `django.request` span down.
5. Inspect the longest child span:
   - PostgreSQL spans identify the query and database role.
   - Redis spans identify the command or cache operation.
   - HTTP and Boto3 spans identify the downstream operation.
6. If the request is absent, confirm the application can reach the Collector and that the route is not ignored or sampled out.

See [dashboard reference](../reference/dashboards.md) for the available views and [configuration reference](../reference/configuration.md) for sampling and route controls.
