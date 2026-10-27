"""Unit tests for Phase 1: SDK scaffold, config, and OTel bootstrap."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

import tp_trace
from tp_trace.config import SDKConfig
from tp_trace.sanitize import normalize_sql_for_metric, sanitize_sql, sanitize_url


@pytest.fixture(autouse=True)
def clean_sdk_state():
    """Ensure clean SDK state before and after each test."""
    tp_trace._reset_for_testing()
    yield
    tp_trace._reset_for_testing()


def test_init_creates_tracer_provider():
    """Verify init() sets up TracerProvider with correct Resource attributes."""
    exporter = InMemorySpanExporter()
    provider = tp_trace.init(
        project_name="test-service",
        environment="staging",
        version="1.2.3",
        resource_attributes={"custom.tag": "value123"},
        exporter=exporter,
        export_batch=False,
    )

    assert provider is not None
    assert trace.get_tracer_provider() == provider

    resource_attrs = provider.resource.attributes
    assert resource_attrs["service.name"] == "test-service"
    assert resource_attrs["deployment.environment"] == "staging"
    assert resource_attrs["service.version"] == "1.2.3"
    assert resource_attrs["telemetry.sdk.name"] == "tp_trace"
    assert resource_attrs["telemetry.sdk.language"] == "python"
    assert resource_attrs["custom.tag"] == "value123"


def test_init_idempotency():
    """Verify that calling init() multiple times returns the same provider."""
    provider1 = tp_trace.init(project_name="first-service")
    provider2 = tp_trace.init(project_name="second-service")
    assert provider1 is provider2


def test_config_from_env_and_kwargs(monkeypatch):
    """Verify SDKConfig properly resolves env vars and kwargs overrides."""
    monkeypatch.setenv("OTEL_PROJECT_NAME", "env-service")
    monkeypatch.setenv("OTEL_ENVIRONMENT", "production")
    monkeypatch.setenv("OTEL_SERVICE_VERSION", "2.0.0")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "api-key=secret123,team=infra")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.5")

    # When kwargs are omitted, env vars are used
    cfg1 = SDKConfig.from_env_and_kwargs()
    assert cfg1.project_name == "env-service"
    assert cfg1.environment == "production"
    assert cfg1.version == "2.0.0"
    assert cfg1.endpoint == "http://otel-collector:4318"
    assert cfg1.headers == {"api-key": "secret123", "team": "infra"}
    assert cfg1.sample_rate == 0.5

    # Kwargs override env vars
    cfg2 = SDKConfig.from_env_and_kwargs(
        project_name="override-service",
        environment="dev",
        sample_rate=0.8,
        headers={"team": "product"},
    )
    assert cfg2.project_name == "override-service"
    assert cfg2.environment == "dev"
    assert cfg2.sample_rate == 0.8
    assert cfg2.headers["team"] == "product"
    assert cfg2.headers["api-key"] == "secret123"


def test_metrics_export_interval_default_and_overrides(monkeypatch):
    for name in (
        "TP_TRACE_METRICS_EXPORT_INTERVAL_MILLIS",
        "OTEL_METRIC_EXPORT_INTERVAL",
    ):
        monkeypatch.delenv(name, raising=False)

    assert SDKConfig.from_env_and_kwargs().metrics_export_interval_millis == 60000

    monkeypatch.setenv("TP_TRACE_METRICS_EXPORT_INTERVAL_MILLIS", "15000")
    assert SDKConfig.from_env_and_kwargs().metrics_export_interval_millis == 15000
    assert SDKConfig.from_env_and_kwargs(metrics_export_interval_millis=30000).metrics_export_interval_millis == 30000


def test_legacy_tp_dog_env_vars_are_ignored(monkeypatch):
    """Verify retired TP_DOG_* environment variables no longer influence configuration."""
    canonical_names = (
        "TP_TRACE_PROJECT_NAME",
        "TP_TRACE_PROJECT",
        "TP_TRACE_SERVICE_NAME",
        "TP_TRACE_CLUSTER_NAME",
        "TP_TRACE_CLUSTER",
        "TP_TRACE_ENV",
        "TP_TRACE_ENVIRONMENT",
        "TP_TRACE_VERSION",
        "TP_TRACE_ENDPOINT",
        "TP_TRACE_TRACES_ENDPOINT",
        "TP_TRACE_METRICS_ENDPOINT",
        "TP_TRACE_METRICS_ENABLED",
        "TP_TRACE_METRICS_EXPORT_INTERVAL_MILLIS",
        "TP_TRACE_HEADERS",
        "TP_TRACE_SAMPLE_RATE",
        "TP_TRACE_DISABLED",
        "TP_TRACE_DEBUG",
        "TP_TRACE_TAGS",
        "TP_TRACE_IGNORE_ENDPOINTS",
        "TP_TRACE_ENDPOINT_SAMPLE_RULES",
        "TP_TRACE_TRUSTED_PROXIES",
        "TP_TRACE_DB_ROLE_MAP",
        "TP_TRACE_AUTO_PATCH",
        "TP_TRACE_EXTRACT_TRACE_CONTEXT",
        "TP_TRACE_CACHE_ENABLED",
        "TP_TRACE_TEMPLATE_EXCLUDE",
        "TP_TRACE_QUERY_MODE",
        "TP_TRACE_REDIS_MASK_ENDPOINT",
        "TP_TRACE_REDIS_MASK_COMMAND",
        "TP_TRACE_REDIS_CMD_MAX_LENGTH",
        "OTEL_SERVICE_NAME",
        "OTEL_PROJECT_NAME",
        "OTEL_CLUSTER_NAME",
        "OTEL_ENVIRONMENT",
        "OTEL_SERVICE_VERSION",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_METRIC_EXPORT_INTERVAL",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_TRACES_SAMPLER_ARG",
        "OTEL_SDK_DISABLED",
        "OTEL_LOG_LEVEL",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_PROPAGATORS",
    )
    for name in canonical_names:
        monkeypatch.delenv(name, raising=False)

    legacy_names = (
        "TP_DOG_PROJECT_NAME",
        "TP_DOG_PROJECT",
        "TP_DOG_SERVICE_NAME",
        "TP_DOG_CLUSTER_NAME",
        "TP_DOG_CLUSTER",
        "TP_DOG_ENV",
        "TP_DOG_ENVIRONMENT",
        "TP_DOG_VERSION",
        "TP_DOG_ENDPOINT",
        "TP_DOG_TRACES_ENDPOINT",
        "TP_DOG_METRICS_ENDPOINT",
        "TP_DOG_METRICS_ENABLED",
        "TP_DOG_METRICS_EXPORT_INTERVAL_MILLIS",
        "TP_DOG_HEADERS",
        "TP_DOG_SAMPLE_RATE",
        "TP_DOG_DISABLED",
        "TP_DOG_DEBUG",
        "TP_DOG_TAGS",
        "TP_DOG_IGNORE_ENDPOINTS",
        "TP_DOG_ENDPOINT_SAMPLE_RULES",
        "TP_DOG_TRUSTED_PROXIES",
        "TP_DOG_DB_ROLE_MAP",
        "TP_DOG_AUTO_PATCH",
        "TP_DOG_EXTRACT_TRACE_CONTEXT",
        "TP_DOG_CACHE_ENABLED",
        "TP_DOG_TEMPLATE_EXCLUDE",
        "TP_DOG_QUERY_MODE",
        "TP_DOG_REDIS_MASK_ENDPOINT",
        "TP_DOG_REDIS_MASK_COMMAND",
        "TP_DOG_REDIS_CMD_MAX_LENGTH",
    )
    for name in legacy_names:
        monkeypatch.setenv(name, "1")

    cfg = SDKConfig.from_env_and_kwargs()

    assert cfg.project_name == "unknown-project"
    assert cfg.cluster_name == "unknown-cluster"
    assert cfg.environment == "development"
    assert cfg.endpoint == "http://localhost:4318"
    assert cfg.traces_endpoint is None
    assert cfg.metrics_endpoint is None
    assert cfg.metrics_enabled is True
    assert cfg.metrics_export_interval_millis == 60000
    assert cfg.headers == {}
    assert cfg.sample_rate == 1.0
    assert cfg.disabled is False
    assert cfg.debug is False
    assert cfg.tags == {}
    assert cfg.ignore_endpoints == []
    assert cfg.endpoint_sample_rules == {}
    assert cfg.trusted_proxies == []
    assert cfg.db_role_map == {}
    assert cfg.auto_patch is True
    assert cfg.extract_trace_context is True
    assert cfg.cache_enabled is True
    assert cfg.query_mode == "expose"
    assert cfg.redis_mask_endpoint is False
    assert cfg.redis_mask_command is False


def test_legacy_tp_dog_symbols_are_removed():
    """Verify retired tp_dog aliases are no longer importable from the SDK."""
    import tp_trace.integrations.boto.integration as boto_integration
    import tp_trace.integrations.django as django_integration
    import tp_trace.integrations.postgres.cursor as postgres_cursor
    import tp_trace.integrations.postgres.integration as postgres_integration
    import tp_trace.integrations.redis.integration as redis_integration
    import tp_trace.integrations.requests.client as requests_client
    import tp_trace.sampler as sampler

    assert not hasattr(django_integration, "TpDogMiddleware")
    assert not hasattr(sampler, "TpDogRuleBasedSampler")
    assert not hasattr(sampler, "create_tp_dog_sampler")
    assert not hasattr(requests_client, "tp_dog_request_hook")
    assert not hasattr(requests_client, "tp_dog_response_hook")
    assert not hasattr(postgres_cursor, "tp_dog_django_db_execute_wrapper")
    assert not hasattr(postgres_integration, "tp_dog_django_db_execute_wrapper")
    assert not hasattr(boto_integration, "_tp_dog_boto_request_hook")
    assert not hasattr(boto_integration, "_tp_dog_boto_response_hook")
    assert not hasattr(redis_integration, "_tp_dog_redis_request_hook")
    assert not hasattr(redis_integration, "_tp_dog_redis_response_hook")


def test_disabled_mode():
    """Verify that disabled mode prevents spans from being recorded."""
    exporter = InMemorySpanExporter()
    _ = tp_trace.init(
        project_name="disabled-service",
        disabled=True,
        exporter=exporter,
        export_batch=False,
    )
    tracer = trace.get_tracer("disabled-test")
    with tracer.start_as_current_span("test-span") as span:
        span.set_attribute("key", "val")

    spans = exporter.get_finished_spans()
    assert len(spans) == 0


def test_span_creation_and_export():
    """Verify spans are created, executed within context, and exported correctly."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="span-test-service",
        exporter=exporter,
        export_batch=False,
    )

    tracer = tp_trace.get_tracer("test-tracer")
    with tracer.start_as_current_span("parent-operation") as parent:
        parent.set_attribute("parent.attr", "parent_val")
        current_span = tp_trace.get_current_span()
        assert current_span == parent

        with tracer.start_as_current_span("child-operation") as child:
            child.set_attribute("child.attr", "child_val")
            assert tp_trace.get_current_span() == child

    spans = exporter.get_finished_spans()
    assert len(spans) == 2

    child_span = spans[0]
    parent_span = spans[1]

    assert child_span.name == "child-operation"
    assert child_span.attributes["child.attr"] == "child_val"
    assert child_span.parent.span_id == parent_span.context.span_id

    assert parent_span.name == "parent-operation"
    assert parent_span.attributes["parent.attr"] == "parent_val"


