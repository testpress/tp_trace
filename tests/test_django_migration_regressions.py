"""Behavioral regression tests for official OpenTelemetry DjangoInstrumentor migration.

Verifies:
1. Deferred instrumentation when Django settings are not yet configured.
2. Host hook composition and restoration (preserving pre-existing host hooks).
3. Ignore endpoints & sampling decision matching on WSGI requests without RAW_URI.
4. URL masking and query stripping overwriting upstream populated attributes.
5. Handlers created before uninstrumentation cleanly bypass tracing after teardown.
"""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision
from opentelemetry.trace import SpanKind

from django.conf import settings
from django.core.handlers.wsgi import WSGIHandler
from django.test import RequestFactory
from django.urls import clear_url_caches

import tp_trace
from tp_trace.integrations import get_integration_manager
from tp_trace.integrations.django import DjangoIntegration
from tp_trace.integrations.django.middleware import ensure_django_instrumented
from tp_trace.sampler import TpTraceRuleBasedSampler


@pytest.fixture(autouse=True)
def cleanup_django_test_state():
    tp_trace._reset_for_testing()
    orig_urlconf = getattr(settings, "ROOT_URLCONF", None)
    settings.ROOT_URLCONF = "tests.test_django"
    clear_url_caches()
    yield
    tp_trace._reset_for_testing()
    if orig_urlconf:
        settings.ROOT_URLCONF = orig_urlconf
        clear_url_caches()


def test_deferred_instrumentation_when_settings_not_configured(monkeypatch):
    """Calling ensure_django_instrumented when settings are not configured must defer without configuring defaults."""
    from django.conf import LazySettings

    # Simulate settings.configured == False
    monkeypatch.setattr(LazySettings, "configured", property(lambda self: False))

    integration = DjangoIntegration()
    result = ensure_django_instrumented(integration)
    assert result is False
    assert integration._installed_django_instrumentor is False
    assert integration._injected_otel_middleware is False


def test_host_hook_composition_and_restoration():
    """If host project already installed DjangoInstrumentor, tp_trace composes hooks and restores originals on teardown."""
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.django.middleware.otel_middleware import _DjangoMiddleware

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="composed-hooks-svc", exporter=exporter, export_batch=False)

    host_req_calls = []
    host_resp_calls = []

    def host_request_hook(span, req):
        host_req_calls.append(req.path)

    def host_response_hook(span, req, resp):
        host_resp_calls.append(resp.status_code)

    # 1. Host instruments official DjangoInstrumentor first
    instrumentor = DjangoInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()

    instrumentor.instrument(
        request_hook=host_request_hook,
        response_hook=host_response_hook,
    )
    assert instrumentor.is_instrumented_by_opentelemetry is True

    try:
        # 2. tp_trace integration instruments and composes
        integration = DjangoIntegration()
        integration.instrument()

        assert integration._installed_django_instrumentor is False  # tp_trace did not install it

        handler = WSGIHandler()
        handler.load_middleware()

        factory = RequestFactory()
        req = factory.get("/test/sample/")
        resp = handler.get_response(req)

        assert resp.status_code == 200
        assert len(host_req_calls) == 1
        assert host_req_calls[0] == "/test/sample/"
        assert len(host_resp_calls) == 1
        assert host_resp_calls[0] == 200

        # tp_trace attributes & trace headers must also be present
        assert "X-Trace-ID" in resp
        spans = exporter.get_finished_spans()
        server_span = next(s for s in spans if s.kind == SpanKind.SERVER)
        assert server_span.attributes.get("http.route_template") == "/test/sample/"

        # 3. Teardown tp_trace must restore host's original hooks and preserve host instrumentor
        integration.uninstrument()
        assert instrumentor.is_instrumented_by_opentelemetry is True
        assert _DjangoMiddleware._otel_request_hook == host_request_hook
        assert _DjangoMiddleware._otel_response_hook == host_response_hook

        # Existing host-owned middleware stays active, with only the host hooks.
        exporter.clear()
        response_after_teardown = handler.get_response(factory.get("/test/sample/"))
        assert response_after_teardown.status_code == 200
        assert host_req_calls == ["/test/sample/", "/test/sample/"]
        assert host_resp_calls == [200, 200]
        assert "X-Trace-ID" not in response_after_teardown
        assert any(s.kind == SpanKind.SERVER for s in exporter.get_finished_spans())
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()


