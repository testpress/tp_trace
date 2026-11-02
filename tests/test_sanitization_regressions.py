"""Sensitive data must not escape through SQL, URL, or cache metadata."""

import pytest

import tp_trace
from tp_trace.sanitize import normalize_sql_for_metric, sanitize_sql, sanitize_url


@pytest.mark.parametrize("mode", [None, "expose", "redact", "strip"])
def test_malformed_url_never_returns_userinfo(mode):
    assert sanitize_url("http://admin:secret@[bad-host]:99999/path?token=private", mode=mode) == "<unparseable-url>"


@pytest.mark.parametrize(
    "query, expected",
    [
        ("SELECT /* it's a comment */ * FROM users WHERE secret = 'private'", "SELECT * FROM users WHERE secret = ?"),
        ("SELECT -- it's a comment\n 'private'", "SELECT ?"),
        ("SELECT /* outer /* 'nested' */ secret */ 'private'", "SELECT ?"),
        ("SELECT /* multiline\n private */ 'value'", "SELECT ?"),
        ("SELECT '/* not a comment */ -- private', 'secret'", "SELECT ?, ?"),
        ("SELECT 'Price: $tag$abc'", "SELECT ?"),
        (r"SELECT E'John\'s notes', 'private'", "SELECT E?, ?"),
        ("SELECT $tag$it's /* private */$tag$, 'secret'", "SELECT ?, ?"),
        ("SELECT \"odd'column\", 'private'", 'SELECT "odd\'column", ?'),
        ("SELECT 'it''s private', 'secret'", "SELECT ?, ?"),
    ],
)
def test_sql_lexical_boundaries(query, expected):
    assert sanitize_sql(query) == expected
    assert normalize_sql_for_metric(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        r"SELECT 'John\'s notes' AND password = 'private'",
        "SELECT 'unterminated private",
        "SELECT /* unterminated private",
        "SELECT $tag$unterminated private",
        'SELECT "unterminated identifier',
    ],
)
def test_ambiguous_or_unterminated_sql_fails_closed(query):
    assert sanitize_sql(query) == "<unparseable-sql>"
    assert normalize_sql_for_metric(query) == "<unparseable-sql>"


def test_sql_pretruncation_inside_literal_fails_closed():
    assert sanitize_sql("SELECT '" + "private" * 100, max_length=20) == "<unparseable-sql>"


@pytest.mark.parametrize("redis_backend", [False, True])
@pytest.mark.parametrize("keyword", [False, True])
@pytest.mark.parametrize("masked", [False, True])
def test_cache_set_many_records_keys_without_values(redis_backend, keyword, masked):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from tp_trace.integrations.django.cache import make_traced_cache_op

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="cache-regression", exporter=exporter, export_batch=False, redis_mask_command=masked)

    class Cache:
        pass

    if redis_backend:
        Cache.__module__ = "django_redis.cache"
    data = {"session:123": "private-token", "session:456": "private-payload"}
    result = []
    calls = []

    def set_many(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    args, kwargs = ((), {"data": data}) if keyword else ((data,), {})
    assert make_traced_cache_op("set_many")(set_many, Cache(), args, kwargs) is result
    assert calls == [(args, kwargs)]
    span = exporter.get_finished_spans()[0]
    assert span.attributes["django.cache.key"] == "session:123, session:456"
    if masked or not redis_backend:
        assert "private-token" not in str(dict(span.attributes))
        assert "private-payload" not in str(dict(span.attributes))
    else:
        # Full Redis command exposure remains the documented opt-in masking default.
        assert "private-token" in span.attributes["db.statement"]
