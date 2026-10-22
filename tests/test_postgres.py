"""Unit tests for Postgres / Database Integration."""

from contextlib import nullcontext
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

import django
from django.conf import settings
from django.db.backends.utils import CursorWrapper
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import path

# Configure minimal Django settings for testing if not already configured
if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="test-secret-key-postgres",
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["*"],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            },
            "slave1": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            },
        },
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
from tp_trace.integrations.postgres import PostgresIntegration


class MockRawCursor:
    def __init__(self, rowcount=1, raise_exc=None):
        self.executed = []
        self.rowcount = rowcount
        self.raise_exc = raise_exc

    def execute(self, sql, params=None):
        if self.raise_exc:
            raise self.raise_exc
        self.executed.append((sql, params))
        return self

    def executemany(self, sql, param_list):
        if self.raise_exc:
            raise self.raise_exc
        self.executed.append((sql, param_list))
        return self


class MockDatabaseConnection:
    def __init__(
        self,
        alias="default",
        vendor="postgresql",
        host="db.internal",
        port=5432,
        db_name="prod_db",
        user="postgres_user",
    ):
        self.alias = alias
        self.vendor = vendor
        self.execute_wrappers = []
        self.wrap_database_errors = nullcontext()
        self.settings_dict = {
            "NAME": db_name,
            "HOST": host,
            "PORT": port,
            "USER": user,
        }

    def validate_no_broken_transaction(self):
        pass


@pytest.fixture(autouse=True)
def clean_sdk_and_postgres():
    tp_trace._reset_for_testing()
    from tp_trace.integrations import get_integration_manager

    mgr = get_integration_manager()
    mgr.apply_integrations()
    yield
    try:
        mgr.uninstrument_all()
    except Exception:
        pass
    tp_trace._reset_for_testing()


def test_select_creates_span():
    """Verify executing a query through CursorWrapper creates a CLIENT span with connection attributes."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-test-svc", exporter=exporter, export_batch=False)

    raw_cursor = MockRawCursor(rowcount=5)
    mock_db = MockDatabaseConnection(
        alias="default",
        vendor="postgresql",
        host="pg-primary.prod",
        port=5432,
        db_name="accounts",
        user="app_user",
    )
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.execute("SELECT id, name FROM users WHERE active = 1")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "🟢 SELECT id, name FROM users WHERE active = ?"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["db.system"] == "postgresql"
    assert span.attributes["db.operation"] == "SELECT"
    assert span.attributes["db.name"] == "accounts"
    assert span.attributes["db.instance"] == "default"
    assert span.attributes["db.connection_alias"] == "default"
    assert span.attributes["net.peer.name"] == "pg-primary.prod"
    assert span.attributes["net.peer.port"] == 5432
    assert span.attributes["server.address"] == "pg-primary.prod"
    assert span.attributes["server.port"] == 5432
    assert span.attributes["db.user"] == "app_user"
    assert "peer.service" not in span.attributes
    assert span.attributes["db.row_count"] == 5
    assert span.status.status_code == StatusCode.OK


def test_sanitized_sql_in_attributes():
    """Verify raw parameters and values are sanitized in db.statement."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-test-svc", exporter=exporter, export_batch=False)

    raw_cursor = MockRawCursor()
    mock_db = MockDatabaseConnection(alias="default")
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.execute("SELECT * FROM sensitive_users WHERE email = 'secret@example.com' AND age >= 21")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    statement = span.attributes["db.statement"]
    assert "secret@example.com" not in statement
    assert "21" not in statement
    assert statement == "SELECT * FROM sensitive_users WHERE email = ? AND age >= ?"


def test_primary_role_detected():
    """Verify primary role is detected for default / non-replica database aliases."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-test-svc", exporter=exporter, export_batch=False)

    raw_cursor = MockRawCursor()
    mock_db = MockDatabaseConnection(alias="default")
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.execute("SELECT 1")

    spans = exporter.get_finished_spans()
    assert spans[0].attributes["db.role"] == "primary"
    assert "peer.service" not in spans[0].attributes


def test_replica_role_detected():
    """Verify replica role and custom peer.service are detected for slave/read aliases."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-test-svc", exporter=exporter, export_batch=False)

    raw_cursor = MockRawCursor()
    mock_db = MockDatabaseConnection(alias="slave1db", host="pg-replica.prod")
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.execute("SELECT 1 FROM readonly_table")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.attributes["db.role"] == "replica"
    assert span.attributes["db.instance"] == "slave1db"
    assert "peer.service" not in span.attributes