def test_sanitize_url():
    """Verify URL credentials and fragments are removed properly."""
    # Basic URL
    assert sanitize_url("http://example.com/path") == "http://example.com/path"

    # URL with username and password
    url_with_auth = "https://user:secretpass@api.external.com:8080/v1/data?query=1#top"
    cleaned = sanitize_url(url_with_auth)
    assert "secretpass" not in cleaned
    assert "user" not in cleaned
    assert "#top" not in cleaned
    assert cleaned == "https://api.external.com:8080/v1/data?query=1"

    # Strip query option
    cleaned_no_query = sanitize_url(url_with_auth, strip_query=True)
    assert cleaned_no_query == "https://api.external.com:8080/v1/data"

    # None and empty
    assert sanitize_url(None) == ""
    assert sanitize_url("") == ""


def test_sanitize_sql():
    """Verify SQL strings and numbers are parameterized and whitespace is normalized."""
    # Strings and numbers replacement
    raw_query = "SELECT * FROM users WHERE id = 42 AND email = 'alice@example.com' AND active = 1"
    expected = "SELECT * FROM users WHERE id = ? AND email = ? AND active = ?"
    assert sanitize_sql(raw_query) == expected

    # Multiple whitespace and newlines
    multiline_query = """
        SELECT  id,   name
        FROM    products
        WHERE   price > 99.99
    """
    assert sanitize_sql(multiline_query) == "SELECT id, name FROM products WHERE price > ?"

    # Preserves table/column names that contain numbers or words
    col_query = "SELECT user_v2_id FROM table1 WHERE status = 'ACTIVE'"
    assert sanitize_sql(col_query) == "SELECT user_v2_id FROM table1 WHERE status = ?"

    # Truncation
    long_query = "SELECT " + ("a" * 5000)
    sanitized_long = sanitize_sql(long_query, max_length=100)
    assert len(sanitized_long) > 100
    assert sanitized_long.endswith("... [truncated]")

    # None and empty
    assert sanitize_sql(None) == ""
    assert sanitize_sql("") == ""


