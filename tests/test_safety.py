"""Tests for the instrumentation transparency invariant.

tp_trace must never:

  1. prevent application execution (``pre``),
  2. execute application code more than once (``once``),
  3. alter the application's result or exception (``post``).

These tests deliberately break *telemetry* to prove the application is
unaffected. A span that merely looks correct is not evidence of any of
the three properties.
"""

import pytest

import tp_trace
from tp_trace.integrations.base import BaseIntegration
from tp_trace.safety import UNTRACED, attempt, safe_set_attribute
from tp_trace.tracing import traced_span


class _FakeIntegration(BaseIntegration):
    name = "fake"

    def is_installed(self) -> bool:
        return True

    def _apply_patch(self) -> None:
        pass


def _boom(*a, **k):
    raise RuntimeError("telemetry bug")


# --------------------------------------------------------------------------
# The safety module itself
# --------------------------------------------------------------------------


def test_attempt_returns_default_instead_of_raising():
    assert attempt(_boom, default="fallback") == "fallback"


def test_attempt_passes_through_success():
    assert attempt(lambda x: x * 2, 21, default=None) == 42


def test_untraced_sentinel_is_falsy_and_singleton():
    from tp_trace.safety import _Untraced

    assert bool(UNTRACED) is False
    assert _Untraced() is UNTRACED


def test_safe_set_attribute_never_raises():
    class BadSpan:
        def set_attribute(self, k, v):
            raise RuntimeError("span is broken")

    safe_set_attribute(BadSpan(), "k", "v")  # must not raise


# --------------------------------------------------------------------------
# Invariant #1 — pre-application telemetry failure must not block the app
# --------------------------------------------------------------------------


def test_pre_app_telemetry_failure_still_runs_statement():
    """A bug in DB span construction must not prevent the query executing."""
    import tp_trace.integrations.postgres.cursor as cur

    tp_trace.init(project_name="pre-app-1", export_batch=False)
    calls = []

    def execute(sql, *a, **k):
        calls.append(sql)
        return 1

    original = cur._build_db_span_context
    cur._build_db_span_context = _boom
    try:
        assert cur.traced_django_cursor_exec(execute, None, ("SELECT 1",), {}) == 1
    finally:
        cur._build_db_span_context = original

    assert len(calls) == 1, "statement must execute even when span building fails"


def test_pre_app_telemetry_failure_still_runs_execute_wrapper():
    import tp_trace.integrations.postgres.cursor as cur

    tp_trace.init(project_name="pre-app-2", export_batch=False)
    calls = []

    def execute(sql, params, many, context):
        calls.append(sql)
        return 1

    class Cur:
        rowcount = 1

    original = cur._build_db_span_context
    cur._build_db_span_context = _boom
    try:
        cur.tp_trace_django_db_execute_wrapper(
            execute, "SELECT 1", None, False, {"cursor": Cur(), "connection": None}
        )
    finally:
        cur._build_db_span_context = original

    assert len(calls) == 1


def test_post_app_telemetry_failure_does_not_change_result():
    """A route-resolution bug must not turn a 200 into a 500."""
    import tp_trace.integrations.django.request as req_mod

    tp_trace.init(project_name="post-app", export_batch=False)

    class Resp:
        status_code = 200

        def __setitem__(self, k, v):
            pass

    class Req:
        path = "/ok/"
        method = "GET"
        META: dict = {}
        headers: dict = {}

        def build_absolute_uri(self):
            return "http://x/ok/"

    original = req_mod._normalize_route
    req_mod._normalize_route = _boom
    try:
        response = req_mod.traced_get_response(lambda *a, **k: Resp(), None, (Req(),), {})
    finally:
        req_mod._normalize_route = original

    assert response.status_code == 200