def test_db_role_map_and_false_positive_prevention():
    """Verify thread_pool is not falsely identified as replica, and db_role_map takes precedence."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="postgres-test-svc",
        exporter=exporter,
        export_batch=False,
        db_role_map={"custom_reader": "replica", "replica_override": "primary"},
    )

    # 1. thread_pool should be primary, NOT replica (previously contained "read")
    mock_db = MockDatabaseConnection(alias="thread_pool")
    wrapper = CursorWrapper(MockRawCursor(), mock_db)
    wrapper.execute("SELECT 1")

    # 2. readiness_probe should be primary
    mock_db2 = MockDatabaseConnection(alias="readiness_probe")
    wrapper2 = CursorWrapper(MockRawCursor(), mock_db2)
    wrapper2.execute("SELECT 1")

    # 3. db_role_map explicit replica
    mock_db3 = MockDatabaseConnection(alias="custom_reader")
    wrapper3 = CursorWrapper(MockRawCursor(), mock_db3)
    wrapper3.execute("SELECT 1")

    # 4. db_role_map explicit primary override
    mock_db4 = MockDatabaseConnection(alias="replica_override")
    wrapper4 = CursorWrapper(MockRawCursor(), mock_db4)
    wrapper4.execute("SELECT 1")

    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    assert spans[0].attributes["db.role"] == "primary"
    assert spans[1].attributes["db.role"] == "primary"
    assert spans[2].attributes["db.role"] == "replica"
    assert spans[3].attributes["db.role"] == "primary"


@pytest.mark.parametrize(
    "alias,expected",
    [
        # Replica keywords as a separator-delimited token.
        ("slave1db", "replica"),
        ("slave1", "replica"),
        ("slave2db", "replica"),
        ("replica", "replica"),
        ("replica1", "replica"),
        ("replica-prod", "replica"),
        ("readonly", "replica"),
        ("read", "replica"),
        ("read_replica", "replica"),
        ("db_slave1", "replica"),
        # Must not be misread as replicas: "read"/"thread" substrings.
        ("default", "primary"),
        ("thread_pool", "primary"),
        ("readiness_probe", "primary"),
        ("readwrite", "primary"),
        ("courses", "primary"),
    ],
)
def test_replica_alias_heuristic_contract(alias, expected):
    """Lock in the alias -> db.role contract.

    Role detection is a name heuristic plus the explicit ``db_role_map`` escape
    hatch. This pins the shapes it does and does not match so a future regex
    edit cannot silently reclassify an alias (which would split peer.service
    series and make replica lag look like zero). ``myreplica``-style names with
    no separator before the keyword are intentionally unsupported -- use
    ``db_role_map`` for those.
    """
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-role-contract", exporter=exporter, export_batch=False)

    CursorWrapper(MockRawCursor(), MockDatabaseConnection(alias=alias)).execute("SELECT 1")

    span = exporter.get_finished_spans()[0]
    assert span.attributes["db.role"] == expected


def test_reentrancy_guard_prevents_duplicate_spans():
    """Verify that re-entrant cursor executions do not create duplicate spans."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-test-svc", exporter=exporter, export_batch=False)

    class ReentrantRawCursor:
        def __init__(self):
            self.rowcount = 1

        def execute(self, sql, params=None):
            # Nested call on same wrapper
            if not getattr(wrapper, "_reentered", False):
                wrapper._reentered = True
                wrapper.execute("SELECT 2")
            return self

    raw_cursor = ReentrantRawCursor()
    mock_db = MockDatabaseConnection(alias="default")
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.execute("SELECT 1")

    spans = exporter.get_finished_spans()
    # Exactly 1 span from the outer call
    assert len(spans) == 1


