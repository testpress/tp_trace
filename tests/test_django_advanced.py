"""Advanced tests for Stage 2-7: middleware, metrics, waterfall, endpoint normalization."""

import django
from django.conf import settings
from django.http import HttpResponse
from django.template import Template, Context
from django.test import RequestFactory
from django.urls import path, clear_url_caches

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

import tp_trace
from tp_trace.integrations.django import DjangoIntegration

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
        TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": False}],
    )
    django.setup()


def product_view(request, id):
    return HttpResponse(f"Product {id}")


def sample_view(request):
    return HttpResponse("Hello")


def template_view(request):
    t = Template("<h1>{{ name }}</h1>")
    t.name = "products.html"
    return HttpResponse(t.render(Context({"name": "World"})))


def error_view(request):
    raise ValueError("boom")


from django.views.generic.base import View


class ProfileView(View):
    def get(self, request):
        return HttpResponse("profile data")


class FakeViewSet(View):
    @classmethod
    def as_view(cls, actions=None, **initkwargs):
        actions = actions or {}

        def view(request, *args, **kwargs):
            self = cls(**initkwargs)
            self.action = actions.get(request.method.lower(), "list")
            for method, action in actions.items():
                setattr(self, method, getattr(self, action))
            return self.dispatch(request, *args, **kwargs)

        view.cls = cls
        view.actions = actions
        return view

    def list(self, request):
        return HttpResponse("orders list")

    def retrieve(self, request, id=None):
        return HttpResponse(f"order {id}")


route_observations = []


def route_probe_view(request):
    """Records the route published to child spans while the request is in flight."""
    from tp_trace.route_context import get_current_route

    route_observations.append(get_current_route())
    return HttpResponse("probe")


urlpatterns = [
    path("test/sample/", sample_view, name="sample-view"),
    path("test/template/", template_view, name="template-view"),
    path("api/products/<int:id>/", product_view, name="product-detail"),
    path("test/error/", error_view, name="error-view"),
    path("test/route-probe/", route_probe_view, name="route-probe"),
    path("test/profile/", ProfileView.as_view(), name="profile-view"),
    path("api/orders/", FakeViewSet.as_view(actions={"get": "list"}), name="orders-list"),
]


import pytest


@pytest.fixture(autouse=True)
def clean():
    tp_trace._reset_for_testing()
    from django.conf import settings as s

    original_urlconf = s.ROOT_URLCONF
    s.ROOT_URLCONF = __name__
    clear_url_caches()
    from tp_trace.integrations import get_integration_manager

    mgr = get_integration_manager()
    # Ensure django is instrumented via manager so reset can clean it
    mgr.apply_integrations()
    yield
    try:
        mgr.uninstrument_all()
    except Exception:
        pass
    tp_trace._reset_for_testing()
    s.ROOT_URLCONF = original_urlconf
    clear_url_caches()


def _make_handler(exporter):
    tp_trace.init(project_name="adv-test", exporter=exporter, export_batch=False)
    from django.core.handlers.wsgi import WSGIHandler

    h = WSGIHandler()
    h.load_middleware()
    return h


def test_middleware_spans_created():
    exporter = InMemorySpanExporter()
    handler = _make_handler(exporter)
    factory = RequestFactory()
    req = factory.get("/test/sample/")
    resp = handler.get_response(req)
    assert resp.status_code == 200
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    # Middleware spans should be present
    assert any("middleware" in n or "load_middleware" in n or "SecurityMiddleware" in n or "CommonMiddleware" in n for n in names)
    # Check tree structure: view span child of middleware, middleware child of request
    trace_spans = [s for s in spans if s.context.trace_id == spans[0].context.trace_id]
    assert len(trace_spans) >= 2
    assert any(s.kind == SpanKind.SERVER for s in trace_spans)


def test_middleware_module_has_docstring():
    """Verify that middleware module has its intended docstring at module level (P3-3)."""
    import tp_trace.integrations.django.middleware as mw
    assert mw.__doc__ is not None
    assert "Middleware spans" in mw.__doc__


def test_config_access_unification():
    """Verify all integration modules share tp_trace.config_access (P3-4)."""
    from tp_trace.config import SDKConfig
    import tp_trace.config_access as ca
    import tp_trace.integrations.django.middleware as mw
    import tp_trace.integrations.django.request as req
    import tp_trace.integrations.django.template as tmpl
    import tp_trace.integrations.django.view as vw
    import tp_trace.integrations.postgres.cursor as cur

    test_cfg = SDKConfig.from_env_and_kwargs(project_name="shared-config-test")
    try:
        mw.set_config(test_cfg)
        assert req._get_config() is test_cfg
        assert vw._get_config() is test_cfg
        assert tmpl._get_config() is test_cfg
        assert cur._get_config() is test_cfg
    finally:
        ca.set_config(None)


