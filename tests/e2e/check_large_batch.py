"""Regression probe for Tempo rejecting collector batches above 4 MiB.

Run from the repo root: python3 tests/e2e/check_large_batch.py
Sends 8192 synthetic spans, then checks exports and traces across batch boundaries.
"""
import re
import time
import uuid

from smoke import eventually, request


def counters():
    text = request("http://localhost:8888/metrics")
    names = ("otelcol_exporter_sent_spans", "otelcol_exporter_send_failed_spans")
    return [sum(float(value) for labels, value in re.findall(
        r"^" + name + r"\{([^\n]+)\} ([0-9.e+]+)$", text, re.MULTILINE
    ) if 'exporter="otlp/tempo"' in labels) for name in names]


def main():
    before = counters()
    now = time.time_ns()
    spans = [{
        "traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16],
        "name": "large-batch-probe", "kind": 2,
        "startTimeUnixNano": str(now - 1000000), "endTimeUnixNano": str(now),
        "attributes": [{"key": "probe.padding", "value": {"stringValue": "x" * 900}}],
    } for _ in range(8192)]
    result = request("http://localhost:4318/v1/traces", {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "batch-smoke"}}]},
        "scopeSpans": [{"scope": {"name": "large-batch-regression"}, "spans": spans}],
    }]})
    assert not result.get("partialSuccess"), result
    eventually("8192 spans exported", lambda: counters()[0] - before[0] >= len(spans))
    for index in (0, 1023, 1024, 4095, 8191):
        trace_id = spans[index]["traceId"]
        eventually("stored span {}".format(index), lambda trace_id=trace_id:
                   bool(request("http://localhost:3200/api/traces/" + trace_id).get("batches")))
    assert counters()[1] == before[1], "Tempo export failures occurred during the probe"
    print("PASS oversized input split and exported without Tempo failures")


if __name__ == "__main__":
    main()
