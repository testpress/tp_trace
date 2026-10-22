"""Sanitization utilities for URLs and SQL statements."""

import re
from typing import Optional
from urllib.parse import urlparse, urlunparse

_WHITESPACE_RE = re.compile(r"\s+")
_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
# Match numeric literals not attached to word characters (e.g. '= 42', ', 123.45', 'IN (1, 2)')
_NUMERIC_LITERAL_RE = re.compile(r"(?<=[^\w\$.])\b\d+(?:\.\d+)?\b")
# Match an IN/NOT IN predicate whose value list is purely a comma-separated run of
# placeholders, e.g. "IN (?, ?, ?)", "IN (%s, %s)", or "IN(?)".
_IN_PLACEHOLDER_LIST_RE = re.compile(
    r"\b(IN|NOT\s+IN)\s*\(\s*(?:\?|%s|\$\d+)(?:\s*,\s*(?:\?|%s|\$\d+))*\s*\)", re.IGNORECASE
)
_BATCH_VALUES_RE = re.compile(r"\bVALUES\s*\([^)]+\)(?:\s*,\s*\([^)]+\))+", re.IGNORECASE)
_SQL_COMMENTS_RE = re.compile(r"/\*.*?\*/|--[^\r\n]*")
_DOLLAR_QUOTE_RE = re.compile(r"\$(\w*)\$.*?\$\1\$", re.DOTALL)
_UNCLOSED_DOLLAR_QUOTE_RE = re.compile(r"\$[a-zA-Z_]\w*\$|\$\$")
_SAVEPOINT_RE = re.compile(
    r"\b(SAVEPOINT|RELEASE\s+SAVEPOINT|ROLLBACK\s+TO\s+SAVEPOINT)\s+([\"`']?)(?:s\d+_\w+|\w+)\2", re.IGNORECASE
)
_MAX_STATEMENT_LENGTH = 4096
_MAX_METRIC_STATEMENT_LENGTH = 256

_SENSITIVE_QUERY_KEYS = {
    "token",
    "auth",
    "password",
    "pass",
    "secret",
    "key",
    "apikey",
    "api_key",
    "access_token",
    "refresh_token",
    "id_token",
    "session",
    "sessionid",
    "code",
    "sig",
    "signature",
    "credential",
    "bearer",
    "private_key",
}


def sanitize_sql(sql: Optional[str], max_length: int = _MAX_STATEMENT_LENGTH) -> str:
    """
    Sanitize SQL statements by replacing literal values with placeholders.

    Replaces:
    - Postgres dollar-quoted strings: $$secret$$ or $tag$secret$tag$ -> ?
    - String literals: 'example' -> ?
    - Numeric literals: 42, 3.14 -> ?
    - Collapses placeholder list arity: 'IN (?, ?, ?)' -> 'IN (?)'
    - Normalizes multiple whitespace characters into a single space.
    - Pre-truncates to bound regex processing time on large queries.
    - Truncates excessively long queries to max_length.
    - Fail-closed: returns '<unparseable-sql>' on unparseable/unterminated constructs.
    """
    if not sql:
        return ""
    if not isinstance(sql, str):
        try:
            sql = str(sql)
        except Exception:
            return "<unparseable-sql>"

    # Pre-truncate to bound regex execution time (O(max_length))
    pre_truncated = sql[: max_length * 4]

    try:
        # 1. Replace dollar-quoted strings first
        sanitized = _DOLLAR_QUOTE_RE.sub("?", pre_truncated)
        # Check for unclosed dollar quote tags
        if _UNCLOSED_DOLLAR_QUOTE_RE.search(sanitized):
            return "<unparseable-sql>"

        # 2. Replace string literals
        sanitized = _STRING_LITERAL_RE.sub("?", sanitized)

        # 3. Replace numeric literals
        sanitized = _NUMERIC_LITERAL_RE.sub("?", sanitized)

        # 4. Collapse arity of placeholder-only IN lists
        sanitized = _IN_PLACEHOLDER_LIST_RE.sub(r"\1 (?)", sanitized)

        # 5. Parameterize savepoint names (e.g. SAVEPOINT "s8597110336_x1" -> SAVEPOINT ?)
        sanitized = _SAVEPOINT_RE.sub(r"\1 ?", sanitized)

        # 6. Normalize whitespace
        normalized = _WHITESPACE_RE.sub(" ", sanitized).strip()

        if len(normalized) > max_length:
            return normalized[:max_length] + " ... [truncated]"
        return normalized
    except Exception:
        return "<unparseable-sql>"


