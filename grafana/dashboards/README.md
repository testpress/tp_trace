# Grafana Investigation Guide

> **Quick links:** [Open Grafana →](https://g.testpress.in) · [SDK setup](../../docs/03-integration.md)

This guide walks you through investigating live telemetry for instrumented services — slow requests, error spikes, missing data, and AI-assisted debugging via Grafana MCP.

---

## Where to start

Open [Service Catalog](https://g.testpress.in) and set the time range to when the problem occurred. Filter by service and look at three signals:

| Signal | What to look for |
|---|---|
| **RPS** (requests/sec) | Sudden drops or spikes in traffic |
| **Error rate** | Any uptick, especially correlated with a deploy |
| **p95 latency** | Sustained elevation across an endpoint |

---

## Investigate a slow request

**Goal:** find which span or database call accounts for the latency.

1. From **Service Catalog**, open the affected service → **Service Overview — Latency, Throughput & Errors**.
2. Compare p95 and p99 latency, traffic, and errors over the same time window.
3. In **Service Operations**, pick the endpoint with the highest latency.
4. Open **Request & Database Operation Traces — Flamegraphs**, click into a slow trace, and inspect the waterfall.

**What to look for in the waterfall:**
- A single long span that blocks the response (e.g. a slow external call or query)
- The same DB query repeated many times in one request (N+1 pattern)
- Gaps between spans (serialization, lock wait, thread scheduling)

---

## Investigate an error spike

**Goal:** identify which endpoint and status code is failing, then find the offending span.

1. Open **Application Errors — Error Rates & Failure Analysis**. Filter to the affected service and time window.
2. Find the endpoint and status code driving the spike.
3. Click a trace ID in **Error Trace Explorer** to open the failing span.

> [!NOTE]
> APM excludes HTTP 4xx responses from its error count. Only 5xx and instrumented exceptions appear here.

4. Open **Application Anomalies & Incidents — Investigation** to check for auto-detected issues in the same window. Results depend on configured detection rules and available telemetry.

---

## Check for missing or delayed telemetry

If a service or time range shows no data:

1. Verify the time range and filters are correct.
2. Confirm the service is instrumented and the process is running.
3. Open **OpenTelemetry Collector — Health, Resource Usage & Metrics** and check:

| Symptom | Likely cause |
|---|---|
| Spans ingested < spans exported | Drops at the Collector — check `refused` or `dropped` counters |
| Growing Tempo export queue | Backpressure from Tempo or network latency |
| High Collector CPU / memory | Tail-sampling or batch processor under load |

---

## Connect an AI coding tool to Grafana

Grafana MCP lets your AI coding client (Claude Code, Cursor, VS Code, Codex) query live telemetry while reading source code — useful for correlating a slow DB span with the ORM call that caused it.

### Prerequisites

```sh
# 1. Install uv (the MCP server runs via uvx)
curl -Lsf https://astral.sh/uv/install.sh | sh
uvx --version   # confirm it works

# 2. Store your service account token in a private file (outside the repo)
echo "glsa_xxxxxxxxxxxx" > ~/.config/grafana-token
chmod 600 ~/.config/grafana-token
```

Get your token from your team's shared secrets channel. Use a **read-only** service account.

---

### Client setup

Pick the client you use and follow the steps below. All clients connect to the same MCP server (`uvx mcp-grafana`).

#### Codex

1. Register the Grafana MCP server:

   ```sh
   codex mcp add grafana \
     --env GRAFANA_URL=https://g.testpress.in \
     --env GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE=/absolute/path/to/grafana-token \
     -- uvx mcp-grafana
   ```

2. Confirm the server was added:

   ```sh
   codex mcp list
   ```

   You should see `grafana` in the output. Restart Codex if it was already running.

#### Claude Code, Cursor, VS Code

1. Open your MCP configuration file:
   - **Claude Code / Cursor:** `~/.claude.json` or the in-app MCP settings
   - **VS Code:** `.vscode/mcp.json` in the project, or the user-level MCP settings

2. Add the following block under `mcpServers` (Claude Code / Cursor) or `servers` (VS Code):

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

3. Replace `/absolute/path/to/grafana-token` with the actual path to your token file (e.g. `~/.config/grafana-token`).

4. Save the file and **fully restart the client** — a reload alone may not pick up new MCP servers.

Refer to the official [installation guide](https://grafana.com/docs/grafana/latest/ai-tools/mcp/set-up/install-with-uvx/) and [token file auth docs](https://github.com/grafana/mcp-grafana/blob/main/docs/sources/configure/authentication.md) if you run into issues.

---

### Verify and start investigating

Confirm the connection first:

> Use Grafana MCP to find the Service Catalog dashboard and list the Prometheus datasources I can query. Report any connection or permission errors.

Then use one of these ready-made prompts. Swap in your service name and time window.

---

**Server health**
> Use Prometheus node exporter metrics to identify the application servers and show CPU and memory utilization for each over the last 30 minutes. Show the labels used to select the servers and call out any missing data.

---

**Slow API**
> Compare p95 latency, request rate, and error rate for `[service]` over the last hour vs. the previous hour. List the endpoints with the largest latency increases and show the PromQL queries you used.

---

**Error spike**
> Find which endpoints and status codes drove the increase in errors for `[service]` during `[time window]`. Link to relevant dashboard panels and, where available, representative traces. Use the source code to identify the handlers to inspect.
