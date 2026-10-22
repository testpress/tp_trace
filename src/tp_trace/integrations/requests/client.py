"""Outgoing HTTP request tracing wrapper for the `requests` library."""

import logging
from typing import Any, Tuple
from urllib.parse import urlparse

from opentelemetry.trace import StatusCode

from tp_trace.config_access import get_config as _get_config, set_config as set_config
from tp_trace.sanitize import sanitize_url
from tp_trace.route_context import get_current_route
from tp_trace.safety import attempt

logger = logging.getLogger("tp_trace.integrations.requests")


def _extract_request_meta(request: Any) -> Tuple[str, str, str, str, int, str]:
    """
    Extract method, sanitized URL, scheme, hostname, port, and peer_service from PreparedRequest.

    Returns:
        (method, sanitized_url, scheme, hostname, port, peer_service)
    """
    raw_method = getattr(request, "method", "GET") or "GET"
    method = str(raw_method).upper()

    raw_url = getattr(request, "url", "") or ""
    # expose (default) keeps the raw query string, matching dd-trace's client
    # default (DD_TRACE_HTTP_CLIENT_TAG_QUERY_STRING=true). Userinfo is always
    # stripped regardless of mode.
    cfg = _get_config()
    query_mode = getattr(cfg, "query_mode", "expose") if cfg else "expose"
    sanitized = sanitize_url(raw_url, mode=query_mode)

    try:
        parsed = urlparse(sanitized)
        scheme = parsed.scheme or "http"
        hostname = parsed.hostname or "localhost"
        if parsed.port:
            port = parsed.port
        else:
            port = 443 if scheme == "https" else 80
    except Exception:
        scheme = "http"
        hostname = "localhost"
        port = 80

    peer_service = hostname
    return method, sanitized, scheme, hostname, port, peer_service



def _apply_request_attributes(span: Any, request: Any) -> None:
    """Set tp_trace attributes on an outgoing requests span. May raise."""
    method, sanitized_url, scheme, hostname, port, peer_service = _extract_request_meta(request)
    parsed = urlparse(sanitized_url)
    clean_target = f"{scheme}://{hostname}{parsed.path or ''}"

    span.set_attribute("http.request.method", method)
    span.set_attribute("http.method", method)
    span.set_attribute("url.full", sanitized_url)
    span.set_attribute("http.url", sanitized_url)
    span.set_attribute("url.scheme", scheme)
    target = f"{parsed.path or '/'}{'?' + parsed.query if parsed.query else ''}"
    span.set_attribute("http.target", target)
    if parsed.query:
        span.set_attribute("url.query", parsed.query)
    span.set_attribute("server.address", hostname)
    span.set_attribute("server.port", port)
    span.set_attribute("net.peer.name", hostname)
    span.set_attribute("net.peer.port", port)
    span.set_attribute("peer.service", peer_service)
    span.set_attribute("resource.name", f"{method} {clean_target}")
    span.set_attribute("normalized.service", "requests")
    span.set_attribute("normalized.operation", f"HTTP {method}")

    # Tag the outgoing call with the in-flight Django endpoint so
    # per-endpoint spanmetrics series (http_route label) include it.
    route = get_current_route()
    if route:
        span.set_attribute("http.route", route)

    if hasattr(span, "update_name"):
        span.update_name(f"🌐 HTTP {method} {hostname}")


def tp_trace_request_hook(span: Any, request: Any) -> None:
    """Request hook for RequestsInstrumentor to sanitize URLs and set tp_trace attributes.

    OTel's requests instrumentor calls this hook *before* ``wrapped_send`` and
    does not guard it, so a fault here would block the outgoing HTTP call
    entirely. The whole body therefore sits behind a single guard: losing the
    attributes is acceptable, losing the request is not.
    """
    if span is None or not getattr(span, "is_recording", lambda: True)():
        return

    attempt(_apply_request_attributes, span, request, _label="request_hook")


tp_dog_request_hook = tp_trace_request_hook


def _apply_response_attributes(span: Any, response: Any) -> None:
    """Set status and error attributes on a requests span. May raise."""
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        return

    status_code = int(status_code)
    span.set_attribute("http.response.status_code", status_code)
    span.set_attribute("http.status_code", status_code)

    if status_code >= 400:
        span.set_attribute("error", True)
        span.set_attribute("error.type", f"HTTP{status_code}")
        span.set_status(StatusCode.ERROR, description=f"HTTP {status_code}")


def tp_trace_response_hook(span: Any, request: Any, response: Any) -> None:
    """Response hook for RequestsInstrumentor to set response status code and error flags.

    The instrumentor calls this outside its own try/except, so a fault here
    would raise in the application *after* the request already succeeded.
    """
    if span is None or not getattr(span, "is_recording", lambda: True)():
        return

    attempt(_apply_response_attributes, span, response, _label="response_hook")


tp_dog_response_hook = tp_trace_response_hook


