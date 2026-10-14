"""Template span — wraps django.template.base.Template.render and TemplateResponse.render."""
import fnmatch
from contextvars import ContextVar
from typing import Any, Callable, FrozenSet, List, Optional, Tuple

from opentelemetry.trace import SpanKind, set_span_in_context

from tp_trace.config import DEFAULT_EXCLUDE_PATTERNS, SDKConfig
from tp_trace.safety import UNTRACED, attempt
from tp_trace.config_access import get_config as _get_config, set_config
from tp_trace.tracing import traced_span
import tp_trace

_in_template_span: ContextVar[bool] = ContextVar("in_template_span", default=False)
_active_rendering_templates: ContextVar[FrozenSet[int]] = ContextVar(
    "active_rendering_templates", default=frozenset()
)


def _is_excluded(template_name: str, patterns: Optional[List[str]]) -> bool:
    if not patterns:
        patterns = DEFAULT_EXCLUDE_PATTERNS
    for pat in patterns:
        if fnmatch.fnmatch(template_name, pat) or fnmatch.fnmatch(template_name, pat.rstrip("*")):
            return True
    return False


def _extract_template_name(instance: Any) -> str:
    if instance is None:
        return ""

    # Unwrap backend template wrapper (e.g. django.template.backends.django.Template) if present
    if hasattr(instance, "template") and not isinstance(instance, str):
        instance = getattr(instance, "template")

    # 1. Check origin.template_name (e.g. 'courses/course_form.html')
    origin = getattr(instance, "origin", None)
    if origin is not None:
        template_name = getattr(origin, "template_name", None)
        if template_name:
            return str(template_name)

    # 2. Check instance.name if valid and not '<string>'
    template_name = getattr(instance, "name", None)
    if template_name and str(template_name) != "<string>":
        if hasattr(template_name, "template_name") and template_name.template_name:
            return str(template_name.template_name)
        if hasattr(template_name, "name"):
            return str(template_name.name)
        return str(template_name)

    # 3. Fallback to origin.name (e.g. full template file path)
    if origin is not None:
        origin_name = getattr(origin, "name", None)
        if origin_name and str(origin_name) != "<string>":
            return str(origin_name)

    # 4. Fallback to instance.name if '<string>'
    if template_name:
        return str(template_name)

    return ""


def traced_template_render(wrapped: Callable, instance: Any, args: Any, kwargs: Any) -> Any:
    # All of the preparation below runs BEFORE the application call, so a
    # failure anywhere in it must degrade to an untraced render rather than
    # prevent the template from being rendered at all. ``None`` (nothing to
    # trace) and UNTRACED (telemetry fault) both render untraced.
    plan = attempt(_plan_template_render, instance, default=UNTRACED, _label="plan_template_render")
    if plan is UNTRACED or plan is None:
        return wrapped(*args, **kwargs)

    span_name, span_attrs, new_active = plan

    token_active = _active_rendering_templates.set(new_active)
    token_span = _in_template_span.set(True)
    tokens = (token_active, token_span)
    try:
        with traced_span(span_name, kind=SpanKind.INTERNAL, attributes=span_attrs, tracer_name="tp_trace.django"):
            return wrapped(*args, **kwargs)
    finally:
        attempt(_clear_template_tokens, tokens, _label="clear_render_tokens")


def _plan_template_render(instance: Any):
    """Build the pre-application telemetry plan for a template render.

    Returns ``None`` when the template should be rendered untraced, otherwise
    ``(span_name, span_attrs, new_active)``. Raises on genuine telemetry faults;
    :func:`traced_template_render` converts those into an untraced render.
    """
    template_str = _extract_template_name(instance)
    if not template_str:
        return None

    cfg = _get_config()

    # 1. Check if template instrumentation is enabled
    if cfg and not getattr(cfg, "template_enabled", True):
        return None

    # 2. Check exclude patterns (e.g. django/forms/*, debug_toolbar/*, */widgets/*)
    exclude_patterns = getattr(cfg, "template_exclude", DEFAULT_EXCLUDE_PATTERNS) if cfg else DEFAULT_EXCLUDE_PATTERNS
    if _is_excluded(template_str, exclude_patterns):
        return None

    # 3. If rendering inside a parent template span, check if nested template tracing is enabled
    if _in_template_span.get():
        trace_nested = getattr(cfg, "trace_nested_templates", True) if cfg else True
        if not trace_nested:
            return None

    active = _active_rendering_templates.get()
    inst_id = id(instance)
    inner_tmpl = getattr(instance, "template", None)
    inner_id = id(inner_tmpl) if (inner_tmpl is not None and inner_tmpl is not instance) else None

    if inst_id in active or (inner_id is not None and inner_id in active):
        return None

    engine = getattr(instance, "engine", None)
    span_name = f"django.template: {template_str}"
    span_attrs = {
        "span.type": "template",
        "component": "django",
        "django.template.name": template_str,
    }
    if engine:
        span_attrs["django.template.engine.class"] = engine.__class__.__name__
    if cfg and cfg.tags:
        span_attrs.update(cfg.tags)

    new_active = active | {inst_id}
    if inner_id is not None:
        new_active = new_active | {inner_id}
    return span_name, span_attrs, new_active


def _clear_template_tokens(tokens: Any) -> None:
    """Reset template rendering context variables safely."""
    if not tokens or not isinstance(tokens, tuple) or len(tokens) != 2:
        return
    token_active, token_span = tokens
    try:
        _active_rendering_templates.reset(token_active)
    except Exception:
        pass
    try:
        _in_template_span.reset(token_span)
    except Exception:
        pass


def _extract_response_template_name(instance: Any) -> str:
    template_name = getattr(instance, "template_name", None)
    if isinstance(template_name, (list, tuple)):
        valid = [str(t) for t in template_name if t]
        return ", ".join(valid) if valid else ""
    elif template_name:
        return str(template_name)
    return ""


def traced_template_response_render(wrapped: Callable, instance: Any, args: Any, kwargs: Any) -> Any:
    cfg = _get_config()
    if cfg and not getattr(cfg, "template_enabled", True):
        return wrapped(*args, **kwargs)

    template_str = _extract_response_template_name(instance)
    span_name = "django.template.response.TemplateResponse.render"
    span_attrs = {
        "django.response.class": instance.__class__.__name__,
    }
    if template_str:
        span_attrs["django.template.name"] = template_str

    if cfg and cfg.tags:
        span_attrs.update(cfg.tags)

    parent_span = getattr(instance, "_tp_parent_span", None)
    parent_ctx = set_span_in_context(parent_span) if parent_span else None

    with traced_span(
        span_name,
        kind=SpanKind.INTERNAL,
        attributes=span_attrs,
        tracer_name="tp_trace.django",
        context=parent_ctx,
    ):
        return wrapped(*args, **kwargs)
