"""View span — wraps BaseHandler._get_response, View.dispatch, and resolves view name/route."""

import functools
import re
import weakref
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from opentelemetry.trace import SpanKind, get_current_span

from tp_trace.route_context import reset_request_route, reset_request_view, set_request_route, set_request_view
from tp_trace.safety import UNTRACED, attempt, safe_enrich, safe_set_attribute, safe_update_name
from tp_trace.tracing import traced_span
from tp_trace.config_access import get_config as _get_config, set_config as set_config

from .request import _format_view_from_match, _normalize_route, _resolve_view_name

_in_view_dispatch: ContextVar[bool] = ContextVar("in_view_dispatch", default=False)


def _apply_tags(span):
    """Apply configured tags. Self-contained: never raises, never blocks.

    Callers run this *before* the application call, so the config lookup is
    guarded here rather than at each call site.
    """
    cfg = attempt(_get_config, default=None, _label="view_config")
    if cfg and cfg.tags:
        safe_enrich(span, cfg.tags)


_RESPONSE_PARENT_SPANS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _bind_response_parent(response: Any) -> None:
    if response is not None and hasattr(response, "render") and not getattr(response, "is_rendered", False):
        curr = get_current_span()
        if curr and curr.is_recording():
            try:
                _RESPONSE_PARENT_SPANS[response] = curr
            except Exception:
                pass