def test_sanitize_sql_collapses_in_list_arity():
    """IN list length must not create a distinct label per batch size."""
    short = sanitize_sql("SELECT * FROM api_product WHERE id IN (1, 2, 3)")
    long = sanitize_sql("SELECT * FROM api_product WHERE id IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)")
    single = sanitize_sql("SELECT * FROM api_product WHERE id IN (7)")

    assert short == "SELECT * FROM api_product WHERE id IN (?)"
    # The whole point: differing arities collapse to one identical label.
    assert short == long == single

    # Whitespace and newline variants collapse too.
    assert sanitize_sql("SELECT 1 FROM t WHERE id IN\n  (1,\n   2)") == ("SELECT ? FROM t WHERE id IN (?)")
    assert sanitize_sql("SELECT 1 FROM t WHERE id IN(1, 2)") == ("SELECT ? FROM t WHERE id IN (?)")


def test_sanitize_sql_preserves_in_subquery_and_composites():
    """Only flat placeholder lists may collapse; real subqueries must survive intact."""
    # A subquery is not a placeholder list and must not be collapsed to "IN (?)",
    # which would merge it with a plain value list and mis-map the query.
    subquery = "SELECT * FROM t WHERE id IN (SELECT user_id FROM orders WHERE total > 100)"
    assert sanitize_sql(subquery) == ("SELECT * FROM t WHERE id IN (SELECT user_id FROM orders WHERE total > ?)")

    # Composite / nested element: inner parens mean the list is not provably flat.
    composite = "SELECT * FROM t WHERE (a, b) IN ((1, 2), (3, 4))"
    assert sanitize_sql(composite) == "SELECT * FROM t WHERE (a, b) IN ((?, ?), (?, ?))"

    # NOT IN collapses consistently with IN, keeping the NOT operator.
    assert sanitize_sql("SELECT * FROM t WHERE id NOT IN (1, 2, 3)") == ("SELECT * FROM t WHERE id NOT IN (?)")

    # A function call that merely ends in "in" must not be treated as a predicate.
    assert sanitize_sql("SELECT checkin FROM t WHERE id = 1") == ("SELECT checkin FROM t WHERE id = ?")

    # An empty IN list has no placeholders and is left alone.
    assert sanitize_sql("SELECT * FROM t WHERE id IN ()") == "SELECT * FROM t WHERE id IN ()"