def test_waterfall_parent_child():
    exporter = InMemorySpanExporter()
    handler = _make_handler(exporter)
    factory = RequestFactory()
    req = factory.get("/test/template/")
    resp = handler.get_response(req)
    assert resp.status_code == 200
    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    for s in spans:
        assert s.context.trace_id == req_span.context.trace_id, f"trace_id mismatch for {s.name}"
    view_spans = [s for s in spans if "django.view" in s.name]
    assert len(view_spans) >= 1
    view_span = view_spans[0]
    assert view_span.parent is not None and view_span.parent.is_valid
    tmpl_spans = [s for s in spans if "django.template" in s.name]
    assert len(tmpl_spans) == 1
    tmpl = tmpl_spans[0]
    assert tmpl.parent.span_id == view_span.context.span_id, "template should be child of view"
    mw_ids = {s.context.span_id for s in spans if "django.middleware" in s.name}
    assert view_span.parent.span_id == req_span.context.span_id or view_span.parent.span_id in mw_ids


def test_trace_ids_consistent():
    exporter = InMemorySpanExporter()
    handler = _make_handler(exporter)
    factory = RequestFactory()
    for p in ["/test/sample/", "/test/sample/"]:
        req = factory.get(p)
        handler.get_response(req)
    spans = exporter.get_finished_spans()
    trace_ids = set(s.context.trace_id for s in spans)
    assert len(trace_ids) == 2
    for tid in trace_ids:
        trace_spans = [s for s in spans if s.context.trace_id == tid]
        assert any(s.kind == SpanKind.SERVER for s in trace_spans)


def test_request_and_view_spans_generated():
    exporter = InMemorySpanExporter()
    handler = _make_handler(exporter)
    factory = RequestFactory()
    req = factory.get("/test/sample/")
    handler.get_response(req)
    spans = exporter.get_finished_spans()
    assert len(spans) > 0
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert req_span.kind == SpanKind.SERVER
    assert req_span.attributes.get("http.route") == "http://testserver/test/sample/"
    assert req_span.attributes.get("http.request.method") == "GET"
    assert req_span.attributes.get("http.response.status_code") == 200


def test_endpoint_label_normalized():
    exporter = InMemorySpanExporter()
    handler = _make_handler(exporter)
    factory = RequestFactory()
    req = factory.get("/api/products/928371/")
    resp = handler.get_response(req)
    assert resp.status_code == 200
    spans = exporter.get_finished_spans()
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    route = req_span.attributes.get("http.route")
    route_template = req_span.attributes.get("http.route_template")
    assert route == "http://testserver/api/products/928371/", f"http.route should be full URL, got {route}"
    assert route_template == "/api/products/<int:id>/", f"route template should be normalized, got {route_template}"
    assert req_span.attributes.get("url.path") == "/api/products/928371/"


def test_route_resolution_uses_path_info_under_subpath_mount():
    """A sub-path mount must still pre-resolve to the real route.

    Django resolves against path_info (SCRIPT_NAME already stripped); request.path
    keeps SCRIPT_NAME. Resolving request.path therefore fails for every request
    under a sub-path mount, so the route published to child spans is
    __unmatched__ and -- via RouteEnrichingSpanProcessor -- poisons every child
    span in the request, breaking the per-endpoint spanmetrics series.
    """
    exporter = InMemorySpanExporter()
    handler = _make_handler(exporter)
    factory = RequestFactory()
    # RequestFactory builds request.path as SCRIPT_NAME + PATH_INFO, so this
    # yields path="/app/test/route-probe/" and path_info="/test/route-probe/".
    req = factory.get("/test/route-probe/", SCRIPT_NAME="/app")
    assert req.path == "/app/test/route-probe/"
    assert req.path_info == "/test/route-probe/"

    route_observations.clear()
    resp = handler.get_response(req)
    assert resp.status_code == 200

    assert route_observations == ["/test/route-probe/"], (
        "child spans would inherit "
        f"{route_observations} instead of the real route"
    )


def test_route_resolution_honors_per_request_urlconf():
    """A URLconf installed by middleware must be used for pre-resolution.

    Without forwarding request.urlconf, pre-resolution falls back to
    ROOT_URLCONF, so every request to a middleware-scoped URLconf reports
    __unmatched__ until resolver_match becomes available after the handler.
    """
    from tp_trace.integrations.django.request import _preresolve_route
    import django.urls as django_urls

    scoped_urlconf = object()  # stand-in for a middleware-installed URLconf

    class ScopedResolverMatch:
        route = "/scoped/thing/"

    def _spy(path, urlconf=None, *a, **k):
        assert path == "/scoped/thing/", f"resolved against wrong path: {path}"
        assert urlconf is scoped_urlconf, f"urlconf not forwarded, got {urlconf!r}"
        return ScopedResolverMatch()

    original = django_urls.resolve
    django_urls.resolve = _spy
    try:
        assert _preresolve_route("/scoped/thing/", scoped_urlconf) == "/scoped/thing/"
    finally:
        django_urls.resolve = original


