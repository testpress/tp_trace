"""Explicit live check: metrics see all spans; Tempo obeys sampled bit 0.

Run against the local development stack: .venv/bin/python tests/e2e/check_sampling.py.
Uses seven isolated synthetic spans and does not start infrastructure.
"""

import os
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode

from smoke import eventually, request


def main():
    host = os.getenv("SMOKE_HOST", "localhost")
    collector = "http://{}:{}".format(
        os.getenv("SMOKE_COLLECTOR_HOST", host), os.getenv("OTLP_HTTP_PORT", "4318")
    )
    prometheus = "http://{}:9090".format(os.getenv("SMOKE_PROMETHEUS_HOST", host))
    tempo = "http://{}:3200".format(os.getenv("SMOKE_TEMPO_HOST", host))
    project = "sampling-probe-" + uuid.uuid4().hex[:12]
    # Higher bits encode parent metadata, independently of the sampled bit.
    cases = [(flags, 1, bool(flags & 1)) for flags in (0, 1, 256, 257, 768, 769)]
    cases.append((256, 2, True))  # Existing policy preserves unsampled errors.
    spans = []
    for flags, status, keep in cases:
        now = time.time_ns()
        spans.append(
            {
                "traceId": uuid.uuid4().hex,
                "spanId": uuid.uuid4().hex[:16],
                "name": "sampling-probe",
                "kind": 2,
                "flags": flags,
                "startTimeUnixNano": str(now - 1000000),
                "endTimeUnixNano": str(now),
                "status": {"code": status},
            }
        )
    result = request(
        collector + "/v1/traces",
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": project}},
                            {"key": "project_name", "value": {"stringValue": project}},
                        ]
                    },
                    "scopeSpans": [{"scope": {"name": "sampling-probe"}, "spans": spans}],
                }
            ]
        },
    )
    assert not result.get("partialSuccess"), result

    def stored(trace_id):
        try:
            return bool(request(tempo + "/api/traces/" + trace_id).get("batches"))
        except HTTPError as exc:
            if exc.code != 404:
                raise
            return False

    query = 'sum(apm_calls_total{project_name="' + project + '"})'

    def all_counted():
        rows = request(prometheus + "/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
        return bool(rows) and float(rows[0]["value"][1]) == len(cases)

    eventually("spanmetrics counted all seven spans", all_counted)
    eventually(
        "sampled spans and unsampled error stored in Tempo",
        lambda: all(stored(span["traceId"]) for span, (_, _, keep) in zip(spans, cases) if keep),
    )
    for span, (flags, status, keep) in zip(spans, cases):
        assert stored(span["traceId"]) == keep, "Unexpected retention: flags={}, status={}".format(flags, status)
    print("PASS unsampled non-error spans absent for flags 0, 256, and 768", flush=True)


def check_sdk_export():
    """Exercise real Python serialization at 50% sampling, not hand-built flags."""
    import tp_trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.trace import SpanKind, Status, StatusCode

    host = os.getenv("SMOKE_HOST", "localhost")
    collector = "http://{}:{}".format(
        os.getenv("SMOKE_COLLECTOR_HOST", host), os.getenv("OTLP_HTTP_PORT", "4318")
    )
    tempo = "http://{}:3200".format(os.getenv("SMOKE_TEMPO_HOST", host))
    prometheus = "http://{}:9090".format(os.getenv("SMOKE_PROMETHEUS_HOST", host))
    project = "sdk-sampling-probe-" + uuid.uuid4().hex[:12]
    provider = tp_trace.init(
        project_name=project,
        sample_rate=0.5,
        auto_patch=False,
        metrics_enabled=False,
        exporter=OTLPSpanExporter(endpoint=collector + "/v1/traces"),
        export_batch=True,
    )
    expected = {}
    sampled = 0
    try:
        tracer = provider.get_tracer("sdk-sampling-probe")
        for i in range(21):
            error = i == 20
            with tracer.start_as_current_span("request", kind=SpanKind.SERVER) as span:
                decision = bool(span.get_span_context().trace_flags.sampled)
                sampled += int(decision)
                expected[format(span.get_span_context().trace_id, "032x")] = decision or error
                span.set_status(Status(StatusCode.ERROR if error else StatusCode.OK))
        assert provider.force_flush()
        assert 0 < sampled < 21, "Probe needs both sampled and unsampled decisions"

        def stored(trace_id):
            try:
                return bool(request(tempo + "/api/traces/" + trace_id).get("batches"))
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                return False

        def counted():
            query = 'sum(apm_calls_total{project_name="' + project + '"})'
            rows = request(prometheus + "/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
            return bool(rows) and float(rows[0]["value"][1]) == 21

        eventually("real SDK: all 21 spans counted at sample_rate=0.5", counted)
        eventually("real SDK: selected traces reached Tempo", lambda: all(stored(t) for t, keep in expected.items() if keep))
        for trace_id, keep in expected.items():
            assert stored(trace_id) == keep, "SDK retention mismatch: " + trace_id
        print("PASS real SDK retention: {} sampled decisions; remaining successful spans dropped".format(sampled), flush=True)
    finally:
        provider.shutdown()


if __name__ == "__main__":
    main()
    check_sdk_export()