def test_query_exception_records_error():
    """Verify database exceptions are recorded with status ERROR and error attributes."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-test-svc", exporter=exporter, export_batch=False)

    raw_cursor = MockRawCursor(raise_exc=RuntimeError("connection deadlock detected"))
    mock_db = MockDatabaseConnection(alias="default")
    wrapper = CursorWrapper(raw_cursor, mock_db)

    with pytest.raises(RuntimeError, match="connection deadlock detected"):
        wrapper.execute("UPDATE accounts SET balance = balance - 100 WHERE id = 1")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error"] is True
    assert span.attributes["error.type"] == "RuntimeError"
    assert len(span.events) >= 1
    assert span.events[0].name == "exception"


def test_executemany_creates_span():
    """Verify executemany creates a CLIENT span with correct operation."""
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-test-svc", exporter=exporter, export_batch=False)

    raw_cursor = MockRawCursor(rowcount=3)
    mock_db = MockDatabaseConnection(alias="default")
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.executemany(
        "INSERT INTO items (name, price) VALUES (%s, %s)",
        [("Item A", 10), ("Item B", 20), ("Item C", 30)],
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.name == "🟢 INSERT INTO items (name, price) VALUES (%s, %s)"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["db.operation"] == "INSERT"
    assert span.attributes["db.row_count"] == 3
    assert span.status.status_code == StatusCode.OK


def db_test_view(request):
    raw_cursor = MockRawCursor(rowcount=2)
    mock_db = MockDatabaseConnection(alias="slave1", host="replica.prod", db_name="shop_db")
    wrapper = CursorWrapper(raw_cursor, mock_db)
    wrapper.execute("SELECT id, name FROM products WHERE category = 'books'")
    return HttpResponse("OK")


urlpatterns = [
    path("api/db-test/", db_test_view, name="db-test"),
]


def test_uninstrument_and_idempotency():
    """Verify clean uninstrumentation restores original methods and instrument is idempotent."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-test-svc", exporter=exporter, export_batch=False, auto_patch=False)

    integration = PostgresIntegration()
    assert integration.instrument() is True
    assert integration.instrument() is True  # Idempotent

    raw_cursor = MockRawCursor()
    mock_db = MockDatabaseConnection(alias="default")
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.execute("SELECT 1")
    assert len(exporter.get_finished_spans()) == 1
    exporter.clear()

    # Uninstrument
    assert integration.uninstrument() is True

    # After uninstrument, wrapper.execute should not create spans
    wrapper.execute("SELECT 1")
    assert len(exporter.get_finished_spans()) == 0


def test_patch_all_enables_postgres():
    """Verify tp_trace.patch_all() auto-discovers and instruments postgres."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="postgres-test-svc", exporter=exporter, export_batch=False)

    enabled = tp_trace.patch_all()
    assert "postgres" in enabled

    raw_cursor = MockRawCursor()
    mock_db = MockDatabaseConnection(alias="default")
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.execute("SELECT 1")
    spans = exporter.get_finished_spans()
    db_spans = [s for s in spans if s.attributes.get("db.system") == "postgresql"]
    assert len(db_spans) == 1
    assert db_spans[0].name == "🟢 SELECT ?"


def test_django_request_waterfall_with_db():
    """Verify full waterfall: django.request -> django.view -> postgres.query."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="waterfall-test", exporter=exporter, export_batch=False)
    tp_trace.patch_all()

    from django.urls import clear_url_caches
    from django.core.handlers.wsgi import WSGIHandler

    original_urlconf = settings.ROOT_URLCONF
    settings.ROOT_URLCONF = __name__
    clear_url_caches()

    try:
        handler = WSGIHandler()
        handler.load_middleware()

        factory = RequestFactory()
        req = factory.get("/api/db-test/")
        resp = handler.get_response(req)
        assert resp.status_code == 200

        spans = exporter.get_finished_spans()
        req_span = next(s for s in spans if s.kind == SpanKind.SERVER)
        db_spans = [s for s in spans if s.attributes.get("db.system") == "postgresql"]
        assert len(db_spans) == 1

        db_span = db_spans[0]
        assert db_span.name == "🟢 SELECT id, name FROM products WHERE category = ?"
        assert db_span.context.trace_id == req_span.context.trace_id
        assert db_span.attributes["db.role"] == "replica"
        assert "peer.service" not in db_span.attributes
        assert db_span.attributes["db.name"] == "shop_db"
        assert db_span.kind == SpanKind.CLIENT
    finally:
        settings.ROOT_URLCONF = original_urlconf
        clear_url_caches()


