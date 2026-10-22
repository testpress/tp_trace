"""Django request-response tracing middleware / wrapper for BaseHandler.get_response."""

import functools
import inspect
import logging
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from opentelemetry.trace import SpanKind, StatusCode, get_tracer
from opentelemetry.propagate import extract

from tp_trace.config_access import get_config as _get_config, set_config as set_config
from tp_trace.safety import attempt, safe_enrich, safe_record_exception, safe_set_attribute, safe_set_status
from tp_trace.sanitize import sanitize_url
from tp_trace.route_context import (
    get_current_route,
    get_current_tags,
    reset_request_route,
    reset_request_tags,
    reset_request_view,
    set_request_route,
    set_request_tag,
    set_request_view,
)
from tp_trace.tracing import reentrant_guard

logger = logging.getLogger("tp_trace.integrations.django")

_CALLBACK_ARITY_CACHE: Dict[int, bool] = {}


@functools.lru_cache(maxsize=256)
def _inspect_takes_response(callback: Any) -> bool:
    try:
        sig = inspect.signature(callback)
        takes = len(sig.parameters) >= 3
    except (ValueError, TypeError):
        takes = False
    _CALLBACK_ARITY_CACHE[id(callback)] = takes
    return takes


def _callback_takes_response(callback: Any) -> bool:
    """Return True if callback accepts 3 or more arguments, cached by callable."""
    try:
        return _inspect_takes_response(callback)
    except TypeError:
        # Unhashable callable fallback
        try:
            return len(inspect.signature(callback).parameters) >= 3
        except Exception:
            return False


def _apply_static_tags(span: Any) -> None:
    """Apply static tags configured on SDKConfig."""
    cfg = attempt(_get_config, default=None, _label="request_config")
    if cfg and cfg.tags:
        safe_enrich(span, cfg.tags)


def _apply_request_callback(span: Any, request: Any = None, response: Any = None) -> None:
    """Invoke the on_request_span callback. Self-contained: never raises."""
    cfg = attempt(_get_config, default=None, _label="request_config")
    callback = getattr(cfg, "on_request_span", None)
    if callback and request is not None:

        def _invoke() -> None:
            if response is not None and _callback_takes_response(callback):
                callback(span, request, response)
                return
            callback(span, request)

        attempt(_invoke, _label="on_request_span")

    try:
        inst = getattr(request, "institute", None)
        if inst and hasattr(inst, "subdomain") and inst.subdomain:
            sub = str(inst.subdomain)
            safe_set_attribute(span, "institute.subdomain", sub)
            safe_set_attribute(span, "institute", sub)
            set_request_tag("institute.subdomain", sub)
            set_request_tag("institute", sub)
    except Exception:
        pass


def _apply_custom_tags(span: Any, request: Any = None, on_request_span: Any = None) -> None:
    """Apply static tags and the user callback. Self-contained: never raises."""
    _apply_static_tags(span)
    callback = on_request_span or getattr(
        attempt(_get_config, default=None, _label="request_config"), "on_request_span", None
    )
    if callback and request is not None:
        attempt(callback, span, request, _label="on_request_span")


_PARAM_REGEX = re.compile(r"<(?:[a-zA-Z0-9_]+:)?([a-zA-Z0-9_]+)>")
_TRAILING_SLASH = re.compile(r"/+$")
_MULTIPLE_SLASHES = re.compile(r"/+")


def _format_trace_id(trace_id_int: int) -> str:
    """Format 128-bit integer trace_id to 32-hex string."""
    return f"{trace_id_int:032x}"


def _format_span_id(span_id_int: int) -> str:
    """Format 64-bit integer span_id to 16-hex string."""
    return f"{span_id_int:016x}"


def _clean_ip(ip: str) -> str:
    """Clean IP by removing port suffix or bracketed IPv6."""
    cleaned = ip.strip()
    if cleaned.startswith("[") and "]" in cleaned:
        cleaned = cleaned[1:].split("]")[0]
    elif ":" in cleaned and cleaned.count(":") == 1:
        cleaned = cleaned.split(":")[0]
    return cleaned.strip()


