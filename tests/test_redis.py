"""Unit tests for Redis Integration (official RedisInstrumentor)."""

import pytest
from unittest.mock import MagicMock, patch
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

import tp_trace
from tp_trace.integrations.redis import RedisIntegration


@pytest.fixture(autouse=True)
def clean_sdk():
    tp_trace._reset_for_testing()
    yield
    tp_trace._reset_for_testing()


@pytest.fixture
def memory_exporter():
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-redis-service",
        environment="test",
        exporter=exporter,
        export_batch=False,
    )
    return exporter


def test_redis_integration_manager_registration():
    from tp_trace.integrations import get_integration_manager

    mgr = get_integration_manager()
    integ = mgr._registered_classes["redis"]
    assert integ == "tp_trace.integrations.redis.RedisIntegration"
    resolved = mgr._resolve_class(integ)
    assert resolved is RedisIntegration


def test_django_redis_cache_tracing(memory_exporter):
    """Verify that django_redis cache backends emit django_redis.cache.<op> spans with db.system='redis'."""
    from opentelemetry.trace import SpanKind
    from tp_trace.integrations.django.cache import make_traced_cache_op

    class FakeRedisCache:
        __module__ = "django_redis.cache"

        def get(self, key):
            return "cached_tenant"

    cache = FakeRedisCache()
    traced_get = make_traced_cache_op("get")

    result = traced_get(lambda k: cache.get(k), cache, ("tenant:subdomain.com",), {})
    assert result == "cached_tenant"

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    # Span name must have redis prefix and CLIENT kind
    assert span.name == "🔸 django_redis.cache.get"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["django.cache.operation"] == "get"
    assert span.attributes["django.cache.key"] == "tenant:subdomain.com"
    assert span.attributes["django.cache.backend"] == "FakeRedisCache"
    assert span.attributes["django.cache.hit"] is True
    assert span.attributes["db.system"] == "redis"
    assert span.attributes["db.system.name"] == "redis"
    assert span.attributes["db.operation"] == "get"
    assert span.attributes["db.operation.name"] == "get"
    assert span.attributes["db.statement"] == "GET tenant:subdomain.com"
    assert span.attributes["db.query.text"] == "GET tenant:subdomain.com"
    assert "cache.system" not in span.attributes
    assert "peer.service" not in span.attributes


def test_cache_span_sets_redis_db_system(memory_exporter):
    """Redis cache spans must set db.system='redis' so the collector and APM dashboards identify Redis."""
    from opentelemetry.trace import SpanKind
    from tp_trace.integrations.django.cache import make_traced_cache_op

    class FakeRedisCache:
        __module__ = "django_redis.cache"
        _server = "redis://127.0.0.1:6379/1"

        def get(self, key):
            return "v"

    traced_get = make_traced_cache_op("get")
    traced_get(lambda k: FakeRedisCache().get(k), FakeRedisCache(), ("k",), {})

    span = memory_exporter.get_finished_spans()[0]
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["db.system"] == "redis"
    assert span.attributes["db.system.name"] == "redis"
    assert span.attributes["db.operation"] == "get"
    assert span.attributes["db.operation.name"] == "get"
    assert span.attributes["db.statement"] == "GET k"
    assert span.attributes["db.query.text"] == "GET k"
    assert "cache.system" not in span.attributes
    assert "peer.service" not in span.attributes
    assert span.attributes["server.address"] == "127.0.0.1"
    assert span.attributes["server.port"] == 6379
    assert span.attributes["django.cache.operation"] == "get"
    assert span.attributes["django.cache.backend"] == "FakeRedisCache"


def test_non_redis_cache_not_misclassified(memory_exporter):
    """Verify that a non-Redis backend with 'redis' in module name (e.g. myapp.redis_helpers) is not misclassified."""
    from opentelemetry.trace import SpanKind
    from tp_trace.integrations.django.cache import make_traced_cache_op

    class HelperCache:
        __module__ = "myapp.redis_helpers"

        def get(self, key):
            return "ok"

    cache = HelperCache()
    traced_get = make_traced_cache_op("get")
    result = traced_get(lambda k: cache.get(k), cache, ("test_key",), {})
    assert result == "ok"

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "django.cache.get"
    assert spans[0].kind == SpanKind.INTERNAL
    assert spans[0].attributes["django.cache.backend"] == "HelperCache"
    assert "db.system" not in spans[0].attributes
    assert "peer.service" not in spans[0].attributes