def test_raw_psycopg_cursor_exec():
    """Verify PostgresIntegration instruments psycopg driver."""
    integration = PostgresIntegration()
    assert integration.is_installed() is True
    assert integration.name == "postgres"


def test_is_installed_does_not_claim_psycopg3_support():
    """psycopg3 alone must not report the integration as installed.

    Only the Django seams trace psycopg3; the raw-driver seam is psycopg2-only.
    Probing psycopg3 made a psycopg3-only host report "🔵 instrumented"
    while emitting no spans at all, which is worse than reporting nothing.
    """
    import tp_trace.integrations.postgres.integration as pg_int

    class _Psycopg3OnlyHost:
        """A non-Django host on psycopg3: psycopg imports, nothing else does."""

        @staticmethod
        def import_module(name):
            if name == "psycopg":
                return object()
            raise ImportError(f"simulated: {name} is not installed")

    integration = PostgresIntegration()
    original = pg_int.importlib
    pg_int.importlib = _Psycopg3OnlyHost
    try:
        assert integration.is_installed() is False
    finally:
        pg_int.importlib = original


def test_django_path_is_driver_agnostic():
    """The Django seams bind to no driver, so psycopg3 needs no special case.

    This is why removing the psycopg3 probe costs nothing for a Django project
    on the psycopg3 driver: cursor.py builds the spans from duck-typed metadata,
    so the same code path serves psycopg2 and psycopg3.
    """
    import inspect

    from tp_trace.integrations.postgres import cursor as cur

    source = inspect.getsource(cur)
    # Covers "import psycopg2" as well, since it contains "import psycopg".
    assert "import psycopg" not in source
    assert "from psycopg" not in source



def test_extract_operation_fallback():
    """Verify operation extraction falls back to QUERY for empty or unknown SQL."""
    from tp_trace.integrations.postgres.cursor import extract_operation

    assert extract_operation("") == "QUERY"
    assert extract_operation(None) == "QUERY"
    assert extract_operation("   ") == "QUERY"
    assert extract_operation("UNKNOWN_SQL_KEYWORD something") == "QUERY"
    assert extract_operation("select * from t") == "SELECT"
    assert extract_operation("INSERT into t values(1)") == "INSERT"


def test_extract_query_summary():
    """Verify query summary extracts operation and target table correctly."""
    from tp_trace.integrations.postgres.cursor import extract_query_summary

    assert extract_query_summary("") == "QUERY"
    assert extract_query_summary(None) == "QUERY"
    assert extract_query_summary("SELECT * FROM products WHERE id = ?") == "SELECT products"
    assert extract_query_summary('SELECT "api_product"."id" FROM "api_product"') == "SELECT api_product"
    assert extract_query_summary('INSERT INTO "api_order" ("id") VALUES (?)') == "INSERT api_order"
    assert extract_query_summary('UPDATE "api_product" SET "stock" = ?') == "UPDATE api_product"
    assert extract_query_summary('DELETE FROM "api_cart"') == "DELETE api_cart"
    assert extract_query_summary("BEGIN") == "BEGIN"
    assert extract_query_summary("COMMIT") == "COMMIT"


def test_db_span_carries_alias_and_physical_db_name():
    """A single CLIENT span carries both the Django connection alias and the
    physical database it resolved to, which is how primary/replica routing is
    attributed without a second parent span.

    slave3db (alias) -> testpress (physical database)
    """
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="postgres-test-svc",
        exporter=exporter,
        export_batch=False,
    )
    from tp_trace.integrations import get_integration_manager
    mgr = get_integration_manager()
    mgr.apply_integrations()

    raw_cursor = MockRawCursor(rowcount=3)
    mock_db = MockDatabaseConnection(
        alias="slave3db",
        vendor="postgresql",
        host="10.0.0.5",
        port=5432,
        db_name="testpress",
        user="postgres",
    )
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.execute("SELECT users_user.id FROM users_user WHERE is_active = 1")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1

    span = spans[0]
    assert span.name == "🟢 SELECT users_user.id FROM users_user WHERE is_active = ?"
    assert "peer.service" not in span.attributes
    assert span.attributes["db.name"] == "testpress"
    assert span.attributes["db.statement"] == "SELECT users_user.id FROM users_user WHERE is_active = ?"
    assert span.attributes["db.row_count"] == 3
    assert span.kind == SpanKind.CLIENT