# --------------------------------------------------------------------------
# Invariant #2 — application code runs exactly once
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO orders (id, total) VALUES (1, 10.00)",
        "UPDATE accounts SET balance = 0 WHERE id = 7",
        "DELETE FROM sessions WHERE expired = 1",
    ],
    ids=["insert", "update", "delete"],
)
def test_failing_write_statement_executes_exactly_once(statement):
    """Write statements are where an accidental retry does the most damage."""
    from tp_trace.integrations.postgres.cursor import traced_django_cursor_exec

    tp_trace.init(project_name="once-write", export_batch=False)
    calls = []

    def execute(sql, *a, **k):
        calls.append(sql)
        raise RuntimeError("statement failed")

    with pytest.raises(RuntimeError, match="statement failed"):
        traced_django_cursor_exec(execute, None, (statement,), {})

    assert len(calls) == 1, f"{statement.split()[0]} executed {len(calls)} times; must run once"


def test_failing_read_statement_executes_exactly_once():
    from tp_trace.integrations.postgres.cursor import traced_django_cursor_exec

    tp_trace.init(project_name="once-read", export_batch=False)
    calls = []

    def execute(sql, *a, **k):
        calls.append(sql)
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        traced_django_cursor_exec(execute, None, ("SELECT 1",), {})

    assert len(calls) == 1


# --------------------------------------------------------------------------
# Invariant #3 — application exceptions propagate unchanged
# --------------------------------------------------------------------------


def test_application_exception_propagates_unchanged():
    class Service:
        calls = 0

        def work(self):
            Service.calls += 1
            raise KeyError("app-level failure")

    svc = Service()
    integ = _FakeIntegration()

    def wrapper(wrapped, instance, args, kwargs):
        with traced_span("span.work"):
            return wrapped(*args, **kwargs)

    integ.wrap(Service, "work", wrapper)
    try:
        with pytest.raises(KeyError, match="app-level failure"):
            svc.work()
        assert Service.calls == 1
    finally:
        integ.unwrap_all()


# --------------------------------------------------------------------------
# Regression: post-application telemetry faults must not corrupt the result
#
# Each of these failed once: a fault after the application call returned still
# propagated to the caller, turning a successful operation into an error.
# --------------------------------------------------------------------------


def test_db_rowcount_fault_preserves_successful_result():
    """A closed/broken cursor raises on ``.rowcount``; the result must survive."""
    from tp_trace.integrations.postgres import cursor as cur

    tp_trace._reset_for_testing()
    tp_trace.init(project_name="rowcount-fault", disabled=True)

    class ClosedCursor:
        @property
        def rowcount(self):
            raise RuntimeError("cursor already closed")

    calls = []

    def execute(sql, params=None):
        calls.append(sql)
        return "RESULT"

    result = cur.traced_django_cursor_exec(execute, ClosedCursor(), ("SELECT 1",), {})

    assert result == "RESULT"
    assert len(calls) == 1


def test_requests_hooks_never_block_outbound_http():
    """OTel calls these hooks *unguarded*; a fault must not stop the request.

    ``tp_trace_request_hook`` runs before ``requests.send``, so raising here
    would block every outbound HTTP call. ``tp_trace_response_hook`` runs
    outside the instrumentor's try/except, so raising there would surface an
    error in the app after the response had already arrived.
    """
    from tp_trace.integrations.requests.client import (
        tp_trace_request_hook,
        tp_trace_response_hook,
    )

    tp_trace._reset_for_testing()
    tp_trace.init(project_name="hook-fault", disabled=True)

    class Span:
        def set_attribute(self, key, value):
            pass

        def set_status(self, *args, **kwargs):
            pass

        def update_name(self, name):
            pass

        def is_recording(self):
            return True

    class BadRequest:
        method = "GET"

        @property
        def url(self):
            raise RuntimeError("url property blew up")

    class BadResponse:
        @property
        def status_code(self):
            raise RuntimeError("status_code blew up")

    # Neither may raise: the instrumentor would not catch it.
    tp_trace_request_hook(Span(), BadRequest())
    tp_trace_response_hook(Span(), BadRequest(), BadResponse())


# --------------------------------------------------------------------------
# Regression: shared telemetry helpers are safe on their own
#
# These run *before* the application call, so a fault in a helper that is only
# guarded at some call sites blocks the app at the others.
# --------------------------------------------------------------------------