def test_sanitize_sql_in_collapse_preserves_case_and_structure():
    """The predicate keyword casing and surrounding SQL must survive collapsing."""
    assert sanitize_sql("select * from t where id in (1,2)") == "select * from t where id in (?)"
    assert sanitize_sql("SELECT * FROM t WHERE a IN (1,2) AND b IN (3,4,5)") == (
        "SELECT * FROM t WHERE a IN (?) AND b IN (?)"
    )
    # A string literal containing "IN (1, 2)" is redacted before list collapsing,
    # so it must not be rewritten.
    assert sanitize_sql("SELECT * FROM t WHERE note = 'keep IN (1, 2) here'") == ("SELECT * FROM t WHERE note = ?")


def test_sanitize_sql_normalizes_search_path():
    """Verify SET search_path statements with dynamic schemas are collapsed to prevent cardinality explosion."""
    cases = [
        ("SET search_path = kpnwvejtop,public", "SET search_path = ?", "SET search_path = ?"),
        ("SET search_path = public", "SET search_path = ?", "SET search_path = ?"),
        ("SET search_path TO tenant, public", "SET search_path = ?", "SET search_path = ?"),
        ('SET search_path = "tenant", public', "SET search_path = ?", "SET search_path = ?"),
        ("SET search_path = 'tenant', public", "SET search_path = ?", "SET search_path = ?"),
        ('SET search_path = "foo;bar", public', "SET search_path = ?", "SET search_path = ?"),
        (
            'SET search_path = "foo;bar", public; SELECT 1',
            "SET search_path = ?; SELECT ?",
            "SET search_path = ?; SELECT ?",
        ),
        ("SET search_path=tenant,public", "SET search_path = ?", "SET search_path = ?"),
        ("set search_path = tenant,public", "set search_path = ?", "set search_path = ?"),
        (
            "SET SESSION search_path = tenant, public",
            "SET SESSION search_path = ?",
            "SET SESSION search_path = ?",
        ),
        ("SET LOCAL search_path = tenant, public", "SET LOCAL search_path = ?", "SET LOCAL search_path = ?"),
        ("SET search_path = ?", "SET search_path = ?", "SET search_path = ?"),
        ("SET search_path = tenant, public;", "SET search_path = ?;", "SET search_path = ?;"),
        (
            "/* comment */ SET search_path = tenant, public",
            "/* comment */ SET search_path = ?",
            "SET search_path = ?",
        ),
    ]
    for raw, expected_sanitized, expected_metric in cases:
        assert sanitize_sql(raw) == expected_sanitized
        assert normalize_sql_for_metric(raw) == expected_metric


