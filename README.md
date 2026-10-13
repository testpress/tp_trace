# tp_trace

`tp_trace` is a lightweight, drop-in Python observability SDK built on OpenTelemetry. It provides distributed tracing and request waterfalls for Django, PostgreSQL, Redis, Boto3/S3, and outgoing HTTP requests, exporting standard OTLP traces directly to an OpenTelemetry Collector.

---

## Requirements

`tp_trace` requires Python ≥ 3.8. The Django integration is supported for **Django ≥ 4.2**. Other integrations are optional and instrumented only when installed.

## Installation

Install directly from GitHub:

```bash
pip install git+https://github.com/testpress/tp_trace.git
```

---

## Quickstart

Add `tp_trace.init()` to your Django application's `settings.py`:

```python
import tp_trace

tp_trace.init()
```

That's it. Calling `tp_trace.init()` automatically detects and instruments:
- **Django**: Request waterfall, middleware, views, and template rendering
- **Database**: PostgreSQL (`psycopg2`) cursor queries
- **Cache**: Redis commands and pipelines
- **AWS / S3**: Boto3 API calls
- **HTTP Client**: Outgoing calls made via `requests

---

## Masking & Data Exposure

By default `tp_trace` **exposes** infrastructure details in spans (matching
dd-trace): real Redis host/port, full Redis command text in `db.statement`
(keys and values, truncated at `TP_TRACE_REDIS_CMD_MAX_LENGTH`), and raw URL
query strings on both server and client spans. Deployments that do **not** own
the infrastructure can restore the pre-exposure masking posture with the three
toggles below (`TP_TRACE_REDIS_CMD_MAX_LENGTH` is an independent truncation
limit and applies in both modes):

| Env var | Default | Effect when enabled |
|---|---|---|
| `TP_TRACE_REDIS_MASK_ENDPOINT` | `false` | Redis spans mask `net.peer.name` / `net.peer.port` to `"?"` |
| `TP_TRACE_REDIS_MASK_COMMAND` | `false` | `db.statement` collapses command args to `"COMMAND ? ?"` |
| `TP_TRACE_REDIS_CMD_MAX_LENGTH` | `1000` | Truncate `db.statement` beyond this many chars (appends `"..."`) |
| `TP_TRACE_QUERY_MODE` | `expose` | `expose` = raw query string; `redact` = credential-like keys (`token`, `password`, ...) → `REDACTED`; `strip` = query omitted entirely |

```bash
# Full-masking posture for non-owned-infrastructure deployments
export TP_TRACE_REDIS_MASK_ENDPOINT=true
export TP_TRACE_REDIS_MASK_COMMAND=true
export TP_TRACE_QUERY_MODE=redact
```

The same options are available as `tp_trace.init()` kwargs
(`redis_mask_endpoint=`, `redis_mask_command=`, `redis_cmd_max_length=`,
`query_mode=`). Userinfo (`user:pass@`) in URLs is always stripped, in every
mode.

---

## Testing & Verification

Run the test suite:

```bash
uv run pytest
```
