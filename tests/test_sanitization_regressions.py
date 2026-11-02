"""Sensitive data must not escape through SQL, URL, or cache metadata."""

import pytest

import tp_trace
from tp_trace.sanitize import normalize_sql_for_metric, sanitize_sql, sanitize_url


@pytest.mark.parametrize("mode", [None, "expose", "redact", "strip"])
def test_malformed_url_never_returns_userinfo(mode):
    # An unmatched IPv6 bracket raises on Python 3.8 too; hostname validation
    # for matched brackets differs between supported Python versions.
    assert sanitize_url("http://admin:secret@[bad-host/path?token=private", mode=mode) == "<unparseable-url>"


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


def test_sql_pretruncation_inside_literal_keeps_only_safe_prefix():
    assert sanitize_sql("SELECT '" + "private" * 100, max_length=20) == "SELECT ... [truncated]"


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


@pytest.mark.parametrize("columns", [100, 200])
def test_wide_django_select_keeps_bounded_redacted_fingerprint(columns):
    """A length limit inside a quoted identifier must not look like invalid SQL."""
    from tp_trace.integrations.postgres.cursor import _build_db_span_context

    fields = ", ".join('"users_user"."profile_field_{}"'.format(i) for i in range(columns))
    query = "SELECT " + fields + ' FROM "users_user" WHERE "email" = \'private@example.test\' AND "id" = 42'
    fingerprint = normalize_sql_for_metric(query)
    assert fingerprint.startswith('SELECT "users_user".')
    assert len(fingerprint) <= 256
    _, attrs, tracker = _build_db_span_context(query, None)
    assert attrs["db.statement"] == fingerprint
    assert attrs["normalized.operation"] == fingerprint
    assert attrs["db.query.summary"] != "<unparseable-sql>"
    assert tracker.startswith('SELECT "users_user".')
    assert len(tracker) <= 4096
    for value in [fingerprint, tracker, attrs["db.statement.full"]]:
        assert "private@example.test" not in value
        assert "= 42" not in value


@pytest.mark.parametrize(
    "tail",
    [
        '"long_identifier' + "x" * 20000 + '" FROM users',
        "'" + "private" * 4000 + "' FROM users",
        "$body$" + "private" * 4000 + "$body$ FROM users",
        "/* " + "private" * 4000 + " */ id FROM users",
    ],
)
def test_valid_sql_exceeding_scan_budget_keeps_safe_prefix(tail):
    query = "SELECT id, " + tail
    for result in [sanitize_sql(query), normalize_sql_for_metric(query)]:
        assert result.startswith("SELECT id,")
        assert "<unparseable-sql>" not in result
        assert "private" not in result
        assert len(result) <= 4096 + len(" ... [truncated]")


@pytest.mark.parametrize(
    "query,expected",
    [
        (r"SELECT U&'d\0061t\+000061', 'private'", "SELECT U&?, ?"),
        (r"SELECT u&'d\0061t''private' UESCAPE '!'", "SELECT u&? UESCAPE ?"),
        ("SELECT $данные$it's private$данные$", "SELECT ?"),
    ],
)
def test_postgresql_unicode_literals_are_redacted_without_fallback(query, expected):
    assert sanitize_sql(query) == expected
    assert normalize_sql_for_metric(query) == expected
