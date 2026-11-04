"""Verify PgBouncer span dimensions reach Prometheus and scope Tempo queries.

Run explicitly against a Collector with the updated shared configuration:
PYTHONPATH=tests/e2e .venv/bin/python tests/e2e/check_pgbouncer.py.
Uses an isolated synthetic project; does not start or restart infrastructure.
"""

import os
import time
import uuid
from urllib.parse import urlencode

from smoke import eventually, request


def main():
    host = os.getenv("SMOKE_HOST", "localhost")
    collector = "http://{}:{}".format(os.getenv("SMOKE_COLLECTOR_HOST", host), os.getenv("OTLP_HTTP_PORT", "4318"))
    prometheus = "http://{}:9090".format(os.getenv("SMOKE_PROMETHEUS_HOST", host))
    tempo = "http://{}:3200".format(os.getenv("SMOKE_TEMPO_HOST", host))
    project = "pgbouncer-probe-" + uuid.uuid4().hex[:12]
    spans = []
    for service, pool, status in [("postgres", False, 1)] * 3 + [("postgres", True, 1)] * 4 + [("postgres", True, 2)] + [("pgbouncer", True, 1)] * 2:
        now = time.time_ns()
        attributes = {
            "normalized.service": service,
            "normalized.operation": "SELECT probe" if service == "postgres" else "pgbouncer.connect",
            "db.system": "postgresql",
            "db.instance": "defaultdb",
        }
        if pool:
            attributes["db.connection.pool"] = "pgbouncer"
        spans.append({
            "traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16],
            "name": attributes["normalized.operation"], "kind": 3, "flags": 1,
            "startTimeUnixNano": str(now - 10000000), "endTimeUnixNano": str(now),
            "status": {"code": status},
            "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attributes.items()],
        })
    response = request(collector + "/v1/traces", {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": project}},
            {"key": "project_name", "value": {"stringValue": project}},
        ]},
        "scopeSpans": [{"scope": {"name": "pgbouncer-probe"}, "spans": spans}],
    }]})
    assert not response.get("partialSuccess"), response

    def total(metric, labels):
        query = 'sum(' + metric + '{project_name="' + project + '",' + labels + '})'
        rows = request(prometheus + "/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
        return float(rows[0]["value"][1]) if rows else None

    pooled = 'normalized_service="postgres",db_connection_pool="pgbouncer"'
    eventually("five pooled SQL calls without connection spans", lambda: total("apm_calls_total", pooled) == 5)
    eventually("PostgreSQL totals include direct and pooled queries", lambda: total("apm_calls_total", 'normalized_service="postgres"') == 8)
    eventually("pooled query errors", lambda: total("apm_calls_total", pooled + ',error="true"') == 1)
    eventually("pooled query duration histogram", lambda: total("apm_duration_milliseconds_count", pooled) == 5)
    eventually("direct queries remain separate", lambda: total("apm_calls_total", 'normalized_service="postgres",db_connection_pool=""') == 3)

    def trace_count(pool):
        query = '{ resource.project_name = "' + project + '" && span.normalized.service = "postgres" && ("' + pool + '" = ".*" || span.db.connection.pool =~ "' + pool + '") }'
        return len(request(tempo + "/api/search?" + urlencode({"q": query, "limit": 20})).get("traces", []))

    eventually("PgBouncer trace filter excludes direct connections", lambda: trace_count("pgbouncer") == 5)
    eventually("All pools trace filter includes missing pool attributes", lambda: trace_count(".*") == 8)
    print("PASS PgBouncer metrics and traces: " + project)


if __name__ == "__main__":
    main()