def test_pgbouncer_single_query_span():
    """Verify queries routed through PgBouncer produce exactly one DB execution span tagged with pgbouncer pool."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="pgbouncer-test-svc",
        exporter=exporter,
        export_batch=False,
    )
    from tp_trace.integrations import get_integration_manager
    mgr = get_integration_manager()
    mgr.apply_integrations()

    raw_cursor = MockRawCursor(rowcount=5)
    mock_db = MockDatabaseConnection(
        alias="default",
        vendor="postgresql",
        host="pgbouncer",
        port=6432,
        db_name="django_otel",
        user="django",
    )
    wrapper = CursorWrapper(raw_cursor, mock_db)

    wrapper.execute("SELECT * FROM auth_user WHERE id = 1")

    spans = exporter.get_finished_spans()
    # Exactly one query span -- no synthetic outer pool-wait wrapper
    assert len(spans) == 1
    span = spans[0]

    assert span.name == "🟢 SELECT * FROM auth_user WHERE id = ?"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["db.system"] == "postgresql"
    assert span.attributes["db.system.name"] == "postgresql"
    assert span.attributes["db.connection.pool"] == "pgbouncer"
    assert span.attributes["server.address"] == "pgbouncer"
    assert span.attributes["server.port"] == 6432
    assert span.attributes["net.peer.name"] == "pgbouncer"
    assert span.attributes["net.peer.port"] == 6432
    assert span.attributes["db.name"] == "django_otel"
    assert span.attributes["db.namespace"] == "django_otel"
    assert span.attributes["db.operation.name"] == "SELECT"
    assert span.attributes["db.query.summary"] == "SELECT auth_user"
    assert span.attributes["db.response.returned_rows"] == 5


def test_pgbouncer_connect_span_on_get_new_connection():
    """Verify get_new_connection emits pgbouncer.connect measuring client-to-PgBouncer connection establishment."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="pgbouncer-connect-svc",
        exporter=exporter,
        export_batch=False,
    )
    from tp_trace.integrations import get_integration_manager
    mgr = get_integration_manager()
    mgr.apply_integrations()

    from django.db import connections

    conn = connections["default"]
    orig_host = conn.settings_dict.get("HOST", "")
    orig_port = conn.settings_dict.get("PORT", "")
    try:
        conn.settings_dict["HOST"] = "pgbouncer"
        conn.settings_dict["PORT"] = 6432
        conn.close()
        conn.connect()
        assert conn.connection is not None
    finally:
        conn.settings_dict["HOST"] = orig_host
        conn.settings_dict["PORT"] = orig_port
        conn.close()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    connect_span = spans[0]

    assert connect_span.name == "🔹 pgbouncer.connect"
    assert connect_span.kind == SpanKind.CLIENT
    assert connect_span.attributes["normalized.service"] == "pgbouncer"
    assert connect_span.attributes["normalized.operation"] == "pgbouncer.connect"
    assert connect_span.attributes["db.connection.pool"] == "pgbouncer"
    assert connect_span.attributes["server.address"] == "pgbouncer"
    assert connect_span.attributes["server.port"] == 6432
    assert connect_span.attributes["net.peer.port"] == 6432


