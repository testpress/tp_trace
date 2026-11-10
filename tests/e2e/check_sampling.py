"""Explicit live check: Tempo obeys SDK sampling decisions and wire flags.

Run against the local development stack: PYTHONPATH=tests/e2e .venv/bin/python tests/e2e/check_sampling.py.
Uses isolated synthetic spans and does not start infrastructure.
"""

import os
from pathlib import Path
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode

from smoke import eventually, request


def is_spanmetrics_enabled() -> bool:
    try:
        cfg = Path(__file__).resolve().parents[2] / "config" / "otel-collector.yaml"
        return "spanmetrics" in cfg.read_text()
    except Exception:
        return False


def main():
    host = os.getenv("SMOKE_HOST", "localhost")
    collector = "http://{}:{}".format(
        os.getenv("SMOKE_COLLECTOR_HOST", host), os.getenv("OTLP_HTTP_PORT", "4318")
    )
    prometheus = "http://{}:9090".format(os.getenv("SMOKE_PROMETHEUS_HOST", host))
    tempo = "http://{}:3200".format(os.getenv("SMOKE_TEMPO_HOST", host))
    project = "sampling-probe-" + uuid.uuid4().hex[:12]
    # Non-error cases: deterministic keep based on sampling flags
    cases = [
        (flags, 1, None, bool(flags & 1))
        for flags in (0, 1, 256, 257, 768, 769)
    ]
    # Test tp_trace.sampled attribute overrides
    cases.append((0, 1, True, True))  # wire flags 0, attribute sampled True -> kept
    cases.append((1, 1, False, False))  # wire flags 1, attribute sampled False -> dropped

    spans = []
    for flags, status, sampled_attr, keep in cases:
        now = time.time_ns()
        span_data = {
            "traceId": uuid.uuid4().hex,
            "spanId": uuid.uuid4().hex[:16],
            "name": "sampling-probe",
            "kind": 2,
            "flags": flags,
            "startTimeUnixNano": str(now - 1000000),
            "endTimeUnixNano": str(now),
            "status": {"code": status},
        }
        if sampled_attr is not None:
            span_data["attributes"] = [
                {"key": "tp_trace.sampled", "value": {"boolValue": sampled_attr}}
            ]
        spans.append(span_data)

    # Error cases: evaluated under 50% probabilistic error policy.
    error_spans = []
    for _ in range(8):
        now = time.time_ns()
        error_spans.append({
            "traceId": uuid.uuid4().hex,
            "spanId": uuid.uuid4().hex[:16],
            "name": "sampling-probe-error",
            "kind": 2,
            "flags": 0,
            "startTimeUnixNano": str(now - 1000000),
            "endTimeUnixNano": str(now),
            "status": {"code": 2},
            "attributes": [
                {"key": "tp_trace.sampled", "value": {"boolValue": False}}
            ],
        })

    all_spans = spans + error_spans
    total_spans_count = len(all_spans)

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
                    "scopeSpans": [{"scope": {"name": "sampling-probe"}, "spans": all_spans}],
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

    if is_spanmetrics_enabled():
        query = 'sum(apm_calls_total{project_name="' + project + '"})'

        def all_counted():
            rows = request(prometheus + "/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
            return bool(rows) and float(rows[0]["value"][1]) == total_spans_count

        eventually("spanmetrics counted all synthetic spans", all_counted)

    eventually(
        "deterministic sampled non-error spans stored in Tempo",
        lambda: all(stored(span["traceId"]) for span, (_, _, _, keep) in zip(spans, cases) if keep),
    )
    for span, (flags, status, sampled_attr, keep) in zip(spans, cases):
        assert stored(span["traceId"]) == keep, "Unexpected retention: flags={}, status={}, attr={}".format(flags, status, sampled_attr)

    eventually(
        "at least one error trace retained under 50% error policy",
        lambda: any(stored(s["traceId"]) for s in error_spans),
    )
    print("PASS unsampled non-error spans absent; sampled spans and error spans preserved", flush=True)


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
    sampled_traces = []
    dropped_traces = []
    error_traces = []
    sampled = 0
    try:
        tracer = provider.get_tracer("sdk-sampling-probe")
        for i in range(22):
            error = i >= 20
            with tracer.start_as_current_span("request", kind=SpanKind.SERVER) as span:
                decision = bool(span.get_span_context().trace_flags.sampled)
                sampled += int(decision)
                tid = format(span.get_span_context().trace_id, "032x")
                if decision:
                    sampled_traces.append(tid)
                elif error:
                    error_traces.append(tid)
                else:
                    dropped_traces.append(tid)
                span.set_attribute("http.response.status_code", 500 if i == 20 else (404 if i == 21 else 200))
                if i == 21:
                    span.set_attribute("error", False)  # Explicit policy overrides error status for metrics.
                span.set_status(Status(StatusCode.ERROR if error else StatusCode.OK))
        assert provider.force_flush()
        assert 0 < sampled < 22, "Probe needs both sampled and unsampled decisions"

        def stored(trace_id):
            try:
                return bool(request(tempo + "/api/traces/" + trace_id).get("batches"))
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                return False

        if is_spanmetrics_enabled():
            def counted():
                query = 'sum(apm_calls_total{project_name="' + project + '"})'
                rows = request(prometheus + "/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
                return bool(rows) and float(rows[0]["value"][1]) == 22

            eventually("real SDK: all 22 spans counted at sample_rate=0.5", counted)

            def classified_errors():
                query = 'sum by (error, http_status_code) (apm_calls_total{project_name="' + project + '"})'
                rows = request(prometheus + "/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
                return {(r["metric"].get("error"), r["metric"].get("http_status_code")): float(r["value"][1]) for r in rows} == {
                    ("false", "200"): 20, ("true", "500"): 1, ("false", "404"): 1,
                }

            eventually("real SDK: RED error/status labels honor explicit false", classified_errors)

        eventually("real SDK: sampled traces reached Tempo", lambda: all(stored(t) for t in sampled_traces))
        for trace_id in sampled_traces:
            assert stored(trace_id), "SDK sampled trace missing from Tempo: " + trace_id
        for trace_id in dropped_traces:
            assert not stored(trace_id), "Unsampled non-error trace unexpectedly in Tempo: " + trace_id
        print("PASS real SDK retention: {} sampled decisions; remaining successful spans dropped".format(sampled), flush=True)
    finally:
        provider.shutdown()


def check_whole_trace_error_retention():
    """Verify that when an unsampled trace has a child error, the entire trace hierarchy is kept."""
    import base64

    host = os.getenv("SMOKE_HOST", "localhost")
    collector = "http://{}:{}".format(
        os.getenv("SMOKE_COLLECTOR_HOST", host), os.getenv("OTLP_HTTP_PORT", "4318")
    )
    tempo = "http://{}:3200".format(os.getenv("SMOKE_TEMPO_HOST", host))
    project = "full-tree-error-" + uuid.uuid4().hex[:12]
    now = time.time_ns()

    def decode_id(raw):
        if len(raw) == 16:
            return raw
        try:
            return base64.b64decode(raw).hex()
        except Exception:
            return raw

    def get_spans(tid):
        try:
            batches = request(tempo + "/api/traces/" + tid).get("batches", [])
            return [
                decode_id(s["spanId"])
                for b in batches
                for ss in b.get("scopeSpans", [])
                for s in ss.get("spans", [])
            ]
        except HTTPError as exc:
            if exc.code != 404:
                raise
            return []

    # Send multiple parent-child error traces so at least one is sampled under 50% error policy
    trace_pairs = []
    spans_to_send = []
    for _ in range(6):
        trace_id = uuid.uuid4().hex
        parent_id = uuid.uuid4().hex[:16]
        child_id = uuid.uuid4().hex[:16]
        trace_pairs.append((trace_id, parent_id, child_id))

        parent_span = {
            "traceId": trace_id,
            "spanId": parent_id,
            "name": "django.request",
            "kind": 2,
            "flags": 0,
            "startTimeUnixNano": str(now - 10000000),
            "endTimeUnixNano": str(now),
            "status": {"code": 1},
            "attributes": [
                {"key": "tp_trace.sampled", "value": {"boolValue": False}},
                {"key": "http.route", "value": {"stringValue": "/api/v2.4/dashboard/"}},
            ],
        }
        child_span = {
            "traceId": trace_id,
            "spanId": child_id,
            "parentSpanId": parent_id,
            "name": "redis.execute",
            "kind": 3,
            "flags": 0,
            "startTimeUnixNano": str(now - 8000000),
            "endTimeUnixNano": str(now - 5000000),
            "status": {"code": 2},
            "attributes": [
                {"key": "tp_trace.sampled", "value": {"boolValue": False}},
                {"key": "db.system", "value": {"stringValue": "redis"}},
            ],
        }
        spans_to_send.extend([parent_span, child_span])

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
                    "scopeSpans": [{"scope": {"name": "test"}, "spans": spans_to_send}],
                }
            ]
        },
    )
    assert not result.get("partialSuccess"), result

    def any_full_trace_retained():
        for tid, pid, cid in trace_pairs:
            spans = set(get_spans(tid))
            if spans:
                assert spans == {pid, cid}, "Orphan span detected! Expected both parent and child: " + tid
                return True
        return False

    eventually(
        "full parent-child error trace stored in Tempo without orphan spans",
        any_full_trace_retained,
    )
    print("PASS full parent-child hierarchy retained for error trace", flush=True)


if __name__ == "__main__":
    main()
    check_sdk_export()
    check_whole_trace_error_retention()