class DummyTarget:
    def greet(self, name: str) -> str:
        return f"Hello, {name}"


class MockInstalledIntegration(tp_trace.BaseIntegration):
    name = "mock_installed"

    def is_installed(self) -> bool:
        return True

    def _apply_patch(self) -> None:
        def wrapper(wrapped, instance, args, kwargs):
            result = wrapped(*args, **kwargs)
            return f"Intercepted: {result}"

        self.wrap(DummyTarget, "greet", wrapper)


class MockMissingIntegration(tp_trace.BaseIntegration):
    name = "mock_missing"

    def is_installed(self) -> bool:
        return False

    def _apply_patch(self) -> None:
        pass


def test_base_integration_wrap_and_unwrap():
    """Verify that BaseIntegration can wrap a method and unwrap cleanly."""
    target = DummyTarget()
    assert target.greet("Alice") == "Hello, Alice"

    integration = MockInstalledIntegration()
    assert integration.instrument() is True

    # Check that wrapper intercepted call
    assert target.greet("Alice") == "Intercepted: Hello, Alice"

    # Uninstrument and verify restored
    assert integration.uninstrument() is True
    assert target.greet("Alice") == "Hello, Alice"


def test_patch_all_discovers_installed_libs():
    """Verify patch_all discovers and instruments installed integrations."""
    manager = tp_trace.get_integration_manager()
    manager.register("mock_installed", MockInstalledIntegration)

    target = DummyTarget()
    applied = tp_trace.patch_all()
    assert "mock_installed" in applied
    assert target.greet("Bob") == "Intercepted: Hello, Bob"


