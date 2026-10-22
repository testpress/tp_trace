"""PostgreSQL / Database query tracing and cursor wrapper helpers."""

import logging
import re
from typing import Any, Callable, Dict, Optional, Tuple

from opentelemetry.trace import SpanKind
from opentelemetry.context import (
    get_value,
    _SUPPRESS_INSTRUMENTATION_KEY,
)

from tp_trace.config_access import get_config as _get_config, set_config as set_config
from tp_trace.safety import UNTRACED, attempt, safe_set_attribute
from tp_trace.sanitize import normalize_sql_for_metric, sanitize_sql
from tp_trace.tracing import reentrant_guard, suppress_instrumentation, traced_span

logger = logging.getLogger("tp_trace.integrations.postgres")

_SUPPRESS_KEY = _SUPPRESS_INSTRUMENTATION_KEY


_KNOWN_SQL_OPS = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "CREATE",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "SET",
    "SHOW",
    "EXPLAIN",
)

_TABLE_RE = re.compile(r'\b(?:FROM|INTO|UPDATE|TABLE)\s+["`]?([a-zA-Z0-9_]+)["`]?', re.IGNORECASE)


def extract_operation(sql: Optional[str]) -> str:
    """Extract SQL operation keyword (e.g. SELECT, INSERT) from statement."""
    if not sql:
        return "QUERY"
    words = str(sql).strip().split()
    if words:
        op = words[0].upper()
        if op in _KNOWN_SQL_OPS:
            return op
    return "QUERY"


def extract_query_summary(sql: Optional[str]) -> str:
    """Extract a concise query summary (e.g. 'SELECT api_product', 'UPDATE api_product')."""
    if not sql:
        return "QUERY"
    cleaned = str(sql).strip()
    op = extract_operation(cleaned)
    match = _TABLE_RE.search(cleaned)
    if match:
        table = match.group(1).replace('"', "").replace("`", "")
        return f"{op} {table}"
    words = cleaned.split()
    if len(words) >= 2 and words[0].upper() in _KNOWN_SQL_OPS:
        return f"{words[0].upper()} {words[1]}"
    return op


def _extract_django_db_meta(instance_or_conn: Any) -> Tuple[str, str, str, str, int, Optional[str], str, str]:
    """Extract connection parameters and role from Django's db connection or CursorWrapper.

    Returns:
        (db_alias, db_vendor, db_name, db_host, db_port, db_user, db_role, peer_service)
    """
    # Check if this is a CursorWrapper (has .db) or a Connection object directly
    if hasattr(instance_or_conn, "db") and getattr(instance_or_conn, "db", None) is not None:
        db_conn = getattr(instance_or_conn, "db")
    else:
        db_conn = instance_or_conn

    db_alias = "default"
    db_vendor = "postgresql"
    db_name = "unknown"
    db_host = "localhost"
    db_port = 5432
    db_user = None

    if db_conn is not None:
        db_alias = getattr(db_conn, "alias", "default") or "default"
        db_vendor = getattr(db_conn, "vendor", "postgresql") or "postgresql"
        settings_dict = getattr(db_conn, "settings_dict", {})
        if isinstance(settings_dict, dict):
            db_name = settings_dict.get("NAME") or "unknown"
            db_host = settings_dict.get("HOST") or "localhost"
            raw_port = settings_dict.get("PORT")
            db_user = settings_dict.get("USER") or None
            try:
                db_port = int(raw_port) if raw_port else 5432
            except (ValueError, TypeError):
                db_port = 5432

    cfg = _get_config()
    db_role_map = cfg.db_role_map if cfg else {}
    alias_str = str(db_alias)
    if alias_str in db_role_map:
        db_role = db_role_map[alias_str]
    elif alias_str.lower() in db_role_map:
        db_role = db_role_map[alias_str.lower()]
    elif re.search(r"(?:^|[_\-])(?:replica|slave|readonly|read)(?:\d+|\d*db)?(?:$|[_\-])", alias_str, re.IGNORECASE):
        db_role = "replica"
    else:
        db_role = "primary"

    peer_service = f"postgres-{db_alias}" if db_alias and db_alias != "default" else "postgres"

    return db_alias, db_vendor, db_name, db_host, db_port, db_user, db_role, peer_service


def is_pgbouncer_connection(db_host: Any, db_port: Any) -> bool:
    """Determine if database connection is routed through PgBouncer."""
    host_str = str(db_host).lower() if db_host else ""
    try:
        port_num = int(db_port) if db_port else 0
    except (ValueError, TypeError):
        port_num = 0
    return host_str == "pgbouncer" or port_num == 6432 or "pgbouncer" in host_str


suppress_db_instrumentation = suppress_instrumentation