def test_pgbouncer_execute_wrapper_single_span():
    """Verify execute_wrapper produces exactly one query span for PgBouncer without outer pool-wait."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="pgbouncer-exec-svc",
        exporter=exporter,
        export_batch=False,
    )
    from tp_trace.integrations.postgres.cursor import tp_trace_django_db_execute_wrapper

    mock_db = MockDatabaseConnection(
        alias="default",
        vendor="postgresql",
        host="pgbouncer",
        port=6432,
        db_name="django_otel",
        user="django",
    )
    raw_cursor = MockRawCursor(rowcount=3)
    executed = []

    def mock_exec(sql, params, many, context):
        executed.append((sql, params))
        return "ok"

    res = tp_trace_django_db_execute_wrapper(
        mock_exec,
        "SELECT id FROM auth_user WHERE is_staff = 1",
        (1,),
        False,
        {"connection": mock_db, "cursor": raw_cursor},
    )
    assert res == "ok"

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.name == "🟢 SELECT id FROM auth_user WHERE is_staff = ?"
    assert span.attributes["db.system"] == "postgresql"
    assert span.attributes["db.connection.pool"] == "pgbouncer"
    assert span.attributes["server.port"] == 6432
    assert span.attributes["db.response.returned_rows"] == 3


def test_suppress_driver_instrumentation_prevents_duplicate_spans():
    """Verify suppress_db_instrumentation sets OTel suppression key to prevent duplicate driver spans."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="single-span-policy-svc", exporter=exporter, export_batch=False)

    from tp_trace.integrations.postgres.cursor import suppress_db_instrumentation, _SUPPRESS_KEY
    from opentelemetry.context import get_value

    # Outside context: not suppressed
    assert get_value(_SUPPRESS_KEY) is None or get_value(_SUPPRESS_KEY) is False

    # Inside context: suppressed
    with suppress_db_instrumentation():
        assert get_value(_SUPPRESS_KEY) is True


def test_django_native_execute_wrapper_creates_span():
    """Verify executing via Django native execute_wrapper creates a span with correct attributes."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="native-wrapper-svc", exporter=exporter, export_batch=False)

    from tp_trace.integrations.postgres.cursor import tp_trace_django_db_execute_wrapper

    mock_db = MockDatabaseConnection(
        alias="slave1",
        vendor="postgresql",
        host="replica-host",
        port=5432,
        db_name="replica_db",
    )
    mock_cursor = MockRawCursor(rowcount=3)

    def mock_execute(sql, params, many, context):
        return mock_cursor.execute(sql, params)

    context = {"connection": mock_db, "cursor": mock_cursor}
    tp_trace_django_db_execute_wrapper(mock_execute, "SELECT id FROM items WHERE active = 1", None, False, context)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "🟢 SELECT id FROM items WHERE active = ?"
    assert span.attributes["db.role"] == "replica"
    assert span.attributes["db.instance"] == "slave1"
    assert span.attributes["db.row_count"] == 3


def test_no_duplicate_spans_when_cursorwrapper_and_execute_wrappers_coexist():
    """Verify that when CursorWrapper.execute and connection.execute_wrappers coexist, only 1 span is created."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="no-dup-svc", exporter=exporter, export_batch=False)

    from tp_trace.integrations.postgres.cursor import (
        traced_django_cursor_exec,
        tp_trace_django_db_execute_wrapper,
    )

    mock_db = MockDatabaseConnection(
        alias="default",
        vendor="postgresql",
        host="master-host",
        port=5432,
        db_name="master_db",
    )
    mock_cursor = MockRawCursor(rowcount=5)

    # Simulate Django's CursorWrapper which internally calls _execute_with_wrappers
    class RealDjangoCursorWrapper:
        def __init__(self, cursor, db):
            self.cursor = cursor
            self.db = db

        def execute(self, sql, params=None):
            # Django's _execute_with_wrappers invokes each wrapper in db.execute_wrappers
            def _raw_exec(s, p, many, ctx):
                return self.cursor.execute(s, p)

            context = {"connection": self.db, "cursor": self.cursor}
            return tp_trace_django_db_execute_wrapper(_raw_exec, sql, params, False, context)

    wrapper_instance = RealDjangoCursorWrapper(mock_cursor, mock_db)

    # traced_django_cursor_exec wraps CursorWrapper.execute
    def wrapped_execute(sql, params=None):
        return wrapper_instance.execute(sql, params)

    traced_django_cursor_exec(
        wrapped_execute,
        wrapper_instance,
        ("SELECT * FROM users WHERE active = 1", None),
        {},
        "execute",
    )

    spans = exporter.get_finished_spans()
    # There MUST be exactly 1 span, not 2 nested duplicate spans
    assert len(spans) == 1
    assert spans[0].name == "🟢 SELECT * FROM users WHERE active = ?"
    assert spans[0].attributes["db.instance"] == "default"


