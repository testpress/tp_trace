"""Unit tests for Phase 3: Django Integration (Full Waterfall)."""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

import django
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.template import Template, Context
from django.test import RequestFactory
from django.urls import path

# Configure minimal Django settings for testing if not already configured
if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="test-secret-key-12345",
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["*"],
        MIDDLEWARE=[
            "django.middleware.security.SecurityMiddleware",
            "django.middleware.common.CommonMiddleware",
        ],
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": False,
            }
        ],
    )
    django.setup()

import tp_trace
from tp_trace.config import SDKConfig
from tp_trace.integrations.django import DjangoIntegration


def sample_view(request):
    return HttpResponse("Hello from Django View", status=200)


def sample_template_view(request):
    t = Template("<h1>Hello, {{ name }}!</h1>")
    t.name = "sample_hello.html"
    rendered = t.render(Context({"name": "World"}))
    return HttpResponse(rendered, status=200)


def sample_error_view(request):
    return HttpResponse("Internal Server Error", status=500)


def sample_throttled_view(request):
    return JsonResponse({"detail": "Request was throttled. Expected available in 55 seconds."}, status=429)


class MockViewSet:
    """Simulates a Django REST Framework ViewSet."""
    pass


def create_drf_view_func(viewset_cls, action_name):
    def view_func(request, *args, **kwargs):
        return JsonResponse({"status": "ok"})
    view_func.cls = viewset_cls
    view_func.actions = {"get": action_name}
    return view_func


urlpatterns = [
    path("test/sample/", sample_view, name="sample-view"),
    path("test/template/", sample_template_view, name="template-view"),
    path("test/error/", sample_error_view, name="error-view"),
    path("test/throttled/", sample_throttled_view, name="throttled-view"),
]


@pytest.fixture(autouse=True)
def clean_sdk_and_django():
    tp_trace._reset_for_testing()
    from django.urls import clear_url_caches
    settings.ROOT_URLCONF = "tests.test_django"
    clear_url_caches()
    integration = DjangoIntegration()
    integration.instrument()
    yield
    integration.uninstrument()
    tp_trace._reset_for_testing()


def test_request_span_created():
    """Verify standard HTTP request creates a SERVER root span with HTTP attributes."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-test-svc", exporter=exporter, export_batch=False)

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/sample/")

    response = handler.get_response(request)
    assert response.status_code == 200
    assert "X-Trace-ID" in response
    assert "X-Span-ID" in response

    spans = exporter.get_finished_spans()
    request_spans = [s for s in spans if s.kind == SpanKind.SERVER]
    assert len(request_spans) == 1

    req_span = request_spans[0]
    assert req_span.kind == SpanKind.SERVER
    # Stable semconv (old http.method/http.target removed)
    assert req_span.attributes["http.request.method"] == "GET"
    assert req_span.attributes["http.response.status_code"] == 200
    assert req_span.attributes["url.path"] == "/test/sample/"
    assert req_span.attributes["http.route"] == "http://testserver/test/sample/"
    assert req_span.status.status_code == StatusCode.OK


def test_view_span_resolves_name():
    """Verify internal view execution generates a django.view child span."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-test-svc", exporter=exporter, export_batch=False)

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/sample/")

    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    view_spans = [s for s in spans if "django.view" in s.name]
    assert len(view_spans) >= 1
    view_span = view_spans[0]
    assert view_span.kind == SpanKind.INTERNAL
    assert view_span.attributes["django.view"] == "sample_view"


def test_template_span_created():
    """Verify template rendering generates a django.template child span."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-test-svc", exporter=exporter, export_batch=False)

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/template/")

    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    template_spans = [s for s in spans if "django.template" in s.name]
    assert len(template_spans) == 1
    t_span = template_spans[0]
    assert t_span.kind == SpanKind.INTERNAL
    assert t_span.attributes["django.template.name"] == "sample_hello.html"


def test_error_500_marks_span():
    """Verify 500 responses mark request spans as ERROR."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-test-svc", exporter=exporter, export_batch=False)

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/error/")

    response = handler.get_response(request)
    assert response.status_code == 500

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert req_span.attributes["http.response.status_code"] == 500
    assert req_span.attributes["error"] is True
    assert req_span.status.status_code == StatusCode.ERROR