def test_apply_tags_survives_config_lookup_failure():
    from tp_trace.integrations.django import view as view_mod

    tp_trace._reset_for_testing()
    tp_trace.init(project_name="tags-fault", disabled=True)

    original = view_mod._get_config
    view_mod._get_config = _raise_value_error
    try:
        calls = []

        def app(*args, **kwargs):
            calls.append(1)
            return "RESPONSE"

        request = type("Request", (), {"path": "/x/", "method": "GET"})()
        assert view_mod.traced_get_response(app, None, (request,), {}) == "RESPONSE"
        assert len(calls) == 1
    finally:
        view_mod._get_config = original


def test_view_dispatch_survives_introspection_failure():
    """A hostile descriptor must not prevent the view from dispatching."""
    from tp_trace.integrations.django import view as view_mod

    tp_trace._reset_for_testing()
    tp_trace.init(project_name="dispatch-fault", disabled=True)

    class HostileView:
        action = "get"

        @property
        def get(self):
            raise RuntimeError("descriptor blew up")

    calls = []

    def dispatch(*args, **kwargs):
        calls.append(1)
        return "DISPATCHED"

    assert view_mod.traced_view_dispatch(dispatch, HostileView(), (object(),), {}) == "DISPATCHED"
    assert len(calls) == 1


def _raise_value_error(*args, **kwargs):
    raise ValueError("telemetry config lookup failed")


# --------------------------------------------------------------------------
# Regression: the SERVER wrapper's pre-application prologue must be guarded
#
# traced_get_response is the only wrapper that hand-rolls its span CM instead of
# using traced_span, so it opts out of the shared degrade path. Every statement
# between span creation and the application call runs *before* the app, so a
# fault in any of them blocks the request. test_django.py covers the happy path
# for the response headers; this proves they are also failure-tolerant.
# --------------------------------------------------------------------------


def test_server_wrapper_survives_hostile_span_context():
    """A span whose get_span_context() blows up must not block the response."""
    import tp_trace.integrations.django.request as req_mod

    tp_trace.init(project_name="span-context-fault", export_batch=False)

    class Resp:
        status_code = 200

        def __setitem__(self, k, v):
            pass

    class Req:
        path = "/ok/"
        method = "GET"
        META: dict = {}
        headers: dict = {}

        def build_absolute_uri(self):
            return "http://x/ok/"

    original = req_mod._prime_request
    req_mod._prime_request = lambda *a, **k: None  # no ids readable
    try:
        response = req_mod.traced_get_response(lambda *a, **k: Resp(), None, (Req(),), {})
    finally:
        req_mod._prime_request = original

    assert response.status_code == 200


def test_server_wrapper_survives_read_only_trace_id():
    """A request refusing the trace_id write must still return a response.

    The trace ids come from the span, not the request, so a request that
    rejects the write loses its correlation attrs but keeps the traceparent
    header and, above all, the application response.
    """
    import tp_trace.integrations.django.request as req_mod

    tp_trace.init(project_name="read-only-fault", export_batch=False)

    class Resp:
        status_code = 200
        headers: dict = {}

        def __setitem__(self, k, v):
            self.headers[k] = v

    class ReadOnlyReq:
        """Writes to trace_id fail, the way a frozen or slotted request would."""

        path = "/ok/"
        method = "GET"
        META: dict = {}
        headers: dict = {}

        def build_absolute_uri(self):
            return "http://x/ok/"

        def __setattr__(self, name, value):
            if name in ("trace_id", "span_id", "_tp_span"):
                raise AttributeError(f"{name} is read-only")
            object.__setattr__(self, name, value)

    calls = []

    def app(*a, **k):
        calls.append(1)
        return Resp()

    req = ReadOnlyReq()
    response = req_mod.traced_get_response(app, None, (req,), {})

    assert response.status_code == 200
    assert len(calls) == 1
    # Header injection is independent of the request binding and must survive it.
    assert "traceparent" in response.headers
    assert response.headers["X-Trace-ID"]