def test_patch_all_skips_missing_libs():
    """Verify patch_all skips uninstalled integrations."""
    manager = tp_trace.get_integration_manager()
    manager.register("mock_missing", MockMissingIntegration)

    applied = tp_trace.patch_all()
    assert "mock_missing" not in applied


def test_patch_all_respects_kwargs_and_config_disables():
    """Verify that individual integrations can be disabled via patch_all kwargs or config."""
    manager = tp_trace.get_integration_manager()
    manager.register("mock_installed", MockInstalledIntegration)

    # 1. Disabled via kwargs
    applied = tp_trace.patch_all(mock_installed=False)
    assert "mock_installed" not in applied

    # 2. Disabled via config
    tp_trace.init(
        project_name="test-service",
        integrations={"mock_installed": False},
        export_batch=False,
    )
    applied_cfg = tp_trace.patch_all()
    assert "mock_installed" not in applied_cfg


def test_safe_span_exporter_absorbs_network_exceptions():
    """Verify SafeSpanExporter absorbs ConnectionError and returns FAILURE without raising."""
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from tp_trace.exporter import SafeSpanExporter

    class BrokenExporter(SpanExporter):
        def export(self, spans):
            raise ConnectionRefusedError("[Errno 61] Connection refused")

        def shutdown(self):
            raise RuntimeError("Shutdown failed")

    safe = SafeSpanExporter(BrokenExporter(), endpoint="http://localhost:4318/v1/traces")
    # Export must return FAILURE and NOT raise!
    result = safe.export([])
    assert result == SpanExportResult.FAILURE

    # Shutdown must not raise!
    safe.shutdown()


def test_safe_span_exporter_rate_limits_warnings(caplog):
    """Verify SafeSpanExporter uses monotonic clock and rate-limits error warnings."""
    import logging
    from unittest.mock import patch
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from tp_trace.exporter import SafeSpanExporter

    class BrokenExporter(SpanExporter):
        def export(self, spans):
            raise ConnectionRefusedError("[Errno 61] Connection refused")

    safe = SafeSpanExporter(BrokenExporter(), endpoint="http://localhost:4318/v1/traces")

    with caplog.at_level(logging.WARNING, logger="tp_trace.exporter"):
        # Initial failure at t=100.0 (e.g. system uptime 100s)
        with patch("time.monotonic", return_value=100.0):
            res1 = safe.export([])
            assert res1 == SpanExportResult.FAILURE
        assert len(caplog.records) == 1
        assert "Unable to export spans" in caplog.records[0].message

        # Immediate second failure at t=105.0 within 300s window should be suppressed
        with patch("time.monotonic", return_value=105.0):
            res2 = safe.export([])
            assert res2 == SpanExportResult.FAILURE
        assert len(caplog.records) == 1

        # Third failure at t=401.0 (> 300s elapsed) should log again
        with patch("time.monotonic", return_value=401.0):
            res3 = safe.export([])
            assert res3 == SpanExportResult.FAILURE
        assert len(caplog.records) == 2
        assert "Failures: 3" in caplog.records[1].message