def _resolve_client_address(request: Any, trusted_proxies: Optional[list] = None) -> Optional[str]:
    """Resolve real client IP address walking X-Forwarded-For right-to-left against trusted proxies."""
    if not hasattr(request, "META") or not isinstance(request.META, dict):
        return None
    remote = request.META.get("REMOTE_ADDR")
    remote_ip = _clean_ip(str(remote)) if remote else None
    if not remote_ip:
        return None

    trusted = set(trusted_proxies or [])
    if not trusted or remote_ip not in trusted:
        return remote_ip

    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if not xff:
        return remote_ip

    raw_hops = [h.strip() for h in str(xff).split(",") if h.strip()]
    if not raw_hops:
        return remote_ip

    # Walk right-to-left: the rightmost hop not in trusted proxies is the real client
    for hop in reversed(raw_hops):
        clean_hop = _clean_ip(hop)
        if clean_hop and clean_hop not in trusted:
            return clean_hop

    return remote_ip


def _clean_regex_pattern(pattern: str) -> str:
    """Clean regex url pattern down to normalized route template."""
    p = str(pattern).lstrip("^").rstrip("$")
    p = re.sub(r"\(\?P<([^>]+)>[^\)]+\)", r"<\1>", p)
    p = _PARAM_REGEX.sub(r"<\1>", p)
    # Clean up escaped slashes or dots: \/ -> /, \. -> .
    p = p.replace(r"\/", "/").replace(r"\.", ".")

    # Clean up any leftover regex syntax (e.g. \w+, [0-9]+)
    p = re.sub(r"\[[^\]]+\]\+?", "*", p)
    p = re.sub(r"\\d\+?", "*", p)
    p = re.sub(r"\\w\+?", "*", p)

    if not p:
        return "/"
    if not p.startswith("/"):
        p = f"/{p}"
    return p


def _finalize_route_str(route_str: str) -> str:
    """Shared tail of route normalization.

    Regex-clean a raw URL pattern when needed, then ensure a leading slash.
    Used by both the pre-handler and post-handler route paths so they cannot
    drift apart.
    """
    if "^" in route_str or "(?P<" in route_str or "\\" in route_str or "$" in route_str:
        route_str = _clean_regex_pattern(route_str)
    if route_str and not route_str.startswith("/") and route_str != "__unmatched__":
        route_str = f"/{route_str}"
    return route_str


def _normalize_route(request, fallback_path: str) -> str:
    """Low-cardinality http.route from resolver_match, never raw URL with IDs."""
    resolver_match = getattr(request, "resolver_match", None)
    route = None
    if resolver_match:
        if getattr(resolver_match, "route", None):
            raw_route = str(resolver_match.route)
            if "^" in raw_route or "(?P<" in raw_route or "\\" in raw_route or "$" in raw_route:
                route = _clean_regex_pattern(raw_route)
            else:
                route = raw_route
        elif hasattr(resolver_match, "_urlpattern") and hasattr(resolver_match._urlpattern, "pattern"):
            try:
                pat = resolver_match._urlpattern.pattern  # type: ignore
                raw_pattern = pat.regex.pattern if hasattr(pat, "regex") else str(pat)
                route = _clean_regex_pattern(raw_pattern)
            except Exception:
                pass
        elif getattr(resolver_match, "url_name", None):
            route = resolver_match.url_name

    # If resolver_match was not populated, try resolving with candidate/request urlconfs
    if not route and request:
        try:
            resolve_path = getattr(request, "path_info", None) or getattr(request, "path", None) or fallback_path
            urlconf = getattr(request, "urlconf", None)
            cand_route = _preresolve_route(resolve_path, urlconf=urlconf, request=request)
            if cand_route and cand_route != "__unmatched__":
                route = cand_route
        except Exception:
            pass

    if not route:
        route = "__unmatched__"
    return _finalize_route_str(str(route))