def test_w3c_traceparent_propagation():
    """Verify incoming W3C traceparent headers link the root request span to the distributed trace."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-test-svc", exporter=exporter, export_batch=False)

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span_id = "00f067aa0ba902b7"
    traceparent = f"00-{trace_id}-{parent_span_id}-01"

    request = factory.get("/test/sample/", HTTP_TRACEPARENT=traceparent)
    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)

    assert format(req_span.context.trace_id, "032x") == trace_id
    assert format(req_span.parent.span_id, "016x") == parent_span_id


def test_w3c_traceparent_ignored_when_extract_disabled():
    """Verify incoming W3C traceparent headers are ignored when extract_trace_context=False."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="django-test-svc",
        exporter=exporter,
        export_batch=False,
        extract_trace_context=False,
    )

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span_id = "00f067aa0ba902b7"
    traceparent = f"00-{trace_id}-{parent_span_id}-01"

    request = factory.get("/test/sample/", HTTP_TRACEPARENT=traceparent)
    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)

    # Must be a root span (no parent span)
    assert req_span.parent is None
    # Must have generated a new trace_id instead of adopting the external one
    assert format(req_span.context.trace_id, "032x") != trace_id
    # Incoming client header is preserved for debugging
    assert req_span.attributes.get("http.client.traceparent") == traceparent


def test_w3c_traceparent_callable_filter():
    """Verify extract_trace_context accepts a callable to filter external callers."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="django-test-svc",
        exporter=exporter,
        export_batch=False,
        extract_trace_context=lambda req: not req.META.get("HTTP_USER_AGENT", "").startswith("external-cli"),
    )

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span_id = "00f067aa0ba902b7"
    traceparent = f"00-{trace_id}-{parent_span_id}-01"

    # 1. External CLI request -> Should ignore traceparent
    request_ext = factory.get(
        "/test/sample/",
        HTTP_TRACEPARENT=traceparent,
        HTTP_USER_AGENT="external-cli/1.0",
    )
    res_ext = handler.get_response(request_ext)
    assert res_ext.status_code == 200

    spans = exporter.get_finished_spans()
    ext_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert ext_span.parent is None
    assert format(ext_span.context.trace_id, "032x") != trace_id

    exporter.clear()

    # 2. Internal / other request -> Should accept traceparent
    request_in = factory.get(
        "/test/sample/",
        HTTP_TRACEPARENT=traceparent,
        HTTP_USER_AGENT="internal-service/2.0",
    )
    res_in = handler.get_response(request_in)
    assert res_in.status_code == 200

    spans = exporter.get_finished_spans()
    in_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert in_span.parent is not None
    assert format(in_span.context.trace_id, "032x") == trace_id
    assert format(in_span.parent.span_id, "016x") == parent_span_id


def test_throttled_429_marks_span():
    """Verify 429 Too Many Requests sets status code correctly without 5xx error flag."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-test-svc", exporter=exporter, export_batch=False)

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/throttled/")

    response = handler.get_response(request)
    assert response.status_code == 429

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert req_span.attributes["http.response.status_code"] == 429
    assert req_span.attributes["error"] is False
    assert req_span.status.status_code == StatusCode.OK


def test_static_tags_applied_to_request_span():
    """Verify static tags from SDKConfig.tags are applied to request spans."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="django-test-svc",
        exporter=exporter,
        export_batch=False,
        tags={"org": "testpress", "institute": "karunya", "subdomain": "lms"},
    )

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/sample/")
    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert req_span.attributes["org"] == "testpress"
    assert req_span.attributes["institute"] == "karunya"
    assert req_span.attributes["subdomain"] == "lms"


def test_static_tags_applied_to_view_span():
    """Verify static tags from SDKConfig.tags are applied to view spans."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="django-test-svc",
        exporter=exporter,
        export_batch=False,
        tags={"org": "testpress", "institute": "karunya"},
    )

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/sample/")
    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    view_spans = [s for s in spans if "django.view" in s.name]
    assert len(view_spans) >= 1
    view_span = view_spans[0]
    assert view_span.attributes["org"] == "testpress"
    assert view_span.attributes["institute"] == "karunya"


def test_on_request_span_callback():
    """Verify on_request_span callback is invoked with span and request."""
    callback_invoked = []

    def my_callback(span, request):
        callback_invoked.append(True)
        span.set_attribute("custom.callback", True)
        span.set_attribute("custom.request_path", getattr(request, "path", ""))

    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="django-test-svc",
        exporter=exporter,
        export_batch=False,
        tags={"org": "testpress"},
        on_request_span=my_callback,
    )

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/sample/")
    response = handler.get_response(request)
    assert response.status_code == 200
    assert len(callback_invoked) == 1

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert req_span.attributes["org"] == "testpress"
    assert req_span.attributes["custom.callback"] is True
    assert req_span.attributes["custom.request_path"] == "/test/sample/"


