"""Explicit live check: tenant tags stay in Tempo without splitting metrics.

Run with PYTHONPATH=tests/e2e .venv/bin/python tests/e2e/check_metric_cardinality.py.
Uses an isolated project and does not start or restart infrastructure.
"""

import os
import time
import uuid
from urllib.parse import urlencode

import tp_trace
from tp_trace.metrics import get_metrics_recorder
from smoke import eventually, request


def main():
    host = os.getenv("SMOKE_HOST", "localhost")
    collector = "http://{}:{}".format(os.getenv("SMOKE_COLLECTOR_HOST", host), os.getenv("OTLP_HTTP_PORT", "4318"))
    prometheus = "http://{}:9090".format(os.getenv("SMOKE_PROMETHEUS_HOST", host))
    tempo = "http://{}:3200".format(os.getenv("SMOKE_TEMPO_HOST", host))
    project = "tenant-cardinality-" + uuid.uuid4().hex[:12]

    def attr(key, value):
        return {"key": key, "value": {"stringValue": value}}

    resources = []
    first_trace = uuid.uuid4().hex
    for i in range(300):
        now = time.time_ns()
        resources.append({
            "resource": {"attributes": [attr("service.name", project), attr("project_name", project),
                                        attr("institute", str(i)), attr("request_id", "preserved-resource-tag")]},
            "scopeSpans": [{"scope": {"name": "tenant-probe"}, "spans": [{
                "traceId": first_trace if i == 0 else uuid.uuid4().hex,
                "spanId": uuid.uuid4().hex[:16], "name": "tenant-probe", "kind": 2, "flags": 1,
                "startTimeUnixNano": str(now - 1000000), "endTimeUnixNano": str(now),
                "status": {"code": 1}, "attributes": [
                    attr("normalized.service", "django"), attr("normalized.operation", "GET /probe/"),
                    attr("http.route_template", "/probe/"), attr("http.method", "GET"),
                    attr("institute", str(i)), attr("customer_id", str(i)),
                ],
            }]}],
        })
    response = request(collector + "/v1/traces", {"resourceSpans": resources})
    assert not response.get("partialSuccess"), response
    tp_trace.init(project_name=project, endpoint=collector, export_batch=False,
                  tags={"institute": "sdk-tenant", "customer_id": "sdk-customer"})
    recorder = get_metrics_recorder()
    for i in range(1200):
        recorder.record_repeated_query_affected_request(
            method="GET", route="/probe/", extra_attributes={"institute": str(i), "request_id": str(i)},
        )
    assert recorder.meter_provider.force_flush()

    def metric(metric_name, expected):
        query = metric_name + '{project_name="' + project + '"}'
        rows = request(prometheus + "/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
        if len(rows) != 1 or float(rows[0]["value"][1]) != expected:
            return False
        assert not {"institute", "institute_subdomain"}.intersection(rows[0]["metric"])
        if metric_name == "apm_calls_total":
            assert rows[0]["metric"]["request_id"] == "preserved-resource-tag"
        if "repeated_query" in metric_name:
            assert rows[0]["metric"].get("worker_id")
        return True

    eventually("300 tenant resources share one RED counter totaling 300", lambda: metric("apm_calls_total", 300))
    eventually("1200 tenant observations share one N+1 counter with worker identity", lambda: metric("apm_repeated_query_affected_requests_total", 1200))

    def trace_attributes_preserved():
        data = request(tempo + "/api/traces/" + first_trace)
        groups = data.get("batches", data.get("resourceSpans", []))
        for group in groups:
            attrs = {a["key"]: a["value"] for a in group.get("resource", {}).get("attributes", [])}
            if "institute" in attrs and "request_id" in attrs:
                return True
        return False

    eventually("Tempo retains institute and custom resource tags", trace_attributes_preserved)
    print("PASS tenant-independent metrics: " + project)


if __name__ == "__main__":
    main()