def _build_db_span_context(
    sql: Optional[str],
    instance_or_conn: Any,
) -> Tuple[str, Dict[str, Any]]:
    """Construct span name and attributes for a database query."""
    sanitized_sql = sanitize_sql(sql)
    metric_sql = normalize_sql_for_metric(sanitized_sql)
    op = extract_operation(sanitized_sql)
    db_alias, db_vendor, db_name, db_host, db_port, db_user, db_role, peer_service = _extract_django_db_meta(
        instance_or_conn
    )
    summary = extract_query_summary(sanitized_sql)
    is_pgbouncer = is_pgbouncer_connection(db_host, db_port)

    resolved_port = db_port
    if is_pgbouncer and (not db_port or str(db_port) == "0"):
        resolved_port = 6432

    span_attrs: Dict[str, Any] = {
        "db.system": db_vendor if db_vendor else "postgresql",
        "db.system.name": db_vendor if db_vendor else "postgresql",
        "db.name": str(db_name),
        "db.namespace": str(db_name),
        "db.instance": str(db_alias),
        "db.connection_alias": str(db_alias),
        "net.peer.name": str(db_host),
        "net.peer.port": resolved_port,
        "server.address": str(db_host),
        "server.port": resolved_port,
        "db.statement": metric_sql,
        "db.statement.full": sanitized_sql,
        "db.query.text": sanitized_sql,
        "db.query.summary": summary,
        "db.operation": op,
        "db.operation.name": op,
        "db.role": db_role,
        "resource.name": metric_sql,
        "normalized.service": "postgres",
        "normalized.operation": metric_sql or "postgres.query",
    }
    if is_pgbouncer:
        span_attrs["db.connection.pool"] = "pgbouncer"
    if db_user:
        span_attrs["db.user"] = str(db_user)
    span_name = f"🟢 {metric_sql}" if metric_sql else "🟢 postgres.query"

    return span_name, span_attrs


def _emit_db_span(span_name: str, span_attrs: dict, execute_fn: Callable, cursor_obj: Any) -> Any:
    with traced_span(
        span_name,
        kind=SpanKind.CLIENT,
        attributes=span_attrs,
        tracer_name="tp_trace.postgres",
    ) as span:
        with suppress_db_instrumentation():
            result = execute_fn()
        rowcount = attempt(getattr, cursor_obj, "rowcount", default=None, _label="db.rowcount")
        if isinstance(rowcount, int) and rowcount >= 0:
            safe_set_attribute(span, "db.row_count", rowcount)
            safe_set_attribute(span, "db.response.returned_rows", rowcount)
        return result


def tp_trace_django_db_execute_wrapper(
    execute: Callable,
    sql: str,
    params: Any,
    many: bool,
    context: Dict[str, Any],
) -> Any:
    """Official Django database execute wrapper (compatible with connection.execute_wrappers)."""
    if get_value(_SUPPRESS_KEY):
        return execute(sql, params, many, context)

    try:
        conn = context.get("connection") if isinstance(context, dict) else None
        cursor = context.get("cursor") if isinstance(context, dict) else None
        guard_obj = conn if conn is not None else cursor

        with reentrant_guard(guard_obj, "_tp_in_exec") as should_trace:
            if not should_trace:
                return execute(sql, params, many, context)

            db_ctx = attempt(_build_db_span_context, sql, conn, default=UNTRACED, _label="db_span_context")
            if db_ctx is UNTRACED:
                # Telemetry could not be built. The statement must still run.
                return execute(sql, params, many, context)
            span_name, span_attrs = db_ctx

            return _emit_db_span(span_name, span_attrs, lambda: execute(sql, params, many, context), cursor)
    except Exception as exc:
        # Never retry: a failing statement has already been sent to the server and
        # re-sending it would double side effects. Propagate instead.
        logger.debug("tp_trace DB execute wrapper error: %s", exc, exc_info=True)
        raise


def traced_django_cursor_exec(
    wrapped: Callable,
    instance: Any,
    args: Any,
    kwargs: Any,
    op_type: str = "execute",
    *extra_args: Any,
    **extra_kwargs: Any,
) -> Any:
    """Wrapper for Django CursorWrapper.execute and executemany."""
    if get_value(_SUPPRESS_KEY):
        return wrapped(*args, **kwargs)

    try:
        # Re-entrancy guard to avoid nested spans for the same logical query
        with reentrant_guard(instance, "_tp_in_exec") as should_trace:
            if not should_trace:
                return wrapped(*args, **kwargs)

            sql = args[0] if args else kwargs.get("sql", "")
            db_ctx = attempt(_build_db_span_context, sql, instance, default=UNTRACED, _label="db_span_context")
            if db_ctx is UNTRACED:
                # Telemetry could not be built. The statement must still run.
                return wrapped(*args, **kwargs)
            span_name, span_attrs = db_ctx

            cursor = attempt(getattr, instance, "cursor", default=instance, _label="db.cursor")
            return _emit_db_span(span_name, span_attrs, lambda: wrapped(*args, **kwargs), cursor)
    except Exception as exc:
        # Never retry: the statement has already been executed, and re-running it
        # would double side effects. Propagate instead.
        logger.debug("tp_trace cursor exec wrapper error: %s", exc, exc_info=True)
        raise