def test_sampling_ignore_endpoints_wsgi_without_raw_uri():
    """Verify sampler extracts path from http.url on WSGI requests where RAW_URI is missing."""
    sampler = TpTraceRuleBasedSampler(
        global_sample_rate=1.0,
        ignore_endpoints=["/health*", "/readyz"],
    )

    # WSGI collect_request_attributes sets http.url when RAW_URI and REQUEST_URI are absent
    wsgi_attrs_ignored = {
        "http.method": "GET",
        "http.url": "http://127.0.0.1:8000/healthz?probe=liveness",
    }
    result_ignored = sampler.should_sample(
        parent_context=None,
        trace_id=0x1234,
        name="HTTP GET",
        kind=SpanKind.SERVER,
        attributes=wsgi_attrs_ignored,
    )
    assert result_ignored.decision == Decision.DROP

    wsgi_attrs_sampled = {
        "http.method": "GET",
        "http.url": "http://127.0.0.1:8000/api/v1/orders/",
    }
    result_sampled = sampler.should_sample(
        parent_context=None,
        trace_id=0x5678,
        name="HTTP GET",
        kind=SpanKind.SERVER,
        attributes=wsgi_attrs_sampled,
    )
    assert result_sampled.decision == Decision.RECORD_AND_SAMPLE


def test_query_stripping_and_masking_overwrites_upstream_attributes():
    """Verify URL masking and query stripping overwrite upstream populated attributes in both strip and redact modes."""
    # 1. Test strip mode
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="strip-mode-svc",
        exporter=exporter,
        export_batch=False,
        query_mode="strip",
    )

    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    req_strip = factory.get("/test/sample/?token=supersecret&user_id=42")
    resp_strip = handler.get_response(req_strip)
    assert resp_strip.status_code == 200

    spans_strip = exporter.get_finished_spans()
    span_strip = next(s for s in spans_strip if s.kind == SpanKind.SERVER)

    # Sensitive query must be omitted entirely
    assert "token" not in span_strip.attributes.get("http.target", "")
    assert "token" not in span_strip.attributes.get("http.url", "")
    assert "token" not in span_strip.attributes.get("url.full", "")
    assert span_strip.attributes.get("url.query") in (None, "")

    exporter.clear()
    tp_trace._reset_for_testing()

    # 2. Test redact mode
    exporter_redact = InMemorySpanExporter()
    tp_trace.init(
        project_name="redact-mode-svc",
        exporter=exporter_redact,
        export_batch=False,
        query_mode="redact",
    )

    handler = WSGIHandler()
    handler.load_middleware()

    req_redact = factory.get("/test/sample/?token=supersecret&page=2")
    resp_redact = handler.get_response(req_redact)
    assert resp_redact.status_code == 200

    spans_redact = exporter_redact.get_finished_spans()
    span_redact = next(s for s in spans_redact if s.kind == SpanKind.SERVER)

    assert "supersecret" not in span_redact.attributes.get("http.target", "")
    assert "supersecret" not in span_redact.attributes.get("http.url", "")
    assert "supersecret" not in span_redact.attributes.get("url.full", "")
    assert "token=REDACTED" in span_redact.attributes.get("http.target", "")
    assert "page=2" in span_redact.attributes.get("http.target", "")
    assert "token=REDACTED" in span_redact.attributes.get("url.query", "")


