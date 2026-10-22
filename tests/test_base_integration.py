"""Tests for the BaseIntegration wrapping seam.

The seam's contract: wrapping an application method must never change the
number of times that method executes. A previous implementation treated any
wrapper exception as a telemetry fault and re-invoked the wrapped function,
which silently doubled side effects for every application error.
"""

import pytest

import tp_trace
from tp_trace.integrations.base import BaseIntegration
from tp_trace.tracing import traced_span


class _FakeIntegration(BaseIntegration):
    name = "fake"

    def is_installed(self) -> bool:
        return True

    def _apply_patch(self) -> None:
        pass


@pytest.fixture
def integration():
    tp_trace.init(project_name="wrap-seam-test", export_batch=False)
    integ = _FakeIntegration()
    yield integ
    integ.uninstrument()


def _wrap_as_trace(integration, obj, method):
    """Wrap using the same shape every real integration uses."""

    def _wrapper(wrapped, instance, args, kwargs):
        with traced_span(f"span.{method}", attributes={"k": "v"}) as span:
            result = wrapped(*args, **kwargs)
            span.set_attribute("post", "processing")
            return result

    integration.wrap(obj, method, _wrapper)


def test_failing_method_runs_exactly_once(integration):
    class Service:
        calls = 0

        def do_work(self, x):
            Service.calls += 1
            raise ValueError("business failure")

    svc = Service()
    _wrap_as_trace(integration, svc, "do_work")

    with pytest.raises(ValueError, match="business failure"):
        svc.do_work(1)

    assert Service.calls == 1, f"app code ran {Service.calls} times; must run once"


def test_successful_method_runs_exactly_once(integration):
    class Service:
        calls = 0

        def do_work(self, x):
            Service.calls += 1
            return x * 2

    svc = Service()
    _wrap_as_trace(integration, svc, "do_work")

    assert svc.do_work(21) == 42
    assert Service.calls == 1


def test_exception_is_not_swallowed(integration):
    """A host exception must propagate, not be absorbed by the safety net."""

    class Service:
        def do_work(self):
            raise KeyError("missing")

    svc = Service()
    _wrap_as_trace(integration, svc, "do_work")

    with pytest.raises(KeyError):
        svc.do_work()


def test_unwrap_restores_original(integration):
    """Uninstrumentation still restores the unwrapped method.

    Real integrations pass the *class* as the wrap target, so that is the
    shape exercised here.
    """

    class Service:
        def do_work(self):
            return "ok"

    original = Service.do_work

    def _wrapper(wrapped, instance, args, kwargs):
        with traced_span("span.do_work"):
            return wrapped(*args, **kwargs)

    integration.wrap(Service, "do_work", _wrapper)
    assert Service.do_work is not original, "method should be wrapped"

    integration.unwrap_all()
    assert Service.do_work is original, "unwrap_all should restore the original"
    assert Service().do_work() == "ok"


def test_traced_span_degrades_when_provider_unusable(monkeypatch):
    """traced_span must run the body once even if span creation explodes.

    This is what makes removing the retry safe: telemetry failure is absorbed
    here, so an exception escaping a wrapper is the application's own.
    """
    import tp_trace.tracing as tracing_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("tracer provider unavailable")

    monkeypatch.setattr(tracing_mod, "get_tracer", _boom)

    calls = []

    with traced_span("degraded") as span:
        calls.append(1)
        # The yielded span must still be safe to enrich.
        span.set_attribute("a", "b")
        span.update_name("renamed")

    assert calls == [1], "body must run exactly once when span creation fails"


def test_traced_span_body_exception_propagates_once(monkeypatch):
    """With a working provider, a body exception still runs the body once."""
    tp_trace.init(project_name="body-exc", export_batch=False)
    calls = []

    with pytest.raises(RuntimeError, match="app bug"):
        with traced_span("boom"):
            calls.append(1)
            raise RuntimeError("app bug")

    assert calls == [1]


def test_alias_opt_out_disables_canonical_integration():
    """Verify that opting out via an alias (e.g. psycopg2=False) disables the canonical integration."""
    from tp_trace.integrations.manager import IntegrationManager

    mgr = IntegrationManager()

    # Pass psycopg2=False as kwarg override
    instrumented = mgr.apply_integrations(psycopg2=False)
    assert "postgres" not in instrumented
    assert "psycopg2" not in instrumented
    assert "postgresql" not in instrumented

    mgr.uninstrument_all()