def test_redis_integration_apply_patch_and_uninstrument(monkeypatch):
    """Verify RedisIntegration instruments and uninstruments cleanly."""
    import sys
    import types

    dummy_redis = types.ModuleType("redis")
    dummy_redis.VERSION = (2, 9, 0)
    dummy_redis.__path__ = []
    dummy_redis.Redis = MagicMock
    dummy_redis.StrictRedis = MagicMock
    monkeypatch.setitem(sys.modules, "redis", dummy_redis)

    import importlib
    otel_redis = importlib.import_module("opentelemetry.instrumentation.redis")

    integ = RedisIntegration()
    assert integ.is_installed() is True

    with patch.object(otel_redis, "RedisInstrumentor") as mock_inst_cls:
        mock_inst = MagicMock()
        mock_inst.is_instrumented_by_opentelemetry = False
        mock_inst_cls.return_value = mock_inst

        assert integ.instrument() is True
        assert integ._instrumented is True
        mock_inst.instrument.assert_called_once()

        # Uninstrument
        mock_inst.is_instrumented_by_opentelemetry = True
        assert integ.uninstrument() is True
        assert integ._instrumented is False
        mock_inst.uninstrument.assert_called_once()


@pytest.fixture
def live_redis_client():
    """Real redis client against localhost:6379/db=2; skips when unavailable."""
    redis = pytest.importorskip("redis")
    client = redis.Redis(host="127.0.0.1", port=6379, db=2)
    try:
        client.ping()
    except Exception:
        pytest.skip("no local redis server on 127.0.0.1:6379")
    yield client
    try:
        client.connection_pool.disconnect()
    except Exception:
        pass


def test_redis_request_hook_prefixes_command_span():
    """Verify _tp_trace_redis_request_hook updates command span with redis: prefix."""
    from tp_trace.integrations.redis.integration import _tp_trace_redis_request_hook

    mock_span = MagicMock()
    mock_span.name = "GET"
    mock_span.is_recording.return_value = True

    _tp_trace_redis_request_hook(mock_span, None, ("GET", "key"), {})
    mock_span.update_name.assert_called_once_with("🔸 GET")
    set_attrs = {call[0][0]: call[0][1] for call in mock_span.set_attribute.call_args_list}
    assert set_attrs.get("db.system") == "redis"
    assert set_attrs.get("db.system.name") == "redis"
    assert set_attrs.get("db.operation") == "GET"
    assert set_attrs.get("db.operation.name") == "GET"
    assert set_attrs.get("db.statement") == "GET key"
    assert set_attrs.get("db.query.text") == "GET key"
    assert "cache.system" not in set_attrs
    assert "peer.service" not in set_attrs


def test_redis_request_hook_exposes_endpoint_by_default():
    """Default posture: the hook must NOT overwrite the real net.peer.name/port."""
    from tp_trace.integrations.redis.integration import _tp_trace_redis_request_hook

    mock_span = MagicMock()
    mock_span.name = "GET"
    mock_span.is_recording.return_value = True

    _tp_trace_redis_request_hook(mock_span, None, ("GET", "key"), {})
    set_attrs = {call[0][0]: call[0][1] for call in mock_span.set_attribute.call_args_list}
    assert "net.peer.name" not in set_attrs
    assert "net.peer.port" not in set_attrs


def test_redis_request_hook_masks_endpoint_when_enabled():
    """TP_TRACE_REDIS_MASK_ENDPOINT=true restores the old "?" masking."""
    from tp_trace.integrations.redis.integration import _tp_trace_redis_request_hook

    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-redis-mask",
        exporter=exporter,
        export_batch=False,
        redis_mask_endpoint=True,
    )

    mock_span = MagicMock()
    mock_span.name = "GET"
    mock_span.is_recording.return_value = True

    _tp_trace_redis_request_hook(mock_span, None, ("GET", "key"), {})
    set_attrs = {call[0][0]: call[0][1] for call in mock_span.set_attribute.call_args_list}
    assert set_attrs["net.peer.name"] == "?"
    assert set_attrs["net.peer.port"] == "?"


