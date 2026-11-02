"""Sanitization utilities for URLs and SQL statements."""

import re
from typing import Optional
from urllib.parse import urlparse, urlunparse

_WHITESPACE_RE = re.compile(r"\s+")
# Match numeric literals not attached to word characters (e.g. '= 42', ', 123.45', 'IN (1, 2)')
_NUMERIC_LITERAL_RE = re.compile(r"(?<=[^\w\$.])\b\d+(?:\.\d+)?\b")
# Match an IN/NOT IN predicate whose value list is purely a comma-separated run of
# placeholders, e.g. "IN (?, ?, ?)", "IN (%s, %s)", or "IN(?)".
_IN_PLACEHOLDER_LIST_RE = re.compile(
    r"\b(IN|NOT\s+IN)\s*\(\s*(?:\?|%s|\$\d+)(?:\s*,\s*(?:\?|%s|\$\d+))*\s*\)", re.IGNORECASE
)
_BATCH_VALUES_RE = re.compile(r"\bVALUES\s*\([^)]+\)(?:\s*,\s*\([^)]+\))+", re.IGNORECASE)
_SQL_TOKEN_RE = re.compile(r"--|/\*|[\'\"$]")
_DOLLAR_TAG_RE = re.compile(r"\$(?:[^\W\d]\w*)?\$")
_SAVEPOINT_RE = re.compile(
    r"\b(SAVEPOINT|RELEASE\s+SAVEPOINT|ROLLBACK\s+TO\s+SAVEPOINT)\s+([\"`']?)(?:s\d+_\w+|\w+)\2", re.IGNORECASE
)
_SEARCH_PATH_ITEM = r"(?:\"(?:\"\"|[^\"])*\"|\?|[^,;\s]+)"
_SEARCH_PATH_RE = re.compile(
    rf"\b(SET\s+(?:(?:SESSION|LOCAL)\s+)?)search_path\s*(?:TO\b|=)\s*"
    rf"{_SEARCH_PATH_ITEM}(?:\s*,\s*{_SEARCH_PATH_ITEM})*",
    re.IGNORECASE,
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


def _redact_sql_literals(sql: str, truncated: bool = False) -> str:
    """Scan bounded input once, respecting SQL lexical boundaries.

    A plain string's backslash semantics depend on the server setting. Reject
    that ambiguous case instead of guessing which quote closes the literal.
    Explicit PostgreSQL E-strings have known backslash escape semantics.
    If the scan budget cut off a construct, retain only the safe prefix before
    that construct. The caller marks the result as truncated.
    """
    parts = []
    i = 0
    size = len(sql)
    while i < size:
        token = _SQL_TOKEN_RE.search(sql, i)
        if token is None:
            parts.append(sql[i:])
            break
        parts.append(sql[i : token.start()])
        i = token.start()
        if sql.startswith("--", i):
            i += 2
            while i < size and sql[i] not in "\r\n":
                i += 1
            parts.append(" ")
        elif sql.startswith("/*", i):
            depth = 1
            i += 2
            while i < size and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                if truncated:
                    return "".join(parts)
                raise ValueError("unterminated SQL comment")
            parts.append(" ")
        elif sql[i] in ("'", '"'):
            quote = sql[i]
            start = i
            escaped = (
                quote == "'"
                and i > 0
                and sql[i - 1] in "eE"
                and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] in "_$"))
            )
            unicode_string = (
                quote == "'"
                and i >= 2
                and sql[i - 2 : i].lower() == "u&"
                and (i < 3 or not (sql[i - 3].isalnum() or sql[i - 3] in "_$"))
            )
            i += 1
            while i < size:
                if sql[i] == "\\" and quote == "'":
                    if unicode_string:
                        # U& strings require standard_conforming_strings=on;
                        # Unicode escapes do not change lexical quote boundaries.
                        i += 1
                    elif escaped:
                        i += 2
                    else:
                        raise ValueError("ambiguous SQL escape")
                elif sql[i] == quote:
                    if i + 1 < size and sql[i + 1] == quote:
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            else:
                if truncated:
                    return "".join(parts)
                raise ValueError("unterminated SQL quote")
            parts.append("?" if quote == "'" else sql[start:i])
        elif sql[i] == "$" and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in "_$")):
            tag = _DOLLAR_TAG_RE.match(sql, i)
            if tag is None:
                parts.append(sql[i])
                i += 1
                continue
            end = sql.find(tag.group(), tag.end())
            if end < 0:
                if truncated:
                    return "".join(parts)
                raise ValueError("unterminated dollar quote")
            parts.append("?")
            i = end + len(tag.group())
        else:
            parts.append(sql[i])
            i += 1
    return "".join(parts)


