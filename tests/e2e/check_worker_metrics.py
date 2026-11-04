"""Verify Collector storage preserves N+1 datapoint worker identity.

Uses two isolated synthetic cumulative counters; does not start infrastructure.
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
    project = "worker-identity-probe-" + uuid.uuid4().hex[:12]
    now = time.time_ns()
    expected = {"worker-one": 11, "worker-two": 13}

    def attr(key, value):
        return {"key": key, "value": {"stringValue": value}}

    points = [{
        "startTimeUnixNano": str(now - 1000000000), "timeUnixNano": str(now),
        "asInt": str(value),
        "attributes": [attr("worker_id", worker), attr("metric_source", "tp_trace_sdk"),
                       attr("normalized_service", "django"), attr("http_route", "/worker-probe/")],
    } for worker, value in expected.items()]
    response = request(collector + "/v1/metrics", {"resourceMetrics": [{
        "resource": {"attributes": [attr("service.name", project), attr("project_name", project),
                                    attr("worker_id", "resource-key-must-not-replace-datapoint")]},
        "scopeMetrics": [{"scope": {"name": "tp-trace-worker-probe"}, "metrics": [{
            "name": "apm_repeated_query_affected_requests_total", "unit": "1",
            "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": points},
        }]}],
    }]})
    assert not response.get("partialSuccess"), response
    query = ('sum by (worker_id) (apm_repeated_query_affected_requests_total{'
             'project_name="' + project + '",metric_source="tp_trace_sdk",worker_id!=""})')

    def preserved():
        rows = request(prometheus + "/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
        return {r["metric"].get("worker_id"): float(r["value"][1]) for r in rows} == expected

    eventually("N+1 datapoint worker_id preserved: two independent counters totaling 24", preserved)


if __name__ == "__main__":
    main()