def test_handler_created_before_uninstrumentation_bypasses_cleanly():
    """A handler instance created while instrumented must execute cleanly and bypass tracing after uninstrumentation."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="lifecycle-teardown-svc", exporter=exporter, export_batch=False)

    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    req1 = factory.get("/test/sample/")
    resp1 = handler.get_response(req1)
    assert resp1.status_code == 200
    assert len(exporter.get_finished_spans()) > 0

    exporter.clear()

    # Uninstrument tp_trace
    get_integration_manager().uninstrument_all()

    # Existing handler instance processes another request
    req2 = factory.get("/test/sample/")
    resp2 = handler.get_response(req2)
    assert resp2.status_code == 200
    assert resp2.content == b"Hello from Django View"

    assert not any(s.kind == SpanKind.SERVER for s in exporter.get_finished_spans())
    assert "X-Trace-ID" not in resp2

    # Keep the provider live: shutting it down would mask continued instrumentation.
    from opentelemetry.trace import get_tracer

    with get_tracer("migration-regression").start_as_current_span("provider-still-live"):
        pass
    assert any(s.name == "provider-still-live" for s in exporter.get_finished_spans())


@pytest.mark.parametrize("allow", [False, True])
def test_extraction_predicate_receives_actual_request_once(allow):
    seen = []

    def policy(request):
        seen.append(request)
        return request.get_host() == "testserver" and request.path == "/test/sample/" and allow

    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="request-policy",
        exporter=exporter,
        export_batch=False,
        extract_trace_context=policy,
    )
    handler = WSGIHandler()
    request = RequestFactory().get(
        "/test/sample/",
        HTTP_TRACEPARENT="00-12345678901234567890123456789012-1234567890123456-01",
    )
    response = handler.get_response(request)
    assert response.status_code == 200
    assert seen == [request]
    server_span = next(s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER)
    if allow:
        assert server_span.context.trace_id == 0x12345678901234567890123456789012
        assert server_span.parent.span_id == 0x1234567890123456
    else:
        assert server_span.context.trace_id != 0x12345678901234567890123456789012
        assert server_span.parent is None
        assert server_span.attributes["http.client.traceparent"] == request.META["HTTP_TRACEPARENT"]


def test_uninstrument_leaves_host_hooks_intact_when_never_composed():
    """If host installed hooks and tp_trace never composed them, uninstrument must not erase host hooks."""
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.django.middleware.otel_middleware import _DjangoMiddleware

    def host_req(span, req):
        pass

    def host_resp(span, req, resp):
        pass

    instrumentor = DjangoInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()

    instrumentor.instrument(request_hook=host_req, response_hook=host_resp)
    try:
        assert _DjangoMiddleware._otel_request_hook == host_req
        assert _DjangoMiddleware._otel_response_hook == host_resp

        # Create DjangoIntegration without composing (e.g. uninstrumenting directly)
        integration = DjangoIntegration()
        integration.uninstrument()

        # Host hooks must remain intact, not set to None
        assert _DjangoMiddleware._otel_request_hook == host_req
        assert _DjangoMiddleware._otel_response_hook == host_resp
    finally:
        instrumentor.uninstrument()


def test_sampled_out_request_records_metrics_and_response_headers(monkeypatch):
    """Even when a request is sampled out (sample_rate=0.0), response headers and request metrics are still emitted."""
    recorded_metrics = []

    def mock_record_request(**kwargs):
        recorded_metrics.append(kwargs)

    import tp_trace.metrics

    monkeypatch.setattr(tp_trace.metrics, "record_request", mock_record_request)

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="sampled-out-svc", exporter=exporter, sample_rate=0.0, export_batch=False)

    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    req = factory.get("/test/sample/")
    resp = handler.get_response(req)

    assert resp.status_code == 200
    # Trace headers must still be injected into response
    assert "X-Trace-ID" in resp
    assert "traceparent" in resp
    # No spans should be exported by in-memory exporter when sampled out
    assert len(exporter.get_finished_spans()) == 0
    # Metrics must still be recorded unconditionally
    assert len(recorded_metrics) == 1
    assert recorded_metrics[0]["status_code"] == 200
    assert recorded_metrics[0]["route"] == "/test/sample/"
    assert recorded_metrics[0]["error"] is False