def test_safe_span_exporter_normalizes_real_readable_spans():
    """Verify SafeSpanExporter correctly mutates BoundedAttributes on real ended ReadableSpans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from tp_trace.exporter import SafeSpanExporter

    memory_exporter = InMemorySpanExporter()
    safe_exporter = SafeSpanExporter(memory_exporter)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(safe_exporter))
    tracer = provider.get_tracer("real.test.tracer")

    with tracer.start_as_current_span("client_query") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.statement", "SELECT * FROM users")

    finished = memory_exporter.get_finished_spans()
    assert len(finished) == 1
    exported_span = finished[0]
    assert exported_span.attributes.get("normalized.service") == "postgres"
    assert exported_span.attributes.get("normalized.operation") == "SELECT * FROM users"


def test_unreachable_collector_never_crashes_application():
    """Verify that tracing with an unreachable collector endpoint NEVER crashes or raises."""
    # Point to a dead/non-existent port on localhost
    provider = tp_trace.init(
        project_name="resilience-test",
        endpoint="http://127.0.0.1:59999",
        export_batch=False,
    )
    tracer = trace.get_tracer("test.tracer")

    # Creating and ending spans when collector is dead must be 100% safe
    with tracer.start_as_current_span("resilient-span") as span:
        span.set_attribute("app.healthy", True)

    # Force flush must not raise
    provider.force_flush()
    # Shutdown must not raise
    provider.shutdown()


def test_auto_patch_true_instruments_installed_integrations():
    """Verify auto_patch=True auto-instruments installed integrations."""
    manager = tp_trace.get_integration_manager()
    manager.register("mock_installed", MockInstalledIntegration)

    target = DummyTarget()
    tp_trace.init(
        project_name="auto-patch-test",
        auto_patch=True,
        exporter=InMemorySpanExporter(),
        export_batch=False,
    )
    assert target.greet("Alice") == "Intercepted: Hello, Alice"


def test_auto_patch_false_skips_instrumentation():
    """Verify auto_patch=False skips auto-instrumentation."""
    manager = tp_trace.get_integration_manager()
    manager.register("mock_installed", MockInstalledIntegration)

    target = DummyTarget()
    tp_trace.init(
        project_name="no-auto-patch-test",
        auto_patch=False,
        exporter=InMemorySpanExporter(),
        export_batch=False,
    )
    # Without auto_patch, the integration is NOT instrumented
    assert target.greet("Alice") == "Hello, Alice"


def test_auto_patch_respects_integrations_config():
    """Verify auto_patch respects integrations config to disable specific integrations."""
    manager = tp_trace.get_integration_manager()
    manager.register("mock_installed", MockInstalledIntegration)

    target = DummyTarget()
    tp_trace.init(
        project_name="config-disable-test",
        integrations={"mock_installed": False},
        exporter=InMemorySpanExporter(),
        export_batch=False,
    )
    # Integration is disabled via config
    assert target.greet("Alice") == "Hello, Alice"


def test_sample_rate_zero():
    """Verify ALWAYS_OFF sampler drops all spans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_OFF)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("sample-test")
    with tracer.start_as_current_span("dropped-span"):
        pass
    assert len(exporter.get_finished_spans()) == 0