def test_regex_route_pattern_cleaning():
    r"""Verify legacy regex patterns like ^admin/courses/(?P<slug>[\w-]+)/$ are converted to clean templates."""
    from tp_trace.integrations.django.request import _clean_regex_pattern

    raw = r"^admin/courses/(?P<course_slug>[\w-]+)/(?P<slug>[\w-]+)/$"
    cleaned = _clean_regex_pattern(raw)
    assert cleaned == "/admin/courses/<course_slug>/<slug>/"


def test_error_recording_and_metrics():
    exporter = InMemorySpanExporter()
    handler = _make_handler(exporter)
    factory = RequestFactory()
    req = factory.get("/test/error/")
    resp = handler.get_response(req)
    # Django converts unhandled exception to 500 response
    assert resp.status_code == 500
    spans = exporter.get_finished_spans()
    # At least one span should be ERROR with exception
    error_spans = [s for s in spans if s.status.status_code == StatusCode.ERROR]
    assert len(error_spans) >= 1
    has_exception = any(len(s.events) > 0 for s in error_spans)
    assert has_exception, "should have exception event"
    # Also check request span is error
    req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
    assert req_span.status.status_code == StatusCode.ERROR


def test_repeated_instrumentation_no_double_wrap():
    exporter = InMemorySpanExporter()
    # First instrument already done by fixture; test second on same instance is no-op
    # Create new standalone integration and instrument twice
    tp_trace._reset_for_testing()
    from django.conf import settings as s

    s.ROOT_URLCONF = __name__
    clear_url_caches()
    i = DjangoIntegration()
    assert i.instrument() is True
    assert i.instrument() is True  # second no-op
    # Now init and make handler
    tp_trace.init(project_name="repeat-test", exporter=exporter, export_batch=False, auto_patch=False)
    from django.core.handlers.wsgi import WSGIHandler

    h = WSGIHandler()
    h.load_middleware()
    factory = RequestFactory()
    req = factory.get("/test/sample/")
    h.get_response(req)
    spans = exporter.get_finished_spans()
    mw_call_spans = [s for s in spans if s.name.endswith(".__call__")]
    mw_req_spans = [s for s in spans if s.name.endswith(".process_request")]
    # Should still be 2, not 4
    assert len(mw_call_spans) == 2, f"double wrap produced duplicate call spans: {[s.name for s in mw_call_spans]}"
    assert len(mw_req_spans) == 2, f"double wrap produced duplicate request spans: {[s.name for s in mw_req_spans]}"
    i.uninstrument()
    tp_trace._reset_for_testing()


def test_uninstrument_restores():
    # This test verifies that uninstrument restores original behavior.
    # The autouse fixture already instrumented; we first verify instrumented works,
    # then explicitly uninstrument via manager and verify no spans.
    exporter = InMemorySpanExporter()
    # Re-use handler from fixture's instrumented state (already instrumented)
    # Create fresh handler to ensure chain is instrumented
    from django.core.handlers.wsgi import WSGIHandler

    # The fixture already did instrument + init, so we need to re-init with new exporter
    # First clear and re-init with new exporter
    tp_trace._reset_for_testing()
    from django.conf import settings as s

    s.ROOT_URLCONF = __name__
    clear_url_caches()
    from tp_trace.integrations import get_integration_manager

    mgr = get_integration_manager()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="uninstrument-test", exporter=exporter, export_batch=False)
    assert mgr.apply_integrations()  # re-instrument
    h = WSGIHandler()
    h.load_middleware()
    factory = RequestFactory()
    req = factory.get("/test/sample/")
    resp = h.get_response(req)
    assert resp.status_code == 200
    assert len([s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]) == 1
    # Now uninstrument via manager
    mgr.uninstrument_all()
    exporter.clear()
    # New handler after uninstrument should produce no spans (even though tracer provider still set, no patch)
    h2 = WSGIHandler()
    h2.load_middleware()
    req2 = factory.get("/test/sample/")
    resp2 = h2.get_response(req2)
    assert resp2.status_code == 200
    spans2 = exporter.get_finished_spans()
    # After uninstrument, no django spans should be created (only 0)
    assert len([s for s in spans2 if s.kind == SpanKind.SERVER]) == 0
    # Cleanup: re-instrument for remaining tests via fixture teardown will handle
    tp_trace._reset_for_testing()


def test_nested_template_includes_enabled_by_default():
    """Verify nested/included templates are captured by default matching Datadog behavior."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-template-test", exporter=exporter, export_batch=False)

    from django.template import Engine

    engine = Engine(
        dirs=[],
        app_dirs=False,
        loaders=[
            (
                "django.template.loaders.locmem.Loader",
                {
                    "parent.html": (
                        "<h1>Parent</h1>"
                        "{% include 'child.html' %}"
                    ),
                    "child.html": "<p>Child</p>",
                },
            )
        ],
    )

    tmpl = engine.get_template("parent.html")
    tmpl.render(Context({}))

    spans = exporter.get_finished_spans()
    template_spans = [s for s in spans if "django.template" in s.name]
    span_names = [s.name for s in template_spans]

    # Both parent and included child template are captured
    assert "django.template: parent.html" in span_names
    assert "django.template: child.html" in span_names


def test_nested_template_suppression_opt_out():
    """Verify nested/included templates can be suppressed via trace_nested_templates=False."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-template-test", exporter=exporter, export_batch=False, trace_nested_templates=False)

    from django.template import Engine

    engine = Engine(
        dirs=[],
        app_dirs=False,
        loaders=[
            (
                "django.template.loaders.locmem.Loader",
                {
                    "parent.html": (
                        "<h1>Parent</h1>"
                        "{% include 'child.html' %}"
                    ),
                    "child.html": "<p>Child</p>",
                },
            )
        ],
    )

    tmpl = engine.get_template("parent.html")
    tmpl.render(Context({}))

    spans = exporter.get_finished_spans()
    template_spans = [s for s in spans if "django.template" in s.name]
    span_names = [s.name for s in template_spans]

    # Only top-level template should have a span when opted out
    assert span_names == ["django.template: parent.html"]


