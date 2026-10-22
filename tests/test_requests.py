"""Unit tests for Requests / HTTP Client Integration."""

import pytest
import requests
from requests.models import PreparedRequest, Response
from requests.sessions import Session

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_tracer, StatusCode

import tp_trace
from tp_trace.integrations.requests import RequestsIntegration
from tp_trace.integrations.requests.client import (
    _extract_request_meta,
    tp_trace_request_hook,
    tp_trace_response_hook,
)


@pytest.fixture(autouse=True)
def clean_sdk():
    tp_trace._reset_for_testing()
    yield
    tp_trace._reset_for_testing()


@pytest.fixture
def memory_exporter():
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-http-service",
        environment="test",
        exporter=exporter,
        export_batch=False,
    )
    return exporter


def test_extract_request_meta():
    req = PreparedRequest()
    req.method = "POST"
    req.url = "https://user:password@api.stripe.com:8443/v1/charges?limit=10"

    method, sanitized_url, scheme, hostname, port, peer_service = _extract_request_meta(req)
    assert method == "POST"
    assert sanitized_url == "https://api.stripe.com:8443/v1/charges?limit=10"
    assert "password" not in sanitized_url
    assert scheme == "https"
    assert hostname == "api.stripe.com"
    assert port == 8443
    assert peer_service == "api.stripe.com"

    req_http = PreparedRequest()
    req_http.method = "get"
    req_http.url = "http://example.com/api"
    method, sanitized_url, scheme, hostname, port, peer_service = _extract_request_meta(req_http)
    assert method == "GET"
    assert port == 80
    assert scheme == "http"


def test_outbound_query_string_exposed_by_default():
    """Default (expose) mode: outbound url.full keeps the raw query string (dd-trace client parity)."""
    req = PreparedRequest()
    req.method = "GET"
    req.url = "https://httpbin.org/get?token=secret123&page=2"

    method, sanitized_url, scheme, hostname, port, peer_service = _extract_request_meta(req)
    assert sanitized_url == "https://httpbin.org/get?token=secret123&page=2"


def test_outbound_query_string_redacted_in_redact_mode():
    """Redact mode: outbound sensitive query values are REDACTED."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-http-service",
        exporter=exporter,
        export_batch=False,
        query_mode="redact",
    )

    req = PreparedRequest()
    req.method = "GET"
    req.url = "https://httpbin.org/get?token=secret123&page=2"

    method, sanitized_url, scheme, hostname, port, peer_service = _extract_request_meta(req)
    assert sanitized_url == "https://httpbin.org/get?token=REDACTED&page=2"


def test_outbound_query_string_stripped_in_strip_mode():
    """Strip mode: outbound url.full carries no query string."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-http-service",
        exporter=exporter,
        export_batch=False,
        query_mode="strip",
    )

    req = PreparedRequest()
    req.method = "GET"
    req.url = "https://httpbin.org/get?token=secret123&page=2"

    method, sanitized_url, scheme, hostname, port, peer_service = _extract_request_meta(req)
    assert sanitized_url == "https://httpbin.org/get"


def test_tp_trace_request_hook(memory_exporter):
    tracer = get_tracer("test-tracer")
    req = PreparedRequest()
    req.method = "GET"
    req.url = "https://httpbin.org/get"

    with tracer.start_as_current_span("initial_name") as span:
        tp_trace_request_hook(span, req)

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.name == "🌐 HTTP GET httpbin.org"
    attrs = span.attributes
    assert attrs["http.request.method"] == "GET"
    assert attrs["url.full"] == "https://httpbin.org/get"
    assert attrs["server.address"] == "httpbin.org"
    assert attrs["server.port"] == 443
    assert attrs["peer.service"] == "httpbin.org"
    assert attrs["resource.name"] == "GET https://httpbin.org/get"
    assert attrs["http.target"] == "/get"
    assert attrs["normalized.service"] == "requests"
    assert attrs["normalized.operation"] == "HTTP GET"


def test_tp_trace_request_hook_query_params(memory_exporter):
    tracer = get_tracer("test-tracer")
    req = PreparedRequest()
    req.method = "GET"
    req.url = "https://httpbin.org/search?q=open+telemetry&page=1"

    with tracer.start_as_current_span("initial_name") as span:
        tp_trace_request_hook(span, req)

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["url.query"] == "q=open+telemetry&page=1"
    assert attrs["http.target"] == "/search?q=open+telemetry&page=1"
    assert attrs["url.full"] == "https://httpbin.org/search?q=open+telemetry&page=1"


def test_tp_trace_response_hook_error(memory_exporter):
    tracer = get_tracer("test-tracer")
    req = PreparedRequest()
    res = Response()
    res.status_code = 404

    with tracer.start_as_current_span("test_span") as span:
        tp_trace_response_hook(span, req, res)

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["http.response.status_code"] == 404
    assert span.attributes["error"] is True
    assert span.attributes["error.type"] == "HTTP404"


def test_requests_integration_lifecycle(memory_exporter):
    integ = RequestsIntegration()
    assert integ.is_installed() is True
    assert integ.name == "requests"

    success = integ.instrument()
    assert success is True

    session = Session()

    def dummy_send(self, request, **kwargs):
        res = Response()
        res.status_code = 200
        res.request = request
        return res

    req = PreparedRequest()
    req.method = "GET"
    req.url = "https://api.github.com/users"
    req.headers = {}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(requests.adapters.HTTPAdapter, "send", lambda *a, **k: dummy_send(None, a[1]))
        res = session.send(req)
        assert res.status_code == 200

    spans = memory_exporter.get_finished_spans()
    assert len(spans) >= 1
    assert any("api.github.com" in s.name for s in spans)

    integ.uninstrument()


def test_requests_integration_manager_registration():
    from tp_trace.integrations import get_integration_manager

    mgr = get_integration_manager()
    assert "requests" in mgr._registered_classes
    assert "http" in mgr._registered_classes

    resolved = mgr._resolve_class(mgr._registered_classes["requests"])
    assert resolved is RequestsIntegration