def test_on_request_span_callback_exception_silenced():
    """Verify on_request_span callback exceptions don't crash the request."""
    def bad_callback(span, request):
        raise RuntimeError("boom")

    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="django-test-svc",
        exporter=exporter,
        export_batch=False,
        on_request_span=bad_callback,
    )

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/sample/")
    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert req_span.attributes["http.response.status_code"] == 200


def test_on_request_span_captures_middleware_attributes():
    """Verify on_request_span callback sees attributes attached dynamically by middleware."""
    from types import SimpleNamespace
    from django.core.handlers.wsgi import WSGIHandler
    tp_trace._reset_for_testing()

    def _tag_institute_subdomain(span, request):
        institute = getattr(request, "institute", None)
        if institute and hasattr(institute, "subdomain"):
            span.set_attribute("institute.subdomain", institute.subdomain)

    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="django-test-svc",
        exporter=exporter,
        export_batch=False,
        on_request_span=_tag_institute_subdomain,
    )

    handler = WSGIHandler()
    handler.load_middleware()

    orig_chain = handler._middleware_chain
    def middleware_with_institute(request):
        request.institute = SimpleNamespace(subdomain="testpress")
        return orig_chain(request)
    handler._middleware_chain = middleware_with_institute

    factory = RequestFactory()
    request = factory.get("/test/sample/")
    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert req_span.attributes.get("institute.subdomain") == "testpress"


def test_on_request_span_with_response_argument():
    """Verify on_request_span callback supports optional 3-argument signature."""
    from django.core.handlers.wsgi import WSGIHandler
    tp_trace._reset_for_testing()

    exporter = InMemorySpanExporter()
    def my_callback(span, request, response):
        span.set_attribute("response.content_type", response.get("Content-Type", ""))

    tp_trace.init(
        project_name="django-test-svc",
        exporter=exporter,
        export_batch=False,
        on_request_span=my_callback,
    )

    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/sample/")
    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert "response.content_type" in req_span.attributes


def test_callback_takes_response_cached():
    """Verify _callback_takes_response caches callback inspection without repeated overhead."""
    from tp_trace.integrations.django.request import _callback_takes_response, _CALLBACK_ARITY_CACHE

    def cb_two(span, req):
        pass

    def cb_three(span, req, resp):
        pass

    assert _callback_takes_response(cb_two) is False
    assert id(cb_two) in _CALLBACK_ARITY_CACHE
    assert _CALLBACK_ARITY_CACHE[id(cb_two)] is False

    assert _callback_takes_response(cb_three) is True
    assert id(cb_three) in _CALLBACK_ARITY_CACHE
    assert _CALLBACK_ARITY_CACHE[id(cb_three)] is True


def test_tags_from_env_var(monkeypatch):
    """Verify tags can be set via TP_DOG_TAGS env var."""
    monkeypatch.setenv("TP_DOG_TAGS", "org=testpress,institute=karunya")
    cfg = SDKConfig.from_env_and_kwargs()
    assert cfg.tags == {"org": "testpress", "institute": "karunya"}
    monkeypatch.delenv("TP_DOG_TAGS")


def test_tags_kwargs_override_env(monkeypatch):
    """Verify kwargs tags override env var tags."""
    monkeypatch.setenv("TP_DOG_TAGS", "org=env_org,env_key=env_val")
    cfg = SDKConfig.from_env_and_kwargs(tags={"org": "kwarg_org", "kwarg_key": "kwarg_val"})
    assert cfg.tags == {"org": "kwarg_org", "kwarg_key": "kwarg_val"}
    monkeypatch.delenv("TP_DOG_TAGS")


def test_user_pii_redacted_for_authenticated_request():
    """Verify user PII (emails, usernames) is never attached to spans."""
    from unittest.mock import MagicMock

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-test-svc", exporter=exporter, export_batch=False)

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/sample/")
    user = MagicMock()
    user.is_authenticated = True
    user.pk = 42
    user.email = "admin@company.com"
    user.username = "superadmin"
    request.user = user

    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)

    # Pseudonymous IDs are permitted
    assert req_span.attributes.get("usr.id") == "42"
    assert req_span.attributes.get("user.id") == "42"
    assert req_span.attributes.get("enduser.id") == "42"
    assert req_span.attributes.get("user.is_authenticated") is True

    # PII MUST NOT be exported
    assert "usr.email" not in req_span.attributes
    assert "user.email" not in req_span.attributes
    assert "usr.username" not in req_span.attributes
    assert "user.username" not in req_span.attributes
    assert "email" not in req_span.attributes
    assert "username" not in req_span.attributes