def test_server_span_records_exception_exactly_once():
    """One failure must produce one exception event, not two.

    This wrapper records the exception itself on the error path, so OTel's
    automatic handling is disabled. Without record_exception=False the same
    exception lands on the span twice (and set_status runs twice), which
    double-counts errors in anything reading exception events.
    """
    import tp_trace.integrations.django.request as req_mod
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="single-exception", exporter=exporter, export_batch=False)

    class Resp:
        status_code = 200

        def __setitem__(self, k, v):
            pass

    class Req:
        path = "/boom/"
        method = "GET"
        META: dict = {}
        headers: dict = {}

        def build_absolute_uri(self):
            return "http://x/boom/"

    def app(*a, **k):
        raise ValueError("handler exploded")

    with pytest.raises(ValueError):
        req_mod.traced_get_response(app, None, (Req(),), {})

    exception_events = [
        e for s in exporter.get_finished_spans() for e in s.events if e.name == "exception"
    ]
    assert len(exception_events) == 1


def test_django_native_wrapper_rowcount_fault_preserves_result():
    """The ``execute_wrapper`` seam gets its cursor from ``context``, not the instance."""
    from tp_trace.integrations.postgres import cursor as cur

    tp_trace._reset_for_testing()
    tp_trace.init(project_name="native-rowcount-fault", disabled=True)

    class ClosedCursor:
        @property
        def rowcount(self):
            raise RuntimeError("cursor already closed")

    calls = []

    def execute(sql, params, many, context):
        calls.append(sql)
        return "RESULT"

    context = {"connection": object(), "cursor": ClosedCursor()}
    result = cur.tp_trace_django_db_execute_wrapper(
        execute, "SELECT 1", None, False, context
    )

    assert result == "RESULT"
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Regression: the error path of traced_span itself
#
# ``traced_span``'s handler is the only thing between an application exception
# and the caller. An unguarded telemetry call there replaces the app's
# exception with its own — corrupting error handling on every traced span.
# --------------------------------------------------------------------------


def test_traced_span_preserves_app_exception_when_telemetry_fails():
    import tp_trace.tracing as tracing_mod

    class HostileSpan:
        """A span whose enrichment blows up, as a broken exporter could."""

        def is_recording(self):
            return True

        def record_exception(self, exc):
            pass

        def set_status(self, *args, **kwargs):
            pass

        @property
        def status(self):
            return type("Status", (), {"status_code": None})()

        def set_attribute(self, *args, **kwargs):
            raise RuntimeError("telemetry blew up")

    class HostileTracer:
        def start_as_current_span(self, *args, **kwargs):
            return self

        def __enter__(self):
            return HostileSpan()

        def __exit__(self, *exc_info):
            return False

    original = tracing_mod.get_tracer
    tracing_mod.get_tracer = lambda *a, **k: HostileTracer()
    try:
        with pytest.raises(KeyError, match="app-level failure"):
            with traced_span("svc.work"):
                raise KeyError("app-level failure")
    finally:
        tracing_mod.get_tracer = original


def test_sanitize_sql_dollar_quoted_strings():
    from tp_trace.sanitize import sanitize_sql
    assert sanitize_sql("SELECT $$alice@example.com$$") == "SELECT ?"
    assert sanitize_sql("SELECT $tag$super_secret$tag$ FROM table") == "SELECT ? FROM table"
    # Unclosed dollar quote fails closed
    assert sanitize_sql("SELECT $$unclosed string") == "<unparseable-sql>"
    assert sanitize_sql("SELECT $tag$unclosed string") == "<unparseable-sql>"


def test_sanitize_url_fail_closed():
    from tp_trace.sanitize import sanitize_url
    # Normal url sanitize strips sensitive query param
    assert "token=REDACTED" in sanitize_url("https://api.example.com/v1?token=secret123")

    # Broken unparseable URL should fail closed and not leak query strings
    class MalformedURL:
        def __str__(self):
            return "https://example.com/path?token=secret#hash"

    # Mock urlparse raising an exception
    import urllib.parse
    orig = urllib.parse.urlparse
    urllib.parse.urlparse = lambda *a, **k: (_ for _ in ()).throw(ValueError("malformed URL"))
    try:
        sanitized = sanitize_url(MalformedURL())
        assert "secret" not in sanitized
        assert sanitized == "https://example.com/path"
    finally:
        urllib.parse.urlparse = orig