def normalize_sql_for_metric(sql: Optional[str], max_length: int = _MAX_METRIC_STATEMENT_LENGTH) -> str:
    """
    Produce a canonical, bounded query fingerprint for metric labels (Prometheus spanmetrics).

    Guarantees:
    - Replaces dynamic IN (?, ?, ...) / IN (%s, %s) arity with IN (?)
    - Collapses multi-row batch inserts VALUES (...), (...) into VALUES (...)
    - Strips inline SQL comments
    - Collapses whitespace
    - Hard bounds string length to max_length (default 256) to cap Prometheus label memory
    """
    if not sql:
        return ""
    sanitized = sanitize_sql(sql, max_length=max_length * 2)
    if sanitized == "<unparseable-sql>":
        return sanitized

    try:
        # Strip comments
        cleaned = _SQL_COMMENTS_RE.sub("", sanitized)
        # Collapse batch insert VALUES (...), (...)
        cleaned = _BATCH_VALUES_RE.sub("VALUES (...)", cleaned)
        # Collapse IN placeholder lists
        cleaned = _IN_PLACEHOLDER_LIST_RE.sub(r"\1 (?)", cleaned)
        # Normalize whitespace
        normalized = _WHITESPACE_RE.sub(" ", cleaned).strip()

        if len(normalized) > max_length:
            return normalized[: max_length - 3] + "..."
        return normalized
    except Exception:
        return "<unparseable-sql>"


def sanitize_query_string(query: Optional[str]) -> str:
    """
    Sanitize a URL query string by replacing values of sensitive parameters with REDACTED.
    """
    if not query:
        return ""
    from urllib.parse import parse_qsl, urlencode

    try:
        pairs = parse_qsl(query, keep_blank_values=True)
        sanitized_pairs = []
        for k, v in pairs:
            k_lower = k.lower()
            if k_lower in _SENSITIVE_QUERY_KEYS or any(
                s in k_lower for s in ("secret", "token", "password", "apikey", "api_key", "auth")
            ):
                sanitized_pairs.append((k, "REDACTED"))
            else:
                sanitized_pairs.append((k, v))
        return urlencode(sanitized_pairs)
    except Exception:
        return ""


def sanitize_url(url: Optional[str], strip_query: bool = False, mode: Optional[str] = None) -> str:
    """
    Sanitize a URL by stripping credentials (userinfo) and fragment.

    Query handling is selected by ``mode`` (takes precedence over ``strip_query``):

    - "expose": raw query string is kept verbatim (default SDK posture).
    - "redact": sensitive query params (token/password/...) -> REDACTED.
    - "strip":  query string is omitted entirely.
    - None (default, backwards compatible): "strip" if strip_query else "redact".

    Userinfo stripping and the fail-closed ``<unparseable-url>`` fallback apply
    in every mode.
    """
    if not url:
        return ""
    if mode is None:
        mode = "strip" if strip_query else "redact"
    try:
        parsed = urlparse(url)
        # Strip userinfo (username:password) if present
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = netloc.split("@")[-1]

        if mode == "strip":
            query = ""
        elif mode == "expose":
            query = parsed.query
        else:
            # "redact" and any unknown mode fail closed to redaction
            query = sanitize_query_string(parsed.query) if parsed.query else ""

        cleaned = urlunparse(
            (
                parsed.scheme,
                netloc,
                parsed.path,
                parsed.params,
                query,
                "",  # strip fragment
            )
        )
        return cleaned
    except Exception:
        # Never return raw URL on parse error to avoid leaking query tokens or credentials
        try:
            clean = str(url).split("?", 1)[0].split("#", 1)[0]
            return clean if clean else "<unparseable-url>"
        except Exception:
            return "<unparseable-url>"