def test_sensitive_query_parameters_sanitized_in_django_request():
    """Verify sensitive query parameters (token, secret, apiKey, etc.) are redacted on span attributes."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-test-svc", exporter=exporter, export_batch=False, query_mode="redact")

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/test/sample/?token=secret123&page=2&apiKey=myKeyVal&sig=987654")

    response = handler.get_response(request)
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)

    assert "url.query" in req_span.attributes
    query = req_span.attributes["url.query"]
    assert "secret123" not in query
    assert "myKeyVal" not in query
    assert "987654" not in query
    assert "page=2" in query
    assert "token=REDACTED" in query
    assert "apiKey=REDACTED" in query
    assert "sig=REDACTED" in query


def test_client_address_trusted_proxies():
    """Verify X-Forwarded-For is only trusted when REMOTE_ADDR is in trusted_proxies, walking right-to-left."""
    from tp_trace.integrations.django.request import _resolve_client_address

    class DummyReq:
        def __init__(self, remote_addr, xff=None):
            self.META = {"REMOTE_ADDR": remote_addr}
            if xff:
                self.META["HTTP_X_FORWARDED_FOR"] = xff

    # 1. Untrusted proxy: client sent spoofed XFF, but REMOTE_ADDR is untrusted
    req = DummyReq("198.51.100.1", xff="1.1.1.1, 2.2.2.2")
    # With trusted_proxies empty, REMOTE_ADDR is used
    assert _resolve_client_address(req, []) == "198.51.100.1"
    # Even if 10.0.0.1 is trusted, remote is 198.51.100.1 -> still uses REMOTE_ADDR
    assert _resolve_client_address(req, ["10.0.0.1"]) == "198.51.100.1"

    # 2. Trusted proxy: remote_addr is in trusted_proxies -> walks right-to-left
    # Hop chain: client (203.0.113.195), proxy1 (10.0.0.2), proxy2 (10.0.0.1)
    req2 = DummyReq("10.0.0.1", xff="spoofed.ip, 203.0.113.195, 10.0.0.2")
    trusted = ["10.0.0.1", "10.0.0.2"]
    assert _resolve_client_address(req2, trusted) == "203.0.113.195"

    # 3. Port stripping and IPv6 brackets
    req3 = DummyReq("10.0.0.1", xff="[2001:db8::1]:8080, 10.0.0.2")
    assert _resolve_client_address(req3, trusted) == "2001:db8::1"


def test_unmatched_route_normalization():
    """Verify unresolvable routes map http.route to __unmatched__ to prevent cardinality explosion."""
    from tp_trace.integrations.django.request import _normalize_route, _preresolve_route

    # 1. Preresolve on non-existent path
    assert _preresolve_route("/scanners/wp-login.php") == "__unmatched__"

    # 2. Normalize route on request with no resolver_match
    class NoMatchReq:
        resolver_match = None

    assert _normalize_route(NoMatchReq(), "/random/123") == "__unmatched__"

    # 3. Full request through handler for a 404
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-test-svc", exporter=exporter, export_batch=False)

    from django.core.handlers.wsgi import WSGIHandler
    handler = WSGIHandler()
    handler.load_middleware()

    factory = RequestFactory()
    request = factory.get("/scanners/probe?id=999")
    response = handler.get_response(request)
    assert response.status_code == 404

    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert req_span.name == "GET 404"
    assert req_span.attributes["resource.name"] == "GET 404"
    assert req_span.attributes["http.route"] == "http://testserver/scanners/probe?id=999"
    assert req_span.attributes["http.route_template"] == "404"
    assert req_span.attributes["normalized.service"] == "django"
    assert req_span.attributes["normalized.operation"] == "GET 404"
    assert req_span.attributes["url.path"] == "/scanners/probe"


def test_dynamic_tenant_urlconf_resolution(monkeypatch):
    """Verify multi-tenant apps with dynamic request.urlconf, TENANT_URLCONF, or TP_DOG_EXTRA_URLCONFS resolve correctly."""
    from django.conf import settings
    from tp_trace.integrations.django.request import _preresolve_route

    class TenantReq:
        path_info = "/api/v2/posts/"
        path = "/api/v2/posts/"
        urlconf = None
        resolver_match = None

    req = TenantReq()
    assert _preresolve_route(req.path_info, request=req) == "__unmatched__"

    req.urlconf = "tests.test_django"
    route = _preresolve_route("/test/sample/", request=req)
    assert route == "/test/sample/"

    # Test discovery via TENANT_URLCONF
    req.urlconf = None
    monkeypatch.setattr(settings, "TENANT_URLCONF", "tests.test_django", raising=False)
    route = _preresolve_route("/test/sample/", request=req)
    assert route == "/test/sample/"

    # Test discovery via TP_DOG_EXTRA_URLCONFS
    monkeypatch.delattr(settings, "TENANT_URLCONF", raising=False)
    monkeypatch.setattr(settings, "TP_DOG_EXTRA_URLCONFS", ["tests.test_django"], raising=False)
    route = _preresolve_route("/test/sample/", request=req)
    assert route == "/test/sample/"


def test_django_integration_does_not_import_drf_at_init():
    """Regression: importing rest_framework eagerly during tp_trace.init()
    (which runs inside Django settings) makes DRF cache its default renderers
    -- including BrowsableAPIRenderer -- and turn JSON endpoints into HTML.

    The DRF patch must be registered as a deferred import hook, never forced.
    """
    import sys

    for name in [m for m in list(sys.modules) if m == "rest_framework" or m.startswith("rest_framework.")]:
        del sys.modules[name]

    integration = DjangoIntegration()
    integration.instrument()
    try:
        assert "rest_framework" not in sys.modules
        assert "rest_framework.views" not in sys.modules
    finally:
        integration.uninstrument()


def test_drf_dispatch_is_wrapped_when_drf_becomes_available(monkeypatch):
    """The deferred DRF hook must wrap APIView.dispatch once rest_framework is
    actually imported (simulated here with a stand-in module)."""
    import importlib.util
    import sys
    from types import ModuleType

    for name in [m for m in list(sys.modules) if m == "rest_framework" or m.startswith("rest_framework.")]:
        del sys.modules[name]

    drf_pkg = ModuleType("rest_framework")
    views_mod = ModuleType("rest_framework.views")

    class FakeAPIView:
        def dispatch(self, request, *args, **kwargs):
            return "app-dispatch"

    views_mod.APIView = FakeAPIView
    sys.modules["rest_framework"] = drf_pkg
    sys.modules["rest_framework.views"] = views_mod

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: object() if name == "rest_framework" else real_find_spec(name, *a, **k),
    )

    integration = DjangoIntegration()
    integration.instrument()
    try:
        assert hasattr(FakeAPIView.dispatch, "__wrapped__"), (
            "rest_framework.views.APIView.dispatch was not wrapped by the deferred hook"
        )
    finally:
        integration.uninstrument()
        for name in [m for m in list(sys.modules) if m == "rest_framework" or m.startswith("rest_framework.")]:
            del sys.modules[name]


def test_django_server_span_respects_query_mode(monkeypatch):
    """Verify that Django server spans respect query_mode='expose' vs 'redact'."""
    from tp_trace.integrations.django.request import traced_get_response
    from tp_trace.config import SDKConfig

    factory = RequestFactory()
    request = factory.get("/api/v1/search?token=secret123&q=python")

    exporter = InMemorySpanExporter()
    cfg = SDKConfig(project_name="test-query-mode", query_mode="expose")
    _ = tp_trace.init(
        project_name="test-query-mode",
        environment="test",
        exporter=exporter,
        export_batch=False,
    )
    from tp_trace.integrations.django.request import set_config
    set_config(cfg)

    def view(req):
        return HttpResponse("OK")

    traced_get_response(view, None, (request,), {})

    spans = exporter.get_finished_spans()
    server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
    assert len(server_spans) == 1
    # Under expose, token is preserved
    assert "token=secret123" in server_spans[0].attributes["url.full"]

    # Now under redact mode
    exporter.clear()
    cfg_redact = SDKConfig(project_name="test-query-mode", query_mode="redact")
    set_config(cfg_redact)
    traced_get_response(view, None, (request,), {})
    server_spans = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert len(server_spans) == 1
    assert "token=REDACTED" in server_spans[0].attributes["url.full"]
    assert "q=python" in server_spans[0].attributes["url.full"]