@functools.lru_cache(maxsize=512)
def _to_snake_case(name: str) -> str:
    """Convert CamelCase view class name to snake_case (memoized for high throughput)."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def traced_get_response(wrapped: Callable, instance: Any, args: Any, kwargs: Any) -> Any:
    request = args[0] if args else kwargs.get("request")
    if request is None:
        return wrapped(*args, **kwargs)

    initial_route = attempt(getattr, request, "path", default="/", _label="request.path")

    with traced_span(
        "django.view",
        kind=SpanKind.INTERNAL,
        attributes={"span.type": "web", "component": "django", "django.view": "view", "http.route": initial_route},
        tracer_name="tp_trace.django",
    ) as span:
        _apply_tags(span)
        method = attempt(getattr, request, "method", default="GET", _label="request.method")
        pre_route = attempt(_normalize_route, request, initial_route, default=initial_route, _label="normalize_route")
        route_token = None
        if pre_route and pre_route != "__unmatched__":
            route_token = set_request_route(pre_route, method)
        match = getattr(request, "resolver_match", None)
        resolved_view = attempt(_format_view_from_match, match, method, default=None, _label="view_format") if match else None
        view_token = set_request_view(resolved_view) if resolved_view else None

        try:
            response = wrapped(*args, **kwargs)
        except Exception:
            norm_route = attempt(
                _normalize_route, request, initial_route, default=initial_route, _label="normalize_route"
            )
            if norm_route and norm_route != "__unmatched__":
                safe_set_attribute(span, "http.route", norm_route)
            raise
        finally:
            if view_token is not None:
                reset_request_view(view_token)
            if route_token is not None:
                reset_request_route(route_token)
        # Post-application enrichment must never alter the result. Each step
        # degrades independently so a failure here cannot affect what the
        # application returned.
        raw_view_name = attempt(_resolve_view_name, request, method, default="view", _label="view_name")
        norm_route = attempt(_normalize_route, request, initial_route, default=initial_route, _label="normalize_route")

        safe_update_name(span, f"django.view.{raw_view_name}")
        safe_set_attribute(span, "django.view", raw_view_name)
        safe_set_attribute(span, "django.view.name", raw_view_name)
        safe_set_attribute(span, "resource.name", raw_view_name)
        safe_set_attribute(span, "http.route", norm_route)
        safe_set_attribute(span, "http.request.method", method.upper())
        attempt(_bind_response_parent, response, _label="bind_response_parent")
        return response


def traced_view_setup(wrapped: Callable, instance: Any, args: Any, kwargs: Any) -> Any:
    span_name = "django.views.generic.base.View.setup"
    attrs = {
        "span.type": "web",
        "component": "django",
        "django.view.method": "setup",
        "resource.name": span_name,
    }
    with traced_span(span_name, kind=SpanKind.INTERNAL, attributes=attrs, tracer_name="tp_trace.django"):
        res = wrapped(*args, **kwargs)
        return res


def _resolve_dispatch_owner(wrapped: Callable, fallback: str) -> str:
    """Best-effort owner of a dispatch method (e.g. rest_framework.views.APIView)."""
    qualname = getattr(wrapped, "__qualname__", "")
    if not qualname or "." not in qualname:
        return fallback
    owner_name = qualname.rsplit(".", 1)[0]
    owner_mod = getattr(wrapped, "__module__", "")
    if owner_mod and owner_mod != "builtins":
        return f"{owner_mod}.{owner_name}"
    return fallback


@dataclass(frozen=True)
class _DispatchPlan:
    """Everything ``traced_view_dispatch`` needs, decided before the app runs.

    Handler fields are ``None`` when there is no instrumentable handler, so
    callers never have to probe for missing keys.
    """

    span_name: str
    attrs: Dict[str, Any]
    action: Optional[str]
    handler: Optional[Callable]
    handler_method: Optional[str] = None
    method: Optional[str] = None
    handler_span_name: Optional[str] = None
    handler_attrs: Optional[Dict[str, Any]] = None


def _build_dispatch_plan(instance: Any, wrapped: Callable, args: Any, kwargs: Any) -> _DispatchPlan:
    """Prepare everything ``traced_view_dispatch`` needs before the app runs.

    Introspection of a Django view is not guaranteed to be safe: views may use
    properties, descriptors, or dynamic ``__getattribute__``. Building the plan
    in one function means a single guard covers the whole phase, instead of
    wrapping each lookup and risking a half-applied plan that blocks dispatch.
    """
    view_cls = attempt(getattr, instance.__class__, "__name__", default="View", _label="view_cls")
    view_module = attempt(getattr, instance.__class__, "__module__", default="", _label="view_module")
    full_view_name = attempt(
        lambda: f"{view_module}.{view_cls}" if view_module else view_cls,
        default=view_cls,
        _label="full_view_name",
    )

    request = args[0] if args else kwargs.get("request")
    method = attempt(getattr, request, "method", default="GET", _label="request.method")
    method = method.lower() if isinstance(method, str) else "get"

    dispatch_cls = attempt(
        _resolve_dispatch_owner, wrapped, full_view_name, default=full_view_name, _label="dispatch_cls"
    )
    action = attempt(getattr, instance, "action", default=None, _label="view.action")

    dispatch_span_name = f"{dispatch_cls}.dispatch"
    dispatch_attrs = {
        "span.type": "web",
        "component": "django",
        "django.view.class": full_view_name,
        "django.view.name": view_cls,
        "django.view.method": "dispatch",
        "resource.name": dispatch_span_name,
    }

    handler_method = action if action and hasattr(instance, action) else method
    handler = attempt(getattr, instance, handler_method, default=None, _label="view.handler")
    if not (handler and callable(handler) and not getattr(handler, "_tp_traced", False)):
        return _DispatchPlan(span_name=dispatch_span_name, attrs=dispatch_attrs, action=action, handler=None)

    snake_cls = attempt(_to_snake_case, view_cls, default=view_cls, _label="view.snake")
    handler_span_name = (
        f"{view_module}.{snake_cls}.{handler_method}" if view_module else f"{snake_cls}.{handler_method}"
    )
    handler_attrs = {
        "span.type": "web",
        "component": "django",
        "django.view.class": full_view_name,
        "django.view.name": view_cls,
        "django.view.method": handler_method,
        "resource.name": handler_span_name,
    }
    if action:
        handler_attrs["django.view.action"] = str(action)

    return _DispatchPlan(
        span_name=dispatch_span_name,
        attrs=dispatch_attrs,
        action=action,
        handler=handler,
        handler_method=handler_method,
        method=method,
        handler_span_name=handler_span_name,
        handler_attrs=handler_attrs,
    )


def traced_view_dispatch(wrapped: Callable, instance: Any, args: Any, kwargs: Any) -> Any:
    if _in_view_dispatch.get():
        return wrapped(*args, **kwargs)

    token = _in_view_dispatch.set(True)
    try:
        # Planning is a single guarded phase: a failure anywhere in it falls back to
        # a plain dispatch rather than partially instrumenting the view.
        plan = attempt(_build_dispatch_plan, instance, wrapped, args, kwargs, default=UNTRACED, _label="dispatch_plan")
        if plan is UNTRACED:
            return wrapped(*args, **kwargs)

        view_cls_name = plan.attrs.get("django.view.class") or "View"
        exact_view = f"{view_cls_name}.{plan.handler_method}" if plan.handler_method else view_cls_name
        view_token = set_request_view(exact_view)
        try:
            with traced_span(
                plan.span_name, kind=SpanKind.INTERNAL, attributes=plan.attrs, tracer_name="tp_trace.django"
            ) as dispatch_span:
                _apply_tags(dispatch_span)
                if plan.action:
                    safe_set_attribute(dispatch_span, "django.view.action", str(plan.action))

                request = args[0] if args else kwargs.get("request")
                route_token = None
                if request:
                    norm_route = attempt(
                        _normalize_route,
                        request,
                        getattr(request, "path", "/"),
                        default="__unmatched__",
                        _label="normalize_route",
                    )
                    if norm_route and norm_route != "__unmatched__":
                        req_method = attempt(getattr, request, "method", default="GET", _label="request.method")
                        route_token = set_request_route(norm_route, req_method)
                        safe_set_attribute(dispatch_span, "http.route", norm_route)

                handler = plan.handler
                if handler is None:
                    res = wrapped(*args, **kwargs)
                    attempt(_bind_response_parent, res, _label="bind_response_parent")
                    return res

                handler_span_name = plan.handler_span_name
                handler_attrs = plan.handler_attrs
                handler_method = plan.handler_method
                method = plan.method

                def _traced_handler(*h_args, **h_kwargs):
                    with traced_span(
                        handler_span_name, kind=SpanKind.INTERNAL, attributes=handler_attrs, tracer_name="tp_trace.django"
                    ):
                        h_res = handler(*h_args, **h_kwargs)
                        attempt(_bind_response_parent, h_res, _label="bind_response_parent")
                        return h_res

                setattr(_traced_handler, "_tp_traced", True)
                orig_action_handler = attempt(getattr, instance, handler_method, default=None, _label="orig_action_handler")
                orig_method_handler = (
                    attempt(getattr, instance, method, default=None, _label="orig_method_handler")
                    if method != handler_method
                    else None
                )
                inst_dict = getattr(instance, "__dict__", None)
                had_action_in_dict = inst_dict is not None and handler_method in inst_dict
                had_method_in_dict = inst_dict is not None and method in inst_dict

                attempt(setattr, instance, handler_method, _traced_handler, _label="bind_handler")
                if orig_method_handler is not None:
                    attempt(setattr, instance, method, _traced_handler, _label="bind_method")
                try:
                    res = wrapped(*args, **kwargs)
                    attempt(_bind_response_parent, res, _label="bind_response_parent")
                    return res
                finally:
                    if had_action_in_dict:
                        if orig_action_handler is not None:
                            attempt(setattr, instance, handler_method, orig_action_handler, _label="restore_handler")
                    else:
                        attempt(delattr, instance, handler_method, _label="del_handler")

                    if orig_method_handler is not None:
                        if had_method_in_dict:
                            attempt(setattr, instance, method, orig_method_handler, _label="restore_method")
                        else:
                            attempt(delattr, instance, method, _label="del_method")
        finally:
            if route_token is not None:
                reset_request_route(route_token)
            reset_request_view(view_token)
    finally:
        _in_view_dispatch.reset(token)