def test_sample_rate_ratio():
    """Verify TraceIdRatioBased(0.5) samples approximately half of root spans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ParentBased(root=TraceIdRatioBased(0.5)))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("sample-test")
    for i in range(100):
        with tracer.start_as_current_span(f"root-span-{i}"):
            pass
    finished = exporter.get_finished_spans()
    assert 20 <= len(finished) <= 80


def test_str_to_bool_validation():
    import pytest
    from tp_trace.config import _str_to_bool

    assert _str_to_bool("true") is True
    assert _str_to_bool("1") is True
    assert _str_to_bool("false") is False
    assert _str_to_bool("0") is False
    assert _str_to_bool(None, default=True) is True

    with pytest.raises(ValueError, match="cannot parse 'invalid_value' as boolean"):
        _str_to_bool("invalid_value")


def test_sample_rate_validation():
    import pytest
    from tp_trace.config import SDKConfig

    # Valid rates
    cfg1 = SDKConfig.from_env_and_kwargs(sample_rate=0.5)
    assert cfg1.sample_rate == 0.5
    cfg2 = SDKConfig.from_env_and_kwargs(sample_rate="0.0")
    assert cfg2.sample_rate == 0.0

    # Invalid rates must raise ValueError
    with pytest.raises(ValueError, match="outside valid range"):
        SDKConfig.from_env_and_kwargs(sample_rate=1.5)
    with pytest.raises(ValueError, match="outside valid range"):
        SDKConfig.from_env_and_kwargs(sample_rate=-0.1)
    with pytest.raises(ValueError, match="not a valid number"):
        SDKConfig.from_env_and_kwargs(sample_rate="not_a_number")


def test_masking_exposure_config_defaults():
    """Masking toggles default to expose: real Redis endpoint, full commands, raw query."""
    cfg = SDKConfig.from_env_and_kwargs()
    assert cfg.redis_mask_endpoint is False
    assert cfg.redis_mask_command is False
    assert cfg.redis_cmd_max_length == 1000
    assert cfg.query_mode == "expose"


def test_masking_exposure_config_env_and_kwargs(monkeypatch):
    monkeypatch.setenv("TP_TRACE_REDIS_MASK_ENDPOINT", "true")
    monkeypatch.setenv("TP_TRACE_REDIS_MASK_COMMAND", "1")
    monkeypatch.setenv("TP_TRACE_REDIS_CMD_MAX_LENGTH", "500")
    monkeypatch.setenv("TP_TRACE_QUERY_MODE", "strip")

    cfg = SDKConfig.from_env_and_kwargs()
    assert cfg.redis_mask_endpoint is True
    assert cfg.redis_mask_command is True
    assert cfg.redis_cmd_max_length == 500
    assert cfg.query_mode == "strip"

    # Explicit kwargs override env vars
    cfg2 = SDKConfig.from_env_and_kwargs(
        redis_mask_endpoint=False,
        redis_mask_command=False,
        query_mode="redact",
    )
    assert cfg2.redis_mask_endpoint is False
    assert cfg2.redis_mask_command is False
    assert cfg2.query_mode == "redact"


def test_masking_exposure_config_validation():
    with pytest.raises(ValueError, match="query_mode"):
        SDKConfig.from_env_and_kwargs(query_mode="drop")
    with pytest.raises(ValueError, match="redis_cmd_max_length"):
        SDKConfig.from_env_and_kwargs(redis_cmd_max_length="not_a_number")
    with pytest.raises(ValueError, match="redis_cmd_max_length"):
        SDKConfig.from_env_and_kwargs(redis_cmd_max_length=0)


def test_init_invalid_config_does_not_crash_app():
    """Verify tp_trace.init() catches invalid config and falls back safely without raising ValueError."""
    import tp_trace

    tp_trace._reset_for_testing()
    try:
        provider = tp_trace.init(
            project_name="safe-fallback-app",
            sample_rate="not_a_number",
        )
        assert provider is not None
        assert tp_trace._ACTIVE_CONFIG is not None
        assert tp_trace._ACTIVE_CONFIG.sample_rate == 1.0
    finally:
        tp_trace._reset_for_testing()


def test_init_configures_composite_propagator():
    """Verify tp_trace.init() sets CompositePropagator supporting both TraceContext and Baggage."""
    from opentelemetry.propagate import get_global_textmap
    from opentelemetry.propagators.composite import CompositePropagator
    import tp_trace

    tp_trace._reset_for_testing()
    try:
        tp_trace.init(project_name="propagator-test")
        prop = get_global_textmap()
        assert isinstance(prop, CompositePropagator)
    finally:
        tp_trace._reset_for_testing()