def _prime_request(span: Any, request: Any) -> Any:
    """Bind the span and trace ids onto the request before the handler runs.

    Returns the span's ``SpanContext`` when it could be read, else ``None``.
    The caller needs those ids for the ``X-Trace-ID`` / ``traceparent``
    response headers, which must survive a failure to bind onto the request.

    This runs *before* the application call, so every step is guarded
    individually. A hostile span object, a request with ``__slots__``, or a
    read-only ``trace_id`` property must degrade to "run the application
    anyway", never prevent it from executing at all.
    """
    span_ctx = attempt(span.get_span_context, default=None, _label="prime_span_context")

    def _bind() -> None:
        if span_ctx is not None:
            request.trace_id = _format_trace_id(span_ctx.trace_id)
            request.span_id = _format_span_id(span_ctx.span_id)
        _apply_static_tags(span)

    attempt(_bind, _label="prime_request")
    return span_ctx


def _preresolve_route(path: str, urlconf: Any = None, request: Any = None) -> str:
    """Best-effort normalized route resolved BEFORE the handler runs.

    Child spans (DB, cache, outgoing HTTP, S3) end before the SERVER span
    learns its route from ``resolver_match``; they read the route published
    from here via context vars instead. Falls back to "__unmatched__" when the
    URLconf cannot resolve it (unmatched URL, Django not fully set up).

    ``path`` must be ``request.path_info`` (SCRIPT_NAME already stripped), which
    is what Django itself resolves against -- passing ``request.path`` fails
    outright on any sub-path mount. ``urlconf`` honours a per-request URLconf
    installed by middleware.
    """
    try:
        from django.urls import resolve
        from django.conf import settings

        candidates = []
        if urlconf and urlconf not in candidates:
            candidates.append(urlconf)
        if request and getattr(request, "urlconf", None) and getattr(request, "urlconf", None) not in candidates:
            candidates.append(getattr(request, "urlconf"))

        root_urlconf = getattr(settings, "ROOT_URLCONF", None)
        if root_urlconf and root_urlconf not in candidates:
            candidates.append(root_urlconf)

        for attr in ("TENANT_URLCONF", "PUBLIC_SCHEMA_URLCONF"):
            extra = getattr(settings, attr, None)
            if extra and extra not in candidates:
                candidates.append(extra)

        extra_confs = getattr(settings, "TP_TRACE_EXTRA_URLCONFS", None)
        if isinstance(extra_confs, (list, tuple)):
            for conf in extra_confs:
                if conf and conf not in candidates:
                    candidates.append(conf)

        for conf in candidates:
            try:
                match = resolve(path, urlconf=conf)
                route = getattr(match, "route", None)
                if not route and hasattr(match, "_urlpattern") and hasattr(match._urlpattern, "pattern"):
                    pat = match._urlpattern.pattern
                    route = pat.regex.pattern if hasattr(pat, "regex") else str(pat)
                elif not route and getattr(match, "url_name", None):
                    route = match.url_name

                if route:
                    return _finalize_route_str(str(route))
            except Exception:
                continue

        return "__unmatched__"
    except Exception:
        return "__unmatched__"


def _format_view_from_match(match: Any, method: str = "") -> Optional[str]:
    if not match:
        return None
    view_func = getattr(match, "func", None)
    cls = getattr(view_func, "cls", getattr(view_func, "view_class", None)) if view_func else None
    req_method = method.lower() if method else ""
    actions = getattr(view_func, "actions", {}) if view_func else {}
    if cls:
        cls_mod = getattr(cls, "__module__", "")
        cls_name = getattr(cls, "__name__", "View")
        prefix = f"{cls_mod}.{cls_name}" if cls_mod and cls_mod != "builtins" else cls_name
        if isinstance(actions, dict) and req_method and req_method in actions:
            return f"{prefix}.{actions[req_method]}"
        if req_method and hasattr(cls, req_method):
            return f"{prefix}.{req_method}"
        return prefix
    if hasattr(match, "_func_path") and match._func_path:
        return str(match._func_path)
    if view_func:
        func_mod = getattr(view_func, "__module__", "")
        func_name = getattr(view_func, "__name__", "")
        if func_mod and func_name and func_mod != "builtins":
            return f"{func_mod}.{func_name}"
        if func_name:
            return str(func_name)
    if getattr(match, "view_name", None):
        return str(match.view_name)
    return None


