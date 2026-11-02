"""Django telemetry must preserve responses and clean up request context."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import tp_trace
from tp_trace.integrations.django import request as req_mod
from tp_trace.integrations.django.view import traced_get_response
from tp_trace.route_context import get_current_route, get_current_view, get_current_tags, current_db_tracker


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="django-safety", exporter=exporter, export_batch=False)
    return exporter


@pytest.mark.parametrize("method", [None, 17, "get"])
@pytest.mark.parametrize("app_fails", [False, True])
def test_view_method_metadata_preserves_application(exporter, method, app_fails):
    request = SimpleNamespace(method=method, path="/ok/", resolver_match=None)
    result = object()
    error = ValueError("application error")
    calls = []

    def wrapped(*args):
        calls.append(args)
        if app_fails:
            raise error
        return result

    if app_fails:
        with pytest.raises(ValueError) as caught:
            traced_get_response(wrapped, None, (request,), {})
        assert caught.value is error
    else:
        assert traced_get_response(wrapped, None, (request,), {}) is result
        assert exporter.get_finished_spans()[0].attributes["http.request.method"] == "GET"
    assert calls == [(request,)]


@pytest.mark.parametrize("method,status", [(None, 200), (17, "200"), ("get", "503"), ("GET", None)])
def test_response_hook_normalizes_metadata_and_resets_context(exporter, method, status):
    from django.test import RequestFactory
    from django.http import HttpResponse

    request = RequestFactory().get("/ok/")
    response = HttpResponse("original response")
    response.status_code = status
    with trace.get_tracer("test").start_as_current_span("request") as span:
        req_mod._tp_trace_django_request_hook(span, request)
        request.method = method
        req_mod._tp_trace_django_response_hook(span, request, response)
    assert response.content == b"original response"
    assert response.status_code == status
    assert exporter.get_finished_spans()[-1].attributes["http.request.method"] == "GET"
    assert exporter.get_finished_spans()[-1].attributes["http.response.status_code"] == (int(status) if status else 200)
    assert get_current_route() is None
    assert get_current_view() is None
    assert current_db_tracker.get() is None
    assert get_current_tags() == {}
    assert not hasattr(request, "_tp_request_context")


def test_response_hook_enrichment_failure_still_cleans_up(exporter, monkeypatch):
    from django.test import RequestFactory
    from django.http import HttpResponse

    request = RequestFactory().get("/ok/")
    response = HttpResponse("ok")
    with trace.get_tracer("test").start_as_current_span("request") as span:
        req_mod._tp_trace_django_request_hook(span, request)
        monkeypatch.setattr(req_mod, "sanitize_url", Mock(side_effect=RuntimeError("telemetry failed")))
        req_mod._tp_trace_django_response_hook(span, request, response)
    assert response.content == b"ok"
    assert current_db_tracker.get() is None
    assert get_current_route() is None
    assert get_current_view() is None
    assert get_current_tags() == {}
    assert not hasattr(request, "_tp_request_context")


def test_response_hook_cleanup_failure_does_not_skip_other_cleanup(exporter, monkeypatch):
    request = SimpleNamespace(_tp_request_context=(object(), object(), object(), None, None))
    cleanups = {}
    for name in ("reset_request_db_tracker", "reset_request_view", "reset_request_route", "reset_request_tags"):
        cleanups[name] = Mock(side_effect=RuntimeError("cleanup failed"))
        monkeypatch.setattr(req_mod, name, cleanups[name])
    req_mod._tp_trace_django_response_hook(None, request, None)
    for cleanup in cleanups.values():
        cleanup.assert_called_once()
    assert not hasattr(request, "_tp_request_context")


def test_response_hook_hostile_context_property_never_raises(exporter):
    class Request:
        @property
        def _tp_request_context(self):
            raise RuntimeError("context lookup failed")

    req_mod._tp_trace_django_response_hook(None, Request(), None)