def test_redis_format_command_args_exposes_full_command():
    """The formatter exposes command + args (bytes decoded), like dd-trace raw_command."""
    from tp_trace.integrations.redis.integration import _tp_trace_format_command_args

    assert _tp_trace_format_command_args(("GET", "users:123")) == "GET users:123"
    assert _tp_trace_format_command_args(("SET", "users:123", b"bala")) == "SET users:123 bala"
    assert _tp_trace_format_command_args(("SET", "k", "bala 🚀")) == "SET k bala 🚀"
    assert _tp_trace_format_command_args(()) == ""


def test_redis_format_command_args_truncates_at_max_length():
    """Commands longer than TP_TRACE_REDIS_CMD_MAX_LENGTH truncate with '...'."""
    from tp_trace.integrations.redis.integration import _tp_trace_format_command_args

    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-redis-truncate",
        exporter=exporter,
        export_batch=False,
        redis_cmd_max_length=20,
    )

    out = _tp_trace_format_command_args(("SET", "k", "v" * 100))
    assert len(out) == 20
    assert out.endswith("...")
    assert out == "SET k " + "v" * 11 + "..."

    # Large binary payloads (e.g., 100KB) are pre-truncated safely
    large_bytes = b"\x00\x01\x02" * 30000
    out_bytes = _tp_trace_format_command_args(("SET", "bin_k", large_bytes))
    assert len(out_bytes) == 20
    assert out_bytes.endswith("...")


def test_redis_format_command_args_masks_when_enabled():
    """TP_TRACE_REDIS_MASK_COMMAND=true reproduces OTel's 'COMMAND ? ?' reduction."""
    from tp_trace.integrations.redis.integration import _tp_trace_format_command_args

    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-redis-mask-cmd",
        exporter=exporter,
        export_batch=False,
        redis_mask_command=True,
    )

    assert _tp_trace_format_command_args(("SET", "k", "v")) == "SET ? ?"
    assert _tp_trace_format_command_args(("GET", "users:123")) == "GET ?"


def test_redis_format_command_args_redacts_credentials():
    """Sensitive tokens (AUTH/CONFIG/HELLO/MIGRATE/ACL) are masked to [REDACTED]."""
    from tp_trace.integrations.redis.integration import _tp_trace_format_command_args as fmt

    # Plain commands are untouched.
    assert fmt(("GET", "users:123")) == "GET users:123"
    assert fmt(("SET", "k", "v")) == "SET k v"

    # AUTH <password> and AUTH <user> <password>.
    assert fmt(("AUTH", "hunter2")) == "AUTH [REDACTED]"
    assert fmt(("AUTH", "bob", "hunter2")) == "AUTH bob [REDACTED]"

    # CONFIG SET requirepass/masterauth.
    assert fmt(("CONFIG", "SET", "requirepass", "s3cret")) == "CONFIG SET requirepass [REDACTED]"
    assert fmt(("CONFIG", "SET", "masterauth", "s3cret")) == "CONFIG SET masterauth [REDACTED]"
    # Non-credential CONFIG keys are preserved.
    assert fmt(("CONFIG", "SET", "maxmemory", "100mb")) == "CONFIG SET maxmemory 100mb"

    # HELLO <ver> AUTH <user> <password>.
    assert fmt(("HELLO", "3", "AUTH", "bob", "hunter2")) == "HELLO 3 AUTH bob [REDACTED]"

    # MIGRATE ... AUTH <password> / AUTH2 <user> <password>.
    assert fmt(("MIGRATE", "h", "p", "k", "0", "5000", "AUTH", "pw")).endswith("AUTH [REDACTED]")
    assert fmt(("MIGRATE", "h", "p", "k", "0", "5000", "AUTH2", "bob", "pw")).endswith("AUTH2 bob [REDACTED]")

    # ACL SETUSER <name> >password.
    assert fmt(("ACL", "SETUSER", "bob", ">hunter2")) == "ACL SETUSER bob >[REDACTED]"


def test_redis_format_command_args_fallback_on_exception():
    """If formatting raises, degrade to str(args[0]) rather than blanking db.statement."""
    from tp_trace.integrations.redis.integration import _tp_trace_format_command_args

    class FaultyArg:
        def __str__(self):
            raise RuntimeError("broken stringify")

    # Command name is valid, second arg raises in stringify
    res = _tp_trace_format_command_args(("SET", FaultyArg()))
    assert res == "SET"

    # If even the command name raises, return "" safely
    class BrokenCommand:
        def __str__(self):
            raise RuntimeError("fatal command")

    assert _tp_trace_format_command_args((BrokenCommand(),)) == ""