def _preresolve_view(path: str, urlconf: Any = None, request: Any = None, method: str = "") -> Optional[str]:
    try:
        from django.urls import resolve
        from django.conf import settings

        candidates = []
        if urlconf and urlconf not in candidates:
            candidates.append(urlconf)
        if request and getattr(request, "urlconf", None) and getattr(request, "urlconf", None) not in candidates:
            candidates.append(getattr(request, "urlconf"))

        root_urlconf = getattr(settings, "ROOT_URLCONF", None)
        if root_urlconf and root_urlconf not in candidates:
            candidates.append(root_urlconf)

        for attr in ("TENANT_URLCONF", "PUBLIC_SCHEMA_URLCONF"):
            extra = getattr(settings, attr, None)
            if extra and extra not in candidates:
                candidates.append(extra)

        extra_confs = getattr(settings, "TP_TRACE_EXTRA_URLCONFS", None)
        if isinstance(extra_confs, (list, tuple)):
            for conf in extra_confs:
                if conf and conf not in candidates:
                    candidates.append(conf)

        for conf in candidates:
            try:
                match = resolve(path, urlconf=conf)
                view_str = _format_view_from_match(match, method=method)
                if view_str:
                    return view_str
            except Exception:
                continue
        return None
    except Exception:
        return None


def _resolve_view_name(request, method: str) -> str:
    resolver_match = getattr(request, "resolver_match", None)
    if not resolver_match:
        return "view"
    view_func = getattr(resolver_match, "func", None)
    cls = None
    if view_func is not None:
        cls = getattr(view_func, "cls", getattr(view_func, "view_class", None))
    actions = getattr(view_func, "actions", {}) if view_func else {}
    req_method = method.lower()
    if cls and isinstance(actions, dict) and req_method in actions:
        return f"{cls.__name__}.{actions[req_method]}"
    elif cls:
        return cls.__name__
    elif hasattr(resolver_match, "_func_path"):
        return str(resolver_match._func_path).split(".")[-1]
    elif view_func and hasattr(view_func, "__name__"):
        return str(view_func.__name__)
    elif getattr(resolver_match, "view_name", None):
        return str(resolver_match.view_name)
    return "view"