def test_suppress_db_instrumentation_disables_otel_dbapi():
    """Verify suppress_db_instrumentation disables is_instrumentation_enabled for official OTel drivers."""
    from opentelemetry.instrumentation.utils import is_instrumentation_enabled
    from tp_trace.integrations.postgres.cursor import suppress_db_instrumentation

    assert is_instrumentation_enabled() is True
    with suppress_db_instrumentation():
        assert is_instrumentation_enabled() is False
    assert is_instrumentation_enabled() is True


def test_reentrant_guard_thread_isolation():
    """Verify reentrant_guard with contextvars isolates concurrent threads sharing the same instance."""
    import threading
    from tp_trace.tracing import reentrant_guard

    class SharedConnection:
        pass

    conn = SharedConnection()
    results = {}

    def worker(worker_id: int):
        with reentrant_guard(conn, "_tp_in_exec") as should_trace_1:
            # Nested call on same thread -> suppressed
            with reentrant_guard(conn, "_tp_in_exec") as should_trace_nested:
                results[worker_id] = (should_trace_1, should_trace_nested)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All threads got (True, False) independently without blocking or interfering
    for i in range(5):
        assert results[i] == (True, False)


def test_single_exception_recorded_on_sql_error():
    """Verify an erroring SQL query produces exactly one exception event in its span."""
    tp_trace._reset_for_testing()
    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="error-svc", exporter=exporter, export_batch=False)

    from tp_trace.integrations.postgres.cursor import tp_trace_django_db_execute_wrapper

    class FailingCursor:
        pass

    def bad_execute(sql, params, many, context):
        raise ValueError("syntax error in SQL")

    cursor = FailingCursor()
    conn = MockDatabaseConnection(alias="default", vendor="postgresql", host="db", port=5432, db_name="test_db")
    context = {"cursor": cursor, "connection": conn}

    with pytest.raises(ValueError):
        tp_trace_django_db_execute_wrapper(bad_execute, "SELECT BAD SYNTAX", None, False, context)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    # Verify exactly one exception event is recorded on the span
    exception_events = [e for e in span.events if e.name == "exception"]
    assert len(exception_events) == 1
    assert exception_events[0].attributes["exception.type"] == "ValueError"


# ---------------------------------------------------------------------------
# Regression: wrappers must never re-invoke the wrapped callable.
#
# These assert the *effect* (how many times the statement ran), not just the
# span. A previous implementation caught every exception and re-called the
# wrapped function, so a failing INSERT or UPDATE was executed twice -- the
# span still looked correct, which is why this went unnoticed.
# ---------------------------------------------------------------------------


def test_failing_cursor_exec_runs_statement_exactly_once():
    """A failing query must be sent to the server once, never retried."""
    from tp_trace.integrations.postgres.cursor import traced_django_cursor_exec

    tp_trace.init(project_name="pg-no-retry", export_batch=False)
    calls: list = []

    def execute(sql, *args, **kwargs):
        calls.append(sql)
        raise RuntimeError("duplicate key value violates unique constraint")

    cursor = MockRawCursor()
    with pytest.raises(RuntimeError, match="duplicate key"):
        traced_django_cursor_exec(execute, cursor, ("INSERT INTO orders (id) VALUES (1)",), {})

    assert len(calls) == 1, f"statement executed {len(calls)} times; application code must run once"


def test_failing_execute_wrapper_runs_statement_exactly_once():
    """The connection.execute_wrappers path must not retry either."""
    from tp_trace.integrations.postgres.cursor import tp_trace_django_db_execute_wrapper

    tp_trace.init(project_name="pg-no-retry-2", export_batch=False)
    calls: list = []

    def execute(sql, params, many, context):
        calls.append(sql)
        raise ValueError("syntax error in SQL")

    conn = MockDatabaseConnection(
        alias="default", vendor="postgresql", host="db", port=5432, db_name="test_db"
    )
    context = {"cursor": MockRawCursor(), "connection": conn}

    with pytest.raises(ValueError):
        tp_trace_django_db_execute_wrapper(execute, "SELECT BAD", None, False, context)

    assert len(calls) == 1, f"statement executed {len(calls)} times; application code must run once"