def sanitize_sql(sql: Optional[str], max_length: int = _MAX_STATEMENT_LENGTH) -> str:
    """
    Sanitize SQL statements by replacing literal values with placeholders.

    Replaces:
    - Postgres dollar-quoted strings: $$secret$$ or $tag$secret$tag$ -> ?
    - String literals: 'example' -> ?
    - SQL comments (including nested block comments) -> whitespace
    - Numeric literals: 42, 3.14 -> ?
    - Collapses placeholder list arity: 'IN (?, ?, ?)' -> 'IN (?)'
    - Parameterizes savepoint names: SAVEPOINT "s8597110336_x1" -> SAVEPOINT ?
    - Parameterizes search_path schemas: SET search_path = tenant,public -> SET search_path = ?
    - Normalizes multiple whitespace characters into a single space.
    - Pre-truncates to bound regex processing time on large queries.
    - Truncates excessively long queries to max_length.
    - Keeps a safe prefix with a truncation marker when the scan budget is exceeded.
    - Fail-closed: returns '<unparseable-sql>' on ambiguous or unterminated input
      within the scan budget.
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
    input_truncated = len(pre_truncated) < len(sql)

    try:
        sanitized = _redact_sql_literals(pre_truncated, truncated=input_truncated)

        # 3. Replace numeric literals
        sanitized = _NUMERIC_LITERAL_RE.sub("?", sanitized)

        # 4. Collapse arity of placeholder-only IN lists
        sanitized = _IN_PLACEHOLDER_LIST_RE.sub(r"\1 (?)", sanitized)

        # 5. Parameterize savepoint names (e.g. SAVEPOINT "s8597110336_x1" -> SAVEPOINT ?)
        sanitized = _SAVEPOINT_RE.sub(r"\1 ?", sanitized)

        # 6. Parameterize search_path (e.g. SET search_path = tenant,public -> SET search_path = ?)
        sanitized = _SEARCH_PATH_RE.sub(r"\1search_path = ?", sanitized)

        # 7. Normalize whitespace
        normalized = _WHITESPACE_RE.sub(" ", sanitized).strip()

        if len(normalized) > max_length:
            return normalized[:max_length] + " ... [truncated]"
        if input_truncated:
            return normalized + " ... [truncated]"
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
    # Scan with the statement budget, independently of the shorter metric label
    # budget. Cutting a wide SELECT at the metric budget can split a quoted
    # identifier and incorrectly classify valid Django SQL as unparseable.
    sanitized = sanitize_sql(sql, max_length=max(_MAX_STATEMENT_LENGTH, max_length * 2))
    return _normalize_sanitized_sql(sanitized, max_length=max_length)


def _normalize_sanitized_sql(sanitized: str, max_length: int = _MAX_METRIC_STATEMENT_LENGTH) -> str:
    """Derive a bounded fingerprint from already-redacted SQL; never pass raw SQL.

    This deliberately does not lex the input again: a bounded display statement
    can end inside an identifier without making the original SQL invalid.
    """
    if not sanitized or sanitized == "<unparseable-sql>":
        return sanitized

    try:
        cleaned = sanitized
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
        # No part of a malformed URL is safe to return as-is.
        return "<unparseable-url>"
