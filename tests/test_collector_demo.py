"""The opt-in sample workload is bounded and preserves real trace parentage."""

import importlib.util
import json
from pathlib import Path

from django.test import RequestFactory, override_settings
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
import pytest

import tp_trace


@pytest.fixture
def demo():
    path = Path(__file__).resolve().parents[1] / "sample-app/api/collector_demo.py"
    spec = importlib.util.spec_from_file_location("sample_collector_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collector_demo


def test_demo_disabled_by_default(demo):
    assert demo(RequestFactory().get("/"), 0).status_code == 404


@pytest.mark.parametrize("operation", [-1, 256, 1000000])
@override_settings(COLLECTOR_DEMO_ENABLED=True)
def test_demo_rejects_operations_outside_finite_corpus(demo, operation):
    assert demo(RequestFactory().get("/"), operation).status_code == 400


@override_settings(COLLECTOR_DEMO_ENABLED=True)
def test_demo_rejects_writes(demo):
    assert demo(RequestFactory().post("/"), 0).status_code == 405


@override_settings(COLLECTOR_DEMO_ENABLED=True)
def test_demo_emits_client_and_internal_spans_under_request(demo):
    exporter = InMemorySpanExporter()
    tp_trace.init(exporter=exporter, auto_patch=False)
    with trace.get_tracer("test").start_as_current_span("request") as parent:
        response = demo(RequestFactory().get("/"), 255)
        expected_trace = parent.get_span_context().trace_id
    assert response.status_code == 200
    assert json.loads(response.content)["trace_id"] == "{:032x}".format(expected_trace)
    trace.get_tracer_provider().force_flush()
    spans = exporter.get_finished_spans()
    client = next(span for span in spans if span.kind == SpanKind.CLIENT)
    children = [span for span in spans if span.name == "demo.internal"]
    assert len(children) == 3
    assert all(span.parent.span_id == client.context.span_id for span in children)
    assert client.parent.span_id == parent.get_span_context().span_id
    assert client.attributes["normalized.operation"] == "demo.operation.255"
    assert all(span.context.trace_id == expected_trace for span in spans)


@pytest.mark.parametrize("workload", ["catalog", "checkout", "search"])
@override_settings(COLLECTOR_DEMO_ENABLED=True)
def test_demo_routes_are_distinct_normalized_metric_dimensions(demo, workload):
    exporter = InMemorySpanExporter()
    tp_trace.init(exporter=exporter, auto_patch=False)
    response = demo(RequestFactory().get("/"), 0, workload=workload)
    assert response.status_code == 200
    trace.get_tracer_provider().force_flush()
    client = next(span for span in exporter.get_finished_spans() if span.kind == SpanKind.CLIENT)
    assert client.attributes["http.route"] == "api/collector-demo/" + workload + "/<int:operation>/"
    assert client.attributes["normalized.operation"] == "demo.operation.0"


@override_settings(COLLECTOR_DEMO_ENABLED=True, COLLECTOR_DEMO_SOAK=True)
def test_soak_corpus_is_larger_but_still_bounded(demo):
    assert demo(RequestFactory().get("/"), 256).status_code == 200
    assert demo(RequestFactory().get("/"), 16383).status_code == 200
    assert demo(RequestFactory().get("/"), 16384).status_code == 400