def test_multipart_template_error_waterfall():
    """Verify multi-part template rendering."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-template-test", exporter=exporter, export_batch=False)

    from django.template import Engine

    # Define engine with in-memory dict loader for multi-part templates
    engine = Engine(
        dirs=[],
        app_dirs=False,
        loaders=[
            (
                "django.template.loaders.locmem.Loader",
                {
                    "parent.html": (
                        "<h1>Parent</h1>"
                        "{% include 'part1_header.html' %}"
                        "{% include 'part2_broken.html' %}"
                        "{% include 'part3_footer.html' %}"
                    ),
                    "part1_header.html": "<header>Header Part 1</header>",
                    "part2_broken.html": "<section>{{ fail_func }}</section>",
                    "part3_footer.html": "<footer>Footer Part 3</footer>",
                },
            )
        ],
    )

    def crashing_func():
        raise RuntimeError("Intentional error inside part2_broken.html")

    tmpl = engine.get_template("parent.html")
    ctx = Context({"fail_func": crashing_func})

    with pytest.raises(RuntimeError, match="Intentional error inside part2_broken.html"):
        tmpl.render(ctx)

    spans = exporter.get_finished_spans()
    template_spans = [s for s in spans if "django.template" in s.name]

    # Should have parent, part1, part2 (part3 is never reached)
    span_names = [s.name for s in template_spans]
    assert "django.template: parent.html" in span_names
    assert "django.template: part1_header.html" in span_names
    assert "django.template: part2_broken.html" in span_names
    assert "django.template: part3_footer.html" not in span_names

    # Check statuses
    part1_span = next(s for s in template_spans if s.name == "django.template: part1_header.html")
    assert part1_span.status.status_code == StatusCode.OK

    part2_span = next(s for s in template_spans if s.name == "django.template: part2_broken.html")
    assert part2_span.status.status_code == StatusCode.ERROR
    assert part2_span.attributes.get("error.type") == "RuntimeError"

    parent_span = next(s for s in template_spans if s.name == "django.template: parent.html")
    assert parent_span.status.status_code == StatusCode.ERROR


def test_template_pattern_exclusion():
    """Verify templates matching exclude patterns (e.g. django/forms/*, */widgets/*) are skipped."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="django-template-exclude-test",
        exporter=exporter,
        export_batch=False,
        template_instrumentation={
            "enabled": True,
            "exclude": [
                "django/forms/*",
                "debug_toolbar/*",
                "*/widgets/*",
            ],
        },
    )

    from django.template import Engine

    engine = Engine(
        dirs=[],
        app_dirs=False,
        loaders=[
            (
                "django.template.loaders.locmem.Loader",
                {
                    "main.html": (
                        "<h1>Main</h1>"
                        "{% include 'django/forms/default.html' %}"
                        "{% include 'admin/widgets/input.html' %}"
                        "{% include 'debug_toolbar/toolbar.html' %}"
                        "{% include 'components/navbar.html' %}"
                    ),
                    "django/forms/default.html": "<form></form>",
                    "admin/widgets/input.html": "<input />",
                    "debug_toolbar/toolbar.html": "<div>debug</div>",
                    "components/navbar.html": "<nav>Nav</nav>",
                },
            )
        ],
    )

    tmpl = engine.get_template("main.html")
    tmpl.render(Context({}))

    spans = exporter.get_finished_spans()
    template_spans = [s for s in spans if "django.template" in s.name]
    span_names = [s.name for s in template_spans]

    # main.html and components/navbar.html should be captured
    assert "django.template: main.html" in span_names
    assert "django.template: components/navbar.html" in span_names

    # django/forms/*, admin/widgets/*, debug_toolbar/* should be excluded
    assert "django.template: django/forms/default.html" not in span_names
    assert "django.template: admin/widgets/input.html" not in span_names
    assert "django.template: debug_toolbar/toolbar.html" not in span_names


def test_regex_route_normalization():
    from tp_trace.integrations.django.request import _normalize_route

    class MockResolverMatch:
        def __init__(self, route):
            self.route = route

    class MockRequest:
        def __init__(self, route):
            self.resolver_match = MockResolverMatch(route)
            self.path = "/exams/42/stats/"

    req = MockRequest(r"^exams/(?P<exam_id>\d+)/stats/$")
    assert _normalize_route(req, req.path) == "/exams/<exam_id>/stats/"

    req2 = MockRequest(r"^admin/courses/(?P<slug>[\w-]+)/chapters/$")
    assert _normalize_route(req2, "/admin/courses/math/chapters/") == "/admin/courses/<slug>/chapters/"

    req3 = MockRequest(r"^$")
    assert _normalize_route(req3, "/") == "/"


def test_datadog_style_middleware_waterfall():
    """Verify that class middleware with __call__ and process_request creates nested waterfall spans."""
    from django.core.handlers.wsgi import WSGIHandler
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import tp_trace

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="dd-waterfall-test", exporter=exporter, export_batch=False)

    # Test with configured middlewares (SecurityMiddleware and CommonMiddleware)
    h = WSGIHandler()
    h.load_middleware()

    factory = RequestFactory()
    req = factory.get("/test/sample/", HTTP_USER_AGENT="TestBrowser/1.0", HTTP_X_DEVICE_ID="device-999")
    resp = h.get_response(req)
    assert resp.status_code == 200

    spans = exporter.get_finished_spans()
    span_names = {s.name: s for s in spans}

    # Verify SecurityMiddleware.__call__ and process_request
    sec_call = span_names.get("django.middleware.security.SecurityMiddleware.__call__")
    sec_req = span_names.get("django.middleware.security.SecurityMiddleware.process_request")
    assert sec_call is not None, f"SecurityMiddleware.__call__ span must exist, got: {list(span_names.keys())}"
    assert sec_req is not None, f"SecurityMiddleware.process_request span must exist, got: {list(span_names.keys())}"
    assert sec_req.parent.span_id == sec_call.context.span_id, "process_request must be child of __call__"

    # Verify CommonMiddleware is downstream child of SecurityMiddleware
    com_call = span_names.get("django.middleware.common.CommonMiddleware.__call__")
    assert com_call is not None, f"CommonMiddleware.__call__ span must exist, got: {list(span_names.keys())}"
    assert com_call.parent.span_id == sec_call.context.span_id, "downstream middleware must be child of outer __call__"


def test_view_cbv_multilevel_nesting():
    """Verify multi-level nesting of CBVs: view -> View.setup -> dispatch -> get."""
    from django.core.handlers.wsgi import WSGIHandler
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import tp_trace
    from tp_trace.integrations import get_integration_manager

    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="cbv-multilevel-test", exporter=exporter, export_batch=False)
    get_integration_manager().apply_integrations()

    h = WSGIHandler()
    h.load_middleware()
    factory = RequestFactory()
    req = factory.get("/test/profile/")
    resp = h.get_response(req)
    assert resp.status_code == 200

    spans = exporter.get_finished_spans()
    span_names = {s.name: s for s in spans}

    view_span = next((s for s in spans if "ProfileView" in s.name and "dispatch" not in s.name and "setup" not in s.name), None)
    dispatch_span = next((s for s in spans if s.name.endswith(".dispatch")), None)
    get_span = next((s for s in spans if s.name.endswith(".get")), None)

    assert view_span is not None, f"Expected ProfileView span, got: {list(span_names.keys())}"
    assert dispatch_span is not None, f"Expected dispatch span, got: {list(span_names.keys())}"
    assert get_span is not None, f"Expected get handler span, got: {list(span_names.keys())}"

    # Nesting hierarchy: get handler is child of dispatch
    assert get_span.parent.span_id == dispatch_span.context.span_id
    # dispatch is child of view span
    assert dispatch_span.parent.span_id == view_span.context.span_id


def test_drf_viewset_action_tracing():
    """Verify DRF ViewSet action routing maps action to handler span name (e.g. fake_view_set.list)."""
    from django.core.handlers.wsgi import WSGIHandler
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import tp_trace
    from tp_trace.integrations import get_integration_manager

    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="viewset-action-test", exporter=exporter, export_batch=False)
    get_integration_manager().apply_integrations()

    h = WSGIHandler()
    h.load_middleware()
    factory = RequestFactory()
    req = factory.get("/api/orders/")
    resp = h.get_response(req)
    assert resp.status_code == 200

    spans = exporter.get_finished_spans()
    span_names = {s.name: s for s in spans}

    view_span = next((s for s in spans if s.name == "django.view.FakeViewSet.list"), None)
    dispatch_span = next((s for s in spans if s.name.endswith(".dispatch")), None)
    list_span = next((s for s in spans if s.name.endswith(".list")), None)

    assert view_span is not None, f"Expected django.view.FakeViewSet.list, got: {list(span_names.keys())}"
    assert dispatch_span is not None, f"Expected dispatch span, got: {list(span_names.keys())}"
    assert list_span is not None, f"Expected .list action span, got: {list(span_names.keys())}"
    assert list_span.name.endswith("fake_view_set.list")

    # Nesting hierarchy: action span must be child of dispatch
    assert list_span.parent.span_id == dispatch_span.context.span_id
    assert dispatch_span.parent.span_id == view_span.context.span_id


def test_template_query_nesting_waterfall():
    """Verify database queries executed inside template rendering nest under django.template: <name>."""
    from contextlib import nullcontext
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from django.template import Template, Context
    from django.db.backends.utils import CursorWrapper
    import tp_trace

    class _MockRawCursor:
        def __init__(self, rowcount=2):
            self.rowcount = rowcount
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            return self

    class _MockDatabaseConnection:
        def __init__(self, alias="default", db_name="shop_db"):
            self.alias = alias
            self.vendor = "postgresql"
            self.execute_wrappers = []
            self.wrap_database_errors = nullcontext()
            self.settings_dict = {"NAME": db_name, "HOST": "localhost", "PORT": 5432, "USER": "postgres"}

        def validate_no_broken_transaction(self):
            pass

    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="template-nesting-test", exporter=exporter, export_batch=False)
    from tp_trace.integrations import get_integration_manager
    mgr = get_integration_manager()
    mgr.apply_integrations()

    raw_cursor = _MockRawCursor(rowcount=2)
    mock_db = _MockDatabaseConnection(alias="default", db_name="shop_db")
    cursor_wrapper = CursorWrapper(raw_cursor, mock_db)

    # Function executed during template evaluation (simulating lazy queryset in template)
    def lazy_query():
        cursor_wrapper.execute("SELECT id, name FROM items WHERE category = 'books'")
        return "Book Items"

    t = Template("<div>{{ get_items }}</div>")
    t.name = "catalog/items_list.html"
    rendered = t.render(Context({"get_items": lazy_query}))
    assert "Book Items" in rendered

    spans = exporter.get_finished_spans()
    template_span = next((s for s in spans if "catalog/items_list.html" in s.name), None)
    db_span = next((s for s in spans if s.attributes.get("db.system") == "postgresql"), None)

    assert template_span is not None, "Template span must be created"
    assert db_span is not None, "DB span must be created"

    # Datadog Parity Nesting: DB query executed during template render MUST have template span as parent!
    assert db_span.parent.span_id == template_span.context.span_id
    assert db_span.context.trace_id == template_span.context.trace_id


def test_concurrent_template_rendering_thread_safety():
    """Verify that multiple concurrent threads rendering the same template instance all generate spans."""
    import concurrent.futures
    from django.template import Template, Context
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="template-thread-safety-test", exporter=exporter, export_batch=False)

    shared_template = Template("<div>Hello {{ name }}</div>")
    shared_template.name = "shared/greeting.html"

    num_threads = 8
    def render_worker(i):
        return shared_template.render(Context({"name": f"User_{i}"}))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        results = list(executor.map(render_worker, range(num_threads)))

    assert len(results) == num_threads
    for i, res in enumerate(results):
        assert f"User_{i}" in res

    spans = exporter.get_finished_spans()
    template_spans = [s for s in spans if "shared/greeting.html" in s.name]
    # Every thread must have successfully traced the template render without dropping spans
    assert len(template_spans) == num_threads


def test_traceparent_header_respects_sampling_decision():
    """Verify that W3C traceparent header reflects the head-based sampling decision (01 vs 00)."""
    from django.core.handlers.wsgi import WSGIHandler
    from django.test import RequestFactory
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    factory = RequestFactory()

    # 1. Sampled request (sample_rate=1.0) -> trace_flags should be 01
    exporter1 = InMemorySpanExporter()
    tp_trace.init(project_name="sample-test-on", exporter=exporter1, sample_rate=1.0, export_batch=False)
    handler = WSGIHandler()
    handler.load_middleware()
    req1 = factory.get("/test/sample/")
    res1 = handler.get_response(req1)
    assert res1.status_code == 200
    assert "traceparent" in res1
    flags1 = int(res1["traceparent"].split("-")[-1], 16)
    assert flags1 & 1 == 1, f"Expected sampled flag bit 1 to be set, got {res1['traceparent']}"

    tp_trace._reset_for_testing()

    # 2. Dropped request (sample_rate=0.0) -> trace_flags bit 0 should be 0 (unsampled)
    exporter2 = InMemorySpanExporter()
    tp_trace.init(project_name="sample-test-off", exporter=exporter2, sample_rate=0.0, export_batch=False)
    handler2 = WSGIHandler()
    handler2.load_middleware()
    req2 = factory.get("/test/sample/")
    res2 = handler2.get_response(req2)
    assert res2.status_code == 200
    assert "traceparent" in res2
    flags2 = int(res2["traceparent"].split("-")[-1], 16)
    assert flags2 & 1 == 0, f"Expected unsampled flag bit 0 to be cleared, got {res2['traceparent']}"


def test_drf_view_dispatch_reentrancy_single_span():
    """Verify that nested dispatch calls (e.g. DRF APIView calling super().dispatch) emit only 1 dispatch span."""
    from tp_trace.integrations.django.view import traced_view_dispatch

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="dispatch-reentrant-test", exporter=exporter, export_batch=False)

    class ParentView(View):
        def dispatch(self, request, *args, **kwargs):
            return HttpResponse("parent done")

    class ChildDRFView(ParentView):
        def dispatch(self, request, *args, **kwargs):
            # Calls ParentView.dispatch through traced_view_dispatch wrapper
            wrapped_parent = lambda *a, **k: super(ChildDRFView, self).dispatch(*a, **k)
            return traced_view_dispatch(wrapped_parent, self, (request,) + args, kwargs)

    view_inst = ChildDRFView()
    factory = RequestFactory()
    req = factory.get("/test/drf/")

    wrapped_child = lambda *a, **k: view_inst.dispatch(*a, **k)
    response = traced_view_dispatch(wrapped_child, view_inst, (req,), {})
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    dispatch_spans = [s for s in spans if "dispatch" in s.name]
    assert len(dispatch_spans) == 1, f"Expected exactly 1 dispatch span, got {[s.name for s in dispatch_spans]}"


def test_cbv_traced_view_dispatch_prevents_dict_pollution():
    """Verify traced_view_dispatch cleans up instance __dict__ and does not leave bound methods."""
    from tp_trace.integrations.django.view import traced_view_dispatch

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="dict-pollution-test", exporter=exporter, export_batch=False)

    class SampleCBV(View):
        def get(self, request, *args, **kwargs):
            return HttpResponse("ok")

    view_inst = SampleCBV()
    # Before dispatch, 'get' is defined on the class, not in instance __dict__
    assert "get" not in view_inst.__dict__

    factory = RequestFactory()
    req = factory.get("/test/cbv/")

    wrapped = lambda *a, **k: view_inst.dispatch(*a, **k)
    response = traced_view_dispatch(wrapped, view_inst, (req,), {})
    assert response.status_code == 200

    # After dispatch, 'get' must NOT remain in instance __dict__
    assert "get" not in view_inst.__dict__



def test_unmatched_route_does_not_poison_child_spans():
    """Verify that unresolvable routes never publish __unmatched__ to current_route or child spans."""
    from tp_trace.route_context import get_current_route
    from tp_trace.integrations.django.request import traced_get_response

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="unmatched-poison-test", exporter=exporter, export_batch=False)

    factory = RequestFactory()
    req = factory.get("/non-existent/random/path/")

    observed_routes = []

    def dummy_handler(request):
        # In-flight child span check
        observed_routes.append(get_current_route())
        with tp_trace.traced_span("child.db.query"):
            pass
        return HttpResponse("not found", status=404)

    response = traced_get_response(dummy_handler, None, (req,), {})
    assert response.status_code == 404

    # Current route in flight must NOT have been "__unmatched__"
    assert observed_routes == [None], f"Expected None in flight, got {observed_routes}"

    spans = exporter.get_finished_spans()
    child_span = next(s for s in spans if s.name == "child.db.query")
    assert child_span.attributes.get("http.route") is None, (
        f"Child span should not inherit __unmatched__, got {child_span.attributes.get('http.route')}"
    )


def test_route_enriching_span_processor_on_end_normalizes_readable_span():
    """Verify RouteEnrichingSpanProcessor.on_end mutates ReadableSpan._attributes directly."""
    from tp_trace.route_context import RouteEnrichingSpanProcessor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = RouteEnrichingSpanProcessor()
    provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("unnormalized_span") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.statement", "SELECT * FROM auth_user")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    finished = spans[0]
    assert finished.attributes.get("normalized.service") == "postgres"
    assert finished.attributes.get("normalized.operation") == "SELECT * FROM auth_user"









def test_institute_subdomain_propagated_to_child_spans():
    """Verify institute.subdomain set during request is propagated to all child spans."""
    from django.test import RequestFactory
    from django.http import HttpResponse
    from tp_trace.integrations.django.request import traced_get_response
    import tp_trace

    class DummyInstitute:
        subdomain = "demo"

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="institute-test", exporter=exporter, export_batch=False)

    factory = RequestFactory()
    req = factory.get("/api/v3/featured-updates/")
    req.institute = DummyInstitute()

    def dummy_handler(request):
        with tp_trace.traced_span("child.db.query"):
            pass
        with tp_trace.traced_span("child.redis.cmd"):
            pass
        return HttpResponse("ok", status=200)

    resp = traced_get_response(dummy_handler, None, (req,), {})
    assert resp.status_code == 200

    spans = exporter.get_finished_spans()
    db_span = next(s for s in spans if s.name == "child.db.query")
    redis_span = next(s for s in spans if s.name == "child.redis.cmd")
    server_span = next(s for s in spans if s.kind == SpanKind.SERVER)

    assert db_span.attributes.get("institute.subdomain") == "demo"
    assert db_span.attributes.get("institute") == "demo"
    assert redis_span.attributes.get("institute.subdomain") == "demo"
    assert redis_span.attributes.get("institute") == "demo"
    assert server_span.attributes.get("institute.subdomain") == "demo"
    assert server_span.attributes.get("institute") == "demo"


def test_response_and_request_remain_picklable_for_cache_middleware():
    """Verify that traced request and TemplateResponse remain picklable.

    Django's cache_page and UpdateCacheMiddleware serialize responses using pickle
    (e.g., with django_redis). Stashing raw Span objects containing _thread.RLock
    on request or response breaks caching with 'TypeError: cannot pickle _thread.RLock object'.
    """
    import pickle
    from django.template.response import SimpleTemplateResponse
    from tp_trace.integrations.django.request import _prime_request
    from tp_trace.integrations.django.view import _bind_response_parent
    from opentelemetry.trace import get_tracer

    tracer = get_tracer("test.pickle")
    with tracer.start_as_current_span("server.req") as span:
        factory = RequestFactory()
        req = factory.get("/api/v2.3/exams/trial-exam/languages/")

        _prime_request(span, req)

        # Ensure request has trace IDs but no raw span object
        assert hasattr(req, "trace_id")
        assert not hasattr(req, "_tp_span")

        resp = SimpleTemplateResponse("dummy.html")
        resp._request = req
        resp.content = b'{"languages":["en","hi"]}'

        _bind_response_parent(resp)

        # Ensure response has no raw unpicklable span attribute
        assert not hasattr(resp, "_tp_parent_span")

        # Must pickle and unpickle cleanly
        pickled = pickle.dumps(resp)
        unpickled = pickle.loads(pickled)
        assert unpickled.content == b'{"languages":["en","hi"]}'


def test_auth_patching_defers_via_when_imported(monkeypatch):
    """Verify that DjangoIntegration._patch_auth registers when_imported if not in sys.modules."""
    import sys
    from tp_trace.integrations.django import DjangoIntegration

    integration = DjangoIntegration()
    registered_hooks = []

    def fake_when_imported(mod_name, callback):
        registered_hooks.append((mod_name, callback))

    monkeypatch.setattr(integration, "when_imported", fake_when_imported)

    # Simulate django.contrib.auth NOT in sys.modules
    orig_auth = sys.modules.pop("django.contrib.auth", None)
    try:
        integration._patch_auth()
        assert any(mod == "django.contrib.auth" for mod, _ in registered_hooks)
    finally:
        if orig_auth is not None:
            sys.modules["django.contrib.auth"] = orig_auth


def test_middleware_import_string_contextvar_safety():
    """Verify that handler_base.import_string uses ContextVar and does not race or flip globally."""
    import django.core.handlers.base as handler_base
    from tp_trace.integrations.django import DjangoIntegration
    from tp_trace.integrations.django.middleware import (
        _patch_handler_base_import_string,
        unpatch_handler_base_import_string,
        _active_middleware_integration,
    )

    integration = DjangoIntegration()
    _patch_handler_base_import_string()
    try:
        assert getattr(handler_base, "_tp_import_string_patched", False)

        # When active_middleware_integration is None, import_string returns unmodified
        orig_func = handler_base.import_string("django.utils.deprecation.MiddlewareMixin")
        assert orig_func is not None

        # When active, callable factories get wrapped
        token = _active_middleware_integration.set(integration)
        try:
            wrapped_func = handler_base.import_string("django.utils.deprecation.MiddlewareMixin")
            # MiddlewareMixin is a class/type, not a factory function, so it passes through
            assert wrapped_func is not None
        finally:
            _active_middleware_integration.reset(token)
    finally:
        unpatch_handler_base_import_string()


def test_cache_classes_deferred_patching(monkeypatch):
    """Verify that third-party cache backends (django_redis) are deferred via when_imported if not loaded."""
    import sys
    from tp_trace.integrations.django import DjangoIntegration

    integration = DjangoIntegration()
    registered_hooks = []

    def fake_when_imported(mod_name, callback):
        registered_hooks.append(mod_name)

    monkeypatch.setattr(integration, "when_imported", fake_when_imported)

    # Remove django_redis.cache if present
    orig_redis = sys.modules.pop("django_redis.cache", None)
    try:
        integration._patch_cache_classes()
        assert "django_redis.cache" in registered_hooks
        assert "django_redis.client.default" in registered_hooks
    finally:
        if orig_redis is not None:
            sys.modules["django_redis.cache"] = orig_redis


def test_unmaterialized_simple_lazy_object_user_not_evaluated():
    """Verify that un-materialized SimpleLazyObject on request.user is not forced to evaluate."""
    from django.utils.functional import SimpleLazyObject
    from django.test import RequestFactory
    from django.http import HttpResponse
    from tp_trace.integrations.django.request import traced_get_response
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import tp_trace

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="lazy-test", exporter=exporter, export_batch=False)

    evaluated = False

    def load_user():
        nonlocal evaluated
        evaluated = True
        return None

    factory = RequestFactory()
    req = factory.get("/api/v1/public/")
    req.user = SimpleLazyObject(load_user)

    def dummy_handler(request):
        return HttpResponse("ok", status=200)

    resp = traced_get_response(dummy_handler, None, (req,), {})
    assert resp.status_code == 200
    # Must NOT have been evaluated!
    assert not evaluated, "request.user was prematurely materialized by SDK telemetry!"