def test_sanitize_url_query_modes():
    """Query-mode matrix: expose keeps raw, redact masks credential keys, strip drops query."""
    from tp_trace.sanitize import sanitize_url

    url = "https://user:pass@api.example.com/v1?token=secret&page=2"
    # Userinfo is stripped in every mode
    assert sanitize_url(url, mode="expose") == "https://api.example.com/v1?token=secret&page=2"
    assert sanitize_url(url, mode="redact") == "https://api.example.com/v1?token=REDACTED&page=2"
    assert sanitize_url(url, mode="strip") == "https://api.example.com/v1"
    # Unknown modes fail closed to redaction
    assert sanitize_url(url, mode="bogus") == "https://api.example.com/v1?token=REDACTED&page=2"
    # Backwards compatible defaults (mode=None): strip_query still honored
    assert sanitize_url(url) == "https://api.example.com/v1?token=REDACTED&page=2"
    assert sanitize_url(url, strip_query=True) == "https://api.example.com/v1"


def test_normalize_sql_for_metric_collapses_in_clauses():
    from tp_trace.sanitize import normalize_sql_for_metric

    q1 = "SELECT * FROM users WHERE id IN (%s, %s, %s, %s)"
    assert normalize_sql_for_metric(q1) == "SELECT * FROM users WHERE id IN (?)"

    q2 = "SELECT * FROM orders WHERE status NOT IN (?, ?, ?)"
    assert normalize_sql_for_metric(q2) == "SELECT * FROM orders WHERE status NOT IN (?)"


def test_normalize_sql_for_metric_collapses_batch_inserts():
    from tp_trace.sanitize import normalize_sql_for_metric

    q = "INSERT INTO products (name, price) VALUES (%s, %s), (%s, %s), (%s, %s)"
    assert normalize_sql_for_metric(q) == "INSERT INTO products (name, price) VALUES (...)"


def test_normalize_sql_for_metric_strips_comments():
    from tp_trace.sanitize import normalize_sql_for_metric

    q = "SELECT /* route:api/users */ id, name FROM users WHERE id = %s"
    assert normalize_sql_for_metric(q) == "SELECT id, name FROM users WHERE id = %s"


def test_normalize_sql_for_metric_bounds_length():
    from tp_trace.sanitize import normalize_sql_for_metric

    columns = ", ".join([f'"col_{i}"' for i in range(100)])
    long_query = f"SELECT {columns} FROM my_very_large_table WHERE id = %s"
    result = normalize_sql_for_metric(long_query, max_length=256)
    assert len(result) <= 256
    assert result.endswith("...")


def test_attempt_getattr_missing_attribute_does_not_log_error(caplog):
    import logging

    class Dummy:
        pass

    with caplog.at_level(logging.DEBUG, logger="tp_trace.safety"):
        val = attempt(getattr, Dummy(), "nonexistent", default=None, _label="dummy.nonexistent")

    assert val is None
    assert "tp_trace: telemetry step dummy.nonexistent failed" not in caplog.text


def test_wrap_application_exception_does_not_log_internal_wrapper_error(caplog):
    import logging

    integ = _FakeIntegration()

    class DummyService:
        def raise_app_error(self):
            raise KeyError("missing item")

    def dummy_wrapper(wrapped, instance, args, kwargs):
        return wrapped(*args, **kwargs)

    obj = DummyService()
    integ.wrap(obj, "raise_app_error", dummy_wrapper)

    with caplog.at_level(logging.DEBUG, logger="tp_trace.integrations"):
        with pytest.raises(KeyError, match="missing item"):
            obj.raise_app_error()

    assert "tp_trace internal wrapper error" not in caplog.text
    integ.unwrap_all()