def test_redis_request_hook_full_contract_deterministic():
    """Deterministic (no live server needed): verify APM attributes contract."""
    from tp_trace.integrations.redis.integration import _tp_trace_redis_request_hook

    tp_trace.init(
        project_name="test-redis-contract",
        exporter=InMemorySpanExporter(),
        export_batch=False,
    )

    mock_span = MagicMock()
    mock_span.name = "HSET"
    mock_span.is_recording.return_value = True

    _tp_trace_redis_request_hook(mock_span, None, ("HSET", "users:1", "name", "bala"), {})
    mock_span.update_name.assert_called_once_with("🔸 HSET")

    attrs = {call[0][0]: call[0][1] for call in mock_span.set_attribute.call_args_list}
    assert attrs["db.system"] == "redis"
    assert attrs["db.system.name"] == "redis"
    assert attrs["db.operation"] == "HSET"
    assert attrs["db.operation.name"] == "HSET"
    assert attrs["db.statement"] == "HSET users:1 name bala"
    assert attrs["db.query.text"] == "HSET users:1 name bala"
    assert attrs["normalized.service"] == "redis"
    assert attrs["normalized.operation"] == "redis.hset"
    # Endpoints are preserved by default (not masked)
    assert "net.peer.name" not in attrs
    assert "net.peer.port" not in attrs


def test_redis_deterministic_span_preserves_endpoint_and_db_index():
    """Deterministic check that span attributes (net.peer.*, db.redis.database_index)
    remain intact and are not cleared by tp_trace hooks."""
    from tp_trace.integrations.redis.integration import _tp_trace_redis_request_hook

    tp_trace.init(
        project_name="test-redis-preserve",
        exporter=InMemorySpanExporter(),
        export_batch=False,
        redis_mask_endpoint=False,
    )

    span_attrs = {
        "net.peer.name": "127.0.0.1",
        "net.peer.port": 6379,
        "db.redis.database_index": 2,
    }
    mock_span = MagicMock()
    mock_span.name = "GET"
    mock_span.is_recording.return_value = True

    def mock_set_attr(k, v):
        span_attrs[k] = v

    mock_span.set_attribute.side_effect = mock_set_attr

    _tp_trace_redis_request_hook(mock_span, None, ("GET", "key"), {})

    assert span_attrs["net.peer.name"] == "127.0.0.1"
    assert span_attrs["net.peer.port"] == 6379
    assert span_attrs["db.redis.database_index"] == 2
    assert span_attrs["db.system"] == "redis"
    assert span_attrs["normalized.service"] == "redis"


def test_redis_deterministic_masking_toggles():
    """Deterministic check: when mask_endpoint is True, net.peer.* attributes are masked to '?'."""
    from tp_trace.integrations.redis.integration import _tp_trace_redis_request_hook, _tp_trace_format_command_args

    tp_trace.init(
        project_name="test-redis-masking",
        exporter=InMemorySpanExporter(),
        export_batch=False,
        redis_mask_endpoint=True,
        redis_mask_command=True,
    )

    span_attrs = {
        "net.peer.name": "127.0.0.1",
        "net.peer.port": 6379,
    }
    mock_span = MagicMock()
    mock_span.name = "SET"
    mock_span.is_recording.return_value = True

    def mock_set_attr(k, v):
        span_attrs[k] = v

    mock_span.set_attribute.side_effect = mock_set_attr

    _tp_trace_redis_request_hook(mock_span, None, ("SET", "key", "val"), {})
    assert span_attrs["net.peer.name"] == "?"
    assert span_attrs["net.peer.port"] == "?"
    assert _tp_trace_format_command_args(("SET", "key", "val")) == "SET ? ?"


def test_redis_live_span_exposes_endpoint_command_and_db_index(memory_exporter, live_redis_client):
    """Real command: db.statement carries the full command, the real host/port,
    and db.redis.database_index stays exposed (regression pin)."""
    key = "tp_trace:test:users:123"
    live_redis_client.set(key, "bala")
    live_redis_client.get(key)

    spans = [
        s for s in memory_exporter.get_finished_spans()
        if s.kind == SpanKind.CLIENT and s.attributes.get("db.system") == "redis"
    ]
    get_span = next(s for s in spans if s.attributes.get("db.statement") == f"GET {key}")
    set_span = next(s for s in spans if s.attributes.get("db.statement") == f"SET {key} bala")

    for span in (get_span, set_span):
        assert span.attributes["net.peer.name"] == "127.0.0.1"
        assert span.attributes["net.peer.port"] == 6379
        assert span.attributes["db.redis.database_index"] == 2

    live_redis_client.delete(key)


