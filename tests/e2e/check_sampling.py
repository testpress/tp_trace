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
    # Higher bits encode parent metadata, independently of the sampled bit.
    # Format: (flags, status, tp_trace_sampled_attr, expected_keep)
    cases = [
        (flags, 1, None, bool(flags & 1))
        for flags in (0, 1, 256, 257, 768, 769)
    ]
    cases.append((256, 2, None, True))  # Preserves unsampled errors
    # Test tp_trace.sampled attribute overrides
    cases.append((0, 1, True, True))  # wire flags 0, attribute sampled True -> kept
    cases.append((1, 1, False, False))  # wire flags 1, attribute sampled False -> dropped
    cases.append((0, 2, False, True))  # wire flags 0, attribute sampled False, error -> kept

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

    if is_spanmetrics_enabled():
        query = 'sum(apm_calls_total{project_name="' + project + '"})'

        def all_counted():
            rows = request(prometheus + "/api/v1/query?" + urlencode({"query": query}))["data"]["result"]
            return bool(rows) and float(rows[0]["value"][1]) == len(cases)

        eventually("spanmetrics counted all synthetic spans", all_counted)

    eventually(
        "sampled spans and unsampled error stored in Tempo",
        lambda: all(stored(span["traceId"]) for span, (_, _, _, keep) in zip(spans, cases) if keep),
    )
    for span, (flags, status, sampled_attr, keep) in zip(spans, cases):
        assert stored(span["traceId"]) == keep, "Unexpected retention: flags={}, status={}, attr={}".format(flags, status, sampled_attr)
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
    expected = {}
    sampled = 0
    try:
        tracer = provider.get_tracer("sdk-sampling-probe")
        for i in range(21):
            error = i == 20
            with tracer.start_as_current_span("request", kind=SpanKind.SERVER) as span:
                decision = bool(span.get_span_context().trace_flags.sampled)
                sampled += int(decision)
                # In current SDK, SampledOnlySpanProcessor gates exports to sampled-only.
                # Once SampledOnlySpanProcessor is removed in the spanmetrics migration,
                # unsampled errors will reach the Collector and be preserved by filter/drop_unsampled.
                expected[format(span.get_span_context().trace_id, "032x")] = decision
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

        if is_spanmetrics_enabled():
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