def traced_get_response(wrapped, instance, args, kwargs):
    """Wrapper for BaseHandler.get_response — SERVER span + metrics."""
    request = args[0] if args else kwargs.get("request")
    if request is None:
        return wrapped(*args, **kwargs)

    # Re-entrancy guard: same request object can be re-entered if middleware
    # calls get_response recursively. The flag lives on the request object
    # (per-request, not global), preventing nested SERVER spans for the same
    # logical request.
    with reentrant_guard(request, "_tp_traced_request") as should_trace:
        if not should_trace:
            return wrapped(*args, **kwargs)

        reset_request_tags()
        try:
            inst = getattr(request, "institute", None)
            if inst and hasattr(inst, "subdomain") and inst.subdomain:
                set_request_tag("institute.subdomain", str(inst.subdomain))
                set_request_tag("institute", str(inst.subdomain))
        except Exception:
            pass

        tracer = get_tracer("tp_trace.django")
        method = getattr(request, "method", "GET").upper()
        path = getattr(request, "path", "/")
        scheme = getattr(request, "scheme", "http")

        # W3C propagation
        carrier: Dict[str, str] = {}
        if hasattr(request, "headers"):
            try:
                carrier.update({k.lower(): str(v) for k, v in request.headers.items()})
            except Exception:
                pass
        elif hasattr(request, "META") and isinstance(request.META, dict):
            for k, v in request.META.items():
                if k.startswith("HTTP_"):
                    carrier[k[5:].replace("_", "-").lower()] = str(v)
                elif k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                    carrier[k.replace("_", "-").lower()] = str(v)

        cfg = attempt(_get_config, default=None, _label="request_config")
        extract_setting = getattr(cfg, "extract_trace_context", True) if cfg else True
        if callable(extract_setting):
            try:
                should_extract = bool(extract_setting(request))
            except Exception:
                logger.debug("extract_trace_context callable raised", exc_info=True)
                should_extract = True
        else:
            should_extract = bool(extract_setting)

        parent_ctx = extract(carrier) if should_extract else None

        try:
            raw_url = request.build_absolute_uri() if hasattr(request, "build_absolute_uri") else path
        except Exception:
            raw_url = path
        query_mode = getattr(cfg, "query_mode", "expose") if cfg else "expose"
        sanitized = sanitize_url(raw_url, mode=query_mode)
        url_path = path
        url_query = ""
        try:
            parsed = urlparse(sanitized)
            url_path = parsed.path or path
            url_query = parsed.query
        except Exception:
            pass

        target_url = f"{url_path}?{url_query}" if url_query else url_path
        ua = carrier.get("user-agent", "")
        span_attrs: Dict[str, Any] = {
            "span.type": "web",
            "component": "django",
            "http.request.method": method,
            "http.method": method,
            "http.route": sanitized or target_url or url_path,
            "url.full": sanitized,
            "http.url": sanitized,
            "url.path": url_path,
            "http.target": target_url,
            "url.scheme": scheme,
            "user_agent.original": ua,
            "http.user_agent": ua,
        }
        if not should_extract and carrier.get("traceparent"):
            span_attrs["http.client.traceparent"] = carrier["traceparent"]
        if url_query:
            span_attrs["url.query"] = url_query
        if hasattr(request, "META") and isinstance(request.META, dict):
            trusted = cfg.trusted_proxies if cfg else []
            client_ip = _resolve_client_address(request, trusted)
            if client_ip:
                span_attrs["client.address"] = str(client_ip)
                span_attrs["http.client_ip"] = str(client_ip)
            remote_port = request.META.get("REMOTE_PORT")
            if remote_port:
                try:
                    span_attrs["client.port"] = int(remote_port)
                except Exception:
                    pass
            host = request.META.get("HTTP_HOST") or request.META.get("SERVER_NAME")
            if host:
                span_attrs["server.address"] = str(host).split(":")[0]
            server_proto = request.META.get("SERVER_PROTOCOL")
            if server_proto:
                span_attrs["network.protocol.version"] = str(server_proto)
                span_attrs["http.flavor"] = str(server_proto)

        # Resolve against path_info (what Django itself resolves against),
        # never request.path -- the latter still carries SCRIPT_NAME, so on a
        # sub-path mount every route pre-resolves to __unmatched__ and, via
        # RouteEnrichingSpanProcessor, poisons every child span in the request.
        # A per-request URLconf set by middleware is honoured too.
        resolve_path = getattr(request, "path_info", None) or path
        preresolved_route = _preresolve_route(resolve_path, getattr(request, "urlconf", None), request=request)
        if preresolved_route and preresolved_route != "__unmatched__":
            span_attrs["http.route_template"] = preresolved_route

        start = time.monotonic()
        final_route = preresolved_route if (preresolved_route and preresolved_route != "__unmatched__") else path

        # Publish the pre-resolved route so child spans started inside the
        # handler carry http.route even though resolver_match is only
        # populated after the handler returns. Never publish __unmatched__
        # as it would poison child spans.
        route_to_publish = preresolved_route if (preresolved_route and preresolved_route != "__unmatched__") else None
        route_token = set_request_route(route_to_publish, method)
        preresolved_view = _preresolve_view(
            resolve_path, getattr(request, "urlconf", None), request=request, method=method
        )
        view_token = set_request_view(preresolved_view)
        start_span_name = (
            f"{method} {preresolved_route}"
            if (preresolved_route and preresolved_route != "__unmatched__")
            else f"{method} {path}"
        )
        final_span_name = start_span_name
        status_code = 500
        error = False
        metric_recorded = False
        try:
            with tracer.start_as_current_span(
                start_span_name,
                context=parent_ctx,
                kind=SpanKind.SERVER,
                attributes=span_attrs,
                # This wrapper records errors itself on the paths below, so OTel's
                # automatic handling is disabled -- otherwise the exception is
                # recorded twice and set_status is called twice for one failure.
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                span_ctx = _prime_request(span, request)
                trace_id_hex = _format_trace_id(span_ctx.trace_id) if span_ctx is not None else ""
                span_id_hex = _format_span_id(span_ctx.span_id) if span_ctx is not None else ""

                status_code = 200
                error = False
                route_for_metrics = path
                # The application call is isolated from all telemetry enrichment.
                # An exception here is the application's own and must propagate.
                # Enrichment happens afterwards and can never turn a successful
                # response into an error, nor raise into the host application.
                try:
                    response = wrapped(*args, **kwargs)
                except Exception as exc:
                    error = True
                    status_code = 500
                    route_for_metrics = attempt(
                        _normalize_route, request, path, default="__unmatched__", _label="normalize_route"
                    )
                    if not route_for_metrics or route_for_metrics == "__unmatched__":
                        current_ctx_route = get_current_route()
                        if current_ctx_route and current_ctx_route != "__unmatched__":
                            route_for_metrics = current_ctx_route
                        elif preresolved_route and preresolved_route != "__unmatched__":
                            route_for_metrics = preresolved_route
                    final_span_name = (
                        f"{method} {status_code}"
                        if (not route_for_metrics or route_for_metrics == "__unmatched__")
                        else f"{method} {route_for_metrics}"
                    )
                    final_route = (
                        str(status_code)
                        if (not route_for_metrics or route_for_metrics == "__unmatched__")
                        else route_for_metrics
                    )
                    attempt(span.update_name, final_span_name, _label="update_span_name")
                    safe_set_attribute(span, "http.route", sanitized or target_url or url_path)
                    safe_set_attribute(span, "http.route_template", final_route)
                    if route_for_metrics and route_for_metrics != "__unmatched__":
                        safe_set_attribute(span, "django.route", route_for_metrics)
                    safe_set_attribute(span, "resource.name", final_span_name)
                    safe_set_attribute(span, "normalized.service", "django")
                    safe_set_attribute(span, "normalized.operation", final_span_name)
                    safe_set_attribute(span, "http.response.status_code", 500)
                    safe_set_attribute(span, "http.status_code", 500)
                    safe_set_attribute(span, "error", True)
                    safe_set_attribute(span, "error.type", exc.__class__.__name__)
                    safe_record_exception(span, exc)
                    safe_set_status(span, StatusCode.ERROR, description=str(exc))
                    _apply_request_callback(span, request)
                    raise
                else:
                    status_code = attempt(getattr, response, "status_code", default=200, _label="response.status_code")
                    norm_route = attempt(
                        _normalize_route, request, path, default="__unmatched__", _label="normalize_route"
                    )
                    if not norm_route or norm_route == "__unmatched__":
                        current_ctx_route = get_current_route()
                        if current_ctx_route and current_ctx_route != "__unmatched__":
                            norm_route = current_ctx_route
                        elif preresolved_route and preresolved_route != "__unmatched__":
                            norm_route = preresolved_route
                    route_for_metrics = norm_route
                    final_span_name = (
                        f"{method} {status_code}"
                        if (not norm_route or norm_route == "__unmatched__")
                        else f"{method} {norm_route}"
                    )
                    final_route = str(status_code) if (not norm_route or norm_route == "__unmatched__") else norm_route
                    attempt(span.update_name, final_span_name, _label="update_span_name")
                    view_name = attempt(_resolve_view_name, request, method, default="view", _label="view_name")

                    safe_set_attribute(span, "http.route", sanitized or target_url or url_path)
                    safe_set_attribute(span, "http.route_template", final_route)
                    if norm_route and norm_route != "__unmatched__":
                        safe_set_attribute(span, "django.route", norm_route)
                    safe_set_attribute(span, "http.response.status_code", status_code)
                    safe_set_attribute(span, "http.status_code", status_code)
                    safe_set_attribute(span, "django.view", str(view_name))
                    safe_set_attribute(span, "django.view.name", str(view_name))
                    safe_set_attribute(span, "resource.name", final_span_name)
                    safe_set_attribute(span, "normalized.service", "django")
                    safe_set_attribute(span, "normalized.operation", final_span_name)

                    def _set_phrase() -> None:
                        import http

                        phrase = http.HTTPStatus(status_code).phrase
                        safe_set_attribute(span, "http.status_text", phrase)
                        safe_set_attribute(span, "http.response.status_text", phrase)

                    attempt(_set_phrase, _label="status_text")

                    def _set_user() -> None:
                        user = getattr(request, "user", None)
                        if user is None:
                            return
                        try:
                            from django.utils.functional import SimpleLazyObject

                            if isinstance(user, SimpleLazyObject):
                                if hasattr(request, "_cached_user"):
                                    user = request._cached_user
                                else:
                                    # User was never evaluated during request; do not force DB query
                                    return
                        except Exception:
                            pass

                        is_auth = False
                        try:
                            is_auth_attr = getattr(user, "is_authenticated", False)
                            is_auth = is_auth_attr() if callable(is_auth_attr) else bool(is_auth_attr)
                        except Exception:
                            pass

                        if is_auth:
                            user_id = str(getattr(user, "pk", getattr(user, "id", "")))
                            if user_id:
                                safe_set_attribute(span, "usr.id", user_id)
                                safe_set_attribute(span, "user.id", user_id)
                                safe_set_attribute(span, "enduser.id", user_id)
                            safe_set_attribute(span, "user.is_authenticated", True)

                    attempt(_set_user, _label="user_attrs")
                    _apply_request_callback(span, request, response)

                    if status_code >= 500:
                        error = True
                        safe_set_attribute(span, "error", True)
                        safe_set_attribute(span, "error.type", str(status_code))
                        safe_set_status(span, StatusCode.ERROR, description=f"HTTP {status_code}")
                    else:
                        safe_set_attribute(span, "error", False)
                        safe_set_status(span, StatusCode.OK)

                    if span_ctx is not None and (hasattr(response, "headers") or hasattr(response, "__setitem__")):

                        def _set_headers() -> None:
                            response["X-Trace-ID"] = trace_id_hex
                            response["X-Span-ID"] = span_id_hex
                            # W3C trace context so downstream consumers can correlate
                            # the response back to this trace without knowing our
                            # custom headers.
                            trace_flags = f"{span_ctx.trace_flags:02x}"
                            response["traceparent"] = f"00-{trace_id_hex}-{span_id_hex}-{trace_flags}"

                        attempt(_set_headers, _label="trace_headers")

                    return response
                finally:
                    duration_ms = (time.monotonic() - start) * 1000.0
                    safe_set_attribute(span, "http.request.duration_ms", duration_ms)
                    try:
                        from tp_trace.metrics import record_request

                        record_request(
                            method=method,
                            route=final_route,
                            status_code=status_code,
                            error=error,
                            duration_ms=duration_ms,
                            service="django",
                            operation=final_span_name,
                            extra_attributes=get_current_tags(),
                        )
                        metric_recorded = True
                    except Exception as metric_exc:
                        logger.debug("Failed to record request metrics: %s", metric_exc)
        finally:
            if not metric_recorded:
                try:
                    from tp_trace.metrics import record_request

                    duration_ms = (time.monotonic() - start) * 1000.0
                    record_request(
                        method=method,
                        route=final_route,
                        status_code=status_code,
                        error=True,
                        duration_ms=duration_ms,
                        service="django",
                        operation=final_span_name,
                        extra_attributes=get_current_tags(),
                    )
                except Exception as metric_exc:
                    logger.debug("Failed to record fallback request metrics: %s", metric_exc)
            reset_request_view(view_token)
            reset_request_route(route_token)
            reset_request_tags()