def test_redis_live_pipeline_span_exposes_full_commands(memory_exporter, live_redis_client):
    """Pipeline spans (request hook never runs) also carry full per-command text."""
    k1, k2 = "tp_trace:test:pipe1", "tp_trace:test:pipe2"
    pipe = live_redis_client.pipeline(transaction=False)
    pipe.get(k1)
    pipe.set(k2, "x")
    pipe.execute()

    pipe_span = next(
        s for s in memory_exporter.get_finished_spans()
        if s.kind == SpanKind.CLIENT and "db.redis.pipeline_length" in s.attributes
    )
    statement = pipe_span.attributes["db.statement"]
    assert f"GET {k1}" in statement
    assert f"SET {k2} x" in statement

    live_redis_client.delete(k1, k2)


def test_redis_live_masking_toggles_restore_old_behavior(live_redis_client):
    """Both mask toggles on: full old behavior ('?' endpoint + 'COMMAND ? ?')."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-redis-mask-live",
        exporter=exporter,
        export_batch=False,
        redis_mask_endpoint=True,
        redis_mask_command=True,
    )

    key = "tp_trace:test:mask"
    live_redis_client.set(key, "v")

    span = next(
        s for s in exporter.get_finished_spans()
        if s.kind == SpanKind.CLIENT and s.attributes.get("db.system") == "redis"
    )
    assert span.attributes["db.statement"] == "SET ? ?"
    assert span.attributes["net.peer.name"] == "?"
    assert span.attributes["net.peer.port"] == "?"

    live_redis_client.delete(key)


def test_redis_formatter_wiring_and_restore(memory_exporter):
    """Deterministic (no redis server needed): the full-command formatter is
    patched into BOTH OTel module namespaces (execute + pipeline paths) and
    the stock reducer is restored on uninstrument."""
    import importlib
    import tp_trace.integrations.redis.integration as redis_intg
    from tp_trace.integrations import get_integration_manager
    from tp_trace.integrations.redis.integration import _tp_trace_format_command_args

    # Skip on the real client package: an earlier test may have cached the OTel
    # instrumentation module in sys.modules against a stubbed `redis` module.
    pytest.importorskip("redis")
    otel_pkg = importlib.import_module("opentelemetry.instrumentation.redis")
    otel_util = importlib.import_module("opentelemetry.instrumentation.redis.util")

    integ = get_integration_manager().get("redis")
    assert integ is not None and integ._instrumented

    assert otel_pkg._format_command_args is _tp_trace_format_command_args
    assert otel_util._format_command_args is _tp_trace_format_command_args

    original = redis_intg._otel_original_format_command_args
    assert original is not None and original is not _tp_trace_format_command_args

    # Uninstrument restores the stock reducer in both namespaces...
    assert integ.uninstrument() is True
    assert otel_pkg._format_command_args is original
    assert otel_util._format_command_args is original

    # ...and re-instrumenting reinstalls it (leave suite state consistent).
    assert integ.instrument() is True
    assert otel_pkg._format_command_args is _tp_trace_format_command_args
    assert otel_util._format_command_args is _tp_trace_format_command_args


def test_redis_cache_ops_never_leak_cached_values(live_redis_client):
    """Regression pin for the cache decision: cache operations produce only
    django_redis.cache.* spans; the redis driver span (and therefore
    db.statement with cached values) is suppressed during cache ops."""
    from opentelemetry.trace import SpanKind
    from tp_trace.integrations.django.cache import make_traced_cache_op

    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="test-redis-cache-leak",
        exporter=exporter,
        export_batch=False,
    )

    key = "tp_trace:test:cache-secret"
    secret = "super-secret-cached-value"

    class FakeDjangoRedisCache:
        __module__ = "django_redis.cache"

        def __init__(self, client):
            self.client = client

        def set(self, k, v):
            return self.client.set(k, v)

        def get(self, k):
            return self.client.get(k)

    cache = FakeDjangoRedisCache(live_redis_client)
    traced_set = make_traced_cache_op("set")
    traced_get = make_traced_cache_op("get")

    traced_set(lambda k, v: cache.set(k, v), cache, (key, secret), {})
    assert traced_get(lambda k: cache.get(k), cache, (key,), {}) is not None

    spans = exporter.get_finished_spans()
    # Only the two cache spans exist -- no raw redis driver span at all
    assert sorted(s.name for s in spans) == [
        "🔸 django_redis.cache.get",
        "🔸 django_redis.cache.set",
    ]
    set_span = next(s for s in spans if s.name == "🔸 django_redis.cache.set")
    get_span = next(s for s in spans if s.name == "🔸 django_redis.cache.get")
    assert set_span.attributes["db.statement"] == f"SET {key} {secret}"
    assert get_span.attributes["db.statement"] == f"GET {key}"
    for s in spans:
        assert s.kind == SpanKind.CLIENT

    live_redis_client.delete(key)


def test_cache_suppresses_downstream_driver_instrumentation():
    """Verify that during cache operation execution, downstream OTel instrumentors are suppressed."""
    from opentelemetry.instrumentation.utils import is_instrumentation_enabled
    from tp_trace.integrations.django.cache import make_traced_cache_op

    instrumentation_state_during_call = []

    class FakeRedisCache:
        __module__ = "django_redis.cache"

        def get(self, key):
            instrumentation_state_during_call.append(is_instrumentation_enabled())
            return "val"

    traced_get = make_traced_cache_op("get")
    traced_get(lambda k: FakeRedisCache().get(k), FakeRedisCache(), ("k",), {})

    assert len(instrumentation_state_during_call) == 1
    assert instrumentation_state_during_call[0] is False  # Suppressed!


def test_cache_set_many_suppresses_pipeline_driver_instrumentation(memory_exporter):
    """Verify that during set_many pipeline execution, downstream driver spans are suppressed."""
    from tp_trace.integrations.django.cache import make_traced_cache_op, _in_cache_span
    from tp_trace.integrations.redis.integration import _redis_suppress_guard

    class DummyPipeline:
        def execute(self):
            return ["OK", "OK"]

    called = []
    def dummy_execute(pipe):
        called.append(True)
        return pipe.execute()

    wrapped_execute = lambda *args, **kwargs: _redis_suppress_guard(DummyPipeline.execute, DummyPipeline(), args, kwargs)

    pipe = DummyPipeline()
    token = _in_cache_span.set(True)
    try:
        res = _redis_suppress_guard(pipe.execute, pipe, (), {})
        assert res == ["OK", "OK"]
    finally:
        _in_cache_span.reset(token)


def test_redis_spans_do_not_inherit_django_http_route_as_operation(memory_exporter):
    """Verify that Redis pipeline and command spans never inherit Django HTTP routes as normalized.operation."""
    from tp_trace.route_context import current_route, current_method
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind
    from tp_trace.integrations.redis.integration import _tp_trace_redis_response_hook

    tracer = trace.get_tracer("test")
    r_token = current_route.set("/api/v2.4/courses/")
    m_token = current_method.set("GET")
    try:
        # Pipeline span
        with tracer.start_as_current_span("SET SET", kind=SpanKind.CLIENT) as span:
            span.set_attribute("db.system", "redis")
            span.set_attribute("db.redis.pipeline_length", 2)
            _tp_trace_redis_response_hook(span, None, ["OK", "OK"])

        # Standalone command span
        with tracer.start_as_current_span("🔸 GET", kind=SpanKind.CLIENT) as span:
            span.set_attribute("db.system", "redis")
            span.set_attribute("db.operation", "get")

        spans = memory_exporter.get_finished_spans()
        assert len(spans) == 2

        pipe_span = spans[0]
        assert pipe_span.attributes["normalized.service"] == "redis"
        assert pipe_span.attributes["normalized.operation"] == "redis.pipeline"
        assert "http.method" not in pipe_span.attributes
        assert pipe_span.attributes.get("django.route") == "/api/v2.4/courses/"

        cmd_span = spans[1]
        assert cmd_span.attributes["normalized.service"] == "redis"
        assert cmd_span.attributes["normalized.operation"] == "redis.get"
        assert "http.method" not in cmd_span.attributes
        assert cmd_span.attributes.get("django.route") == "/api/v2.4/courses/"
    finally:
        current_route.reset(r_token)
        current_method.reset(m_token)