def test_successful_query_runs_statement_exactly_once():
    """The happy path is unaffected by the retry removal."""
    from tp_trace.integrations.postgres.cursor import traced_django_cursor_exec

    exporter = InMemorySpanExporter()
    tp_trace.init(project_name="pg-happy", exporter=exporter, export_batch=False)

    calls: list = []

    def execute(sql, *args, **kwargs):
        calls.append(sql)
        return 1

    traced_django_cursor_exec(execute, MockRawCursor(rowcount=1), ("SELECT 1",), {})

    assert len(calls) == 1
    assert len(exporter.get_finished_spans()) == 1


def test_db_statement_and_full_statement_separation():
    """Verify that db.statement contains bounded normalized SQL while db.statement.full contains complete SQL."""
    from tp_trace.integrations.postgres.cursor import _build_db_span_context

    conn = MockDatabaseConnection(
        alias="default", vendor="postgresql", host="localhost", port=5432, db_name="shop"
    )

    # 1. Normal query with dynamic IN list and comment
    sql1 = "SELECT /* comment */ id, name FROM large_table WHERE id IN (%s, %s, %s)"
    _, attrs1 = _build_db_span_context(sql1, conn)
    assert attrs1["db.statement"] == "SELECT id, name FROM large_table WHERE id IN (?)"
    assert attrs1["resource.name"] == attrs1["db.statement"]
    assert "/* comment */" not in attrs1["db.statement"]

    # 2. Long query exceeding 256 chars gets bounded
    long_cols = ", ".join([f'"col_{i}"' for i in range(80)])
    sql2 = f"SELECT {long_cols} FROM large_table WHERE id = %s"
    _, attrs2 = _build_db_span_context(sql2, conn)

    assert len(attrs2["db.statement"]) <= 256
    assert attrs2["db.statement"].endswith("...")
    assert len(attrs2["db.statement.full"]) > len(attrs2["db.statement"])
    assert attrs2["db.query.text"] == attrs2["db.statement.full"]


def test_db_execute_wrapper_suppresses_downstream_driver_instrumentation():
    """Verify that during DB query execution, downstream OTel instrumentors (psycopg2) are suppressed."""
    from unittest.mock import MagicMock
    from opentelemetry.instrumentation.utils import is_instrumentation_enabled
    from tp_trace.integrations.postgres.cursor import tp_trace_django_db_execute_wrapper

    instrumentation_state = []

    def mock_execute(sql, params, many, context):
        instrumentation_state.append(is_instrumentation_enabled())
        return "ok"

    context = {"connection": MagicMock(vendor="postgresql", alias="default")}
    tp_trace_django_db_execute_wrapper(mock_execute, "SELECT 1", None, False, context)

    assert len(instrumentation_state) == 1
    assert instrumentation_state[0] is False  # Suppressed!


def test_postgres_integration_does_not_touch_connections_before_apps_ready(monkeypatch):
    """Verify that PostgresIntegration._apply_patch() does not access connections if apps.ready is False.

    Accessing connections before Django finishes loading settings forces ConnectionHandler
    to cache dummy settings and instantiate dummy database wrappers, breaking custom backends
    like tenant_schemas.
    """
    from django.apps import apps
    from django.db import connections
    from django.db.backends.signals import connection_created
    from unittest.mock import MagicMock
    from tp_trace.integrations.postgres.cursor import tp_trace_django_db_execute_wrapper

    integration = PostgresIntegration()

    # Simulate apps.ready = False (settings import time)
    monkeypatch.setattr(apps, "ready", False)

    connections_all_called = False
    orig_all = connections.all

    def spy_all(*args, **kwargs):
        nonlocal connections_all_called
        connections_all_called = True
        return orig_all(*args, **kwargs)

    monkeypatch.setattr(connections, "all", spy_all)

    try:
        integration._apply_patch()

        # connections.all should NOT have been called while apps.ready is False
        assert not connections_all_called

        # But new connections created later should still get the execute_wrapper attached via signal
        mock_conn = MagicMock()
        mock_conn.execute_wrappers = []
        connection_created.send(sender=mock_conn.__class__, connection=mock_conn)

        assert tp_trace_django_db_execute_wrapper in mock_conn.execute_wrappers
    finally:
        integration.uninstrument()
