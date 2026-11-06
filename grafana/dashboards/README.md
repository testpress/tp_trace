# Investigate application services in Grafana

Use this guide to investigate telemetry from an instrumented application.
For SDK setup, see [Integrate tp_trace into a Django app](../../docs/03-integration.md).

Start in **Service Catalog**. Select the time range when the problem occurred,
then filter by service. Check requests per second
(RPS), error rate, and p95 latency to see which services changed.
[Open Grafana](https://g.testpress.in).

## Investigate a slow request

1. Open the affected service from **Service Catalog** to reach **Service
   Overview — Latency, Throughput & Errors**.
2. Compare traffic, errors, and p95 and p99 latency over the same time range.
   In **Service Operations**, select an endpoint with elevated latency.
3. In **Request & Database Operation Traces — Flamegraphs**, open a slow trace
   and inspect its waterfall. Look for spans that account for the delay and
   repeated database calls within the same request.

## Investigate an error spike

1. Open **Application Errors — Error Rates & Failure Analysis** and filter to
   the affected service and time range.
2. Check which endpoints and status codes account for the spike, then click a
   trace ID in **Error Trace Explorer** to find the failing span. APM excludes
   HTTP 4xx responses from its error count.
3. Open **Application Anomalies & Incidents — Investigation** to look for
   detected issues in the same window. This view depends on configured
   detection and available telemetry.

## Check for missing or delayed telemetry

Check the selected time range and filters, then verify that the service is
instrumented and exporting telemetry. In **OpenTelemetry Collector — Health,
Resource Usage & Metrics**, compare spans ingested and exported, and look for
dropped or refused spans, a growing Tempo export queue, or high CPU or memory.

## Connect an AI coding tool to Grafana

Use Grafana MCP to query telemetry alongside the source code during an
investigation. Available data depends on the configured datasources, the MCP
tools your client exposes, and the service account's permissions.

1. Install `uv` and confirm `uvx --version` runs in the environment that
   launches your client. The client starts `uvx mcp-grafana` locally.
2. Obtain a read-only Grafana service account token through your team's
   approved channel. Store it in a private local file outside the repository,
   readable only by your user. Use the absolute path to that file in the
   examples below.
3. Add the server using your client's configuration and reload the client.
   [Grafana MCP installation guide](https://grafana.com/docs/grafana/latest/ai-tools/mcp/set-up/install-with-uvx/)
   and [token file configuration](https://github.com/grafana/mcp-grafana/blob/main/docs/sources/configure/authentication.md).

### Codex

```sh
codex mcp add grafana \
  --env GRAFANA_URL=https://g.testpress.in \
  --env GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE=/absolute/path/to/grafana-token \
  -- uvx mcp-grafana
codex mcp list
```

### Claude Code, Cursor, and VS Code

Add the following entry under `mcpServers` in Claude Code or Cursor. In VS Code,
add the same entry under `servers` in your MCP configuration.

```json
{
  "mcpServers": {
    "grafana": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-grafana"],
      "env": {
        "GRAFANA_URL": "https://g.testpress.in",
        "GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE": "/absolute/path/to/grafana-token"
      }
    }
  }
}
```

### Verify the connection and try an investigation

Start by asking your client to make a real Grafana query.

> Use Grafana MCP to find the Service Catalog dashboard and list the Prometheus
> datasources I can query. Report any connection or permission errors.

Then try a focused prompt. Replace bracketed values with your incident details.

- **Server health.** Use Prometheus node exporter metrics to identify the
  application servers and show CPU and memory utilization for each over the
  last 30 minutes. Show the labels used to select the servers and call out
  missing data.
- **Slow API.** Compare p95 latency, request rate, and errors for [service] in
  the last hour with the previous hour. List the endpoints with the largest
  latency increases and show the queries you used.
- **Error spike.** Find which endpoints and status codes drove the increase
  in errors for [service] during [time window]. Link to relevant dashboard
  panels and, where available, representative traces. Use the source code to
  identify the handlers to inspect.
