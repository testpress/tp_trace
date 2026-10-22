"""Auth spans — wraps django.contrib.auth login/authenticate operations."""

import logging
from typing import Any, Callable

from opentelemetry.trace import SpanKind

from tp_trace.safety import attempt, safe_set_attribute
from tp_trace.tracing import traced_span

logger = logging.getLogger("tp_trace.integrations.django.auth")


def traced_login(wrapped: Callable, instance: Any, args: Any, kwargs: Any) -> Any:
    # Pre-application telemetry: user extraction must never prevent login.
    user = args[1] if len(args) > 1 else kwargs.get("user")
    user_id = attempt(
        lambda: str(getattr(user, "pk", getattr(user, "id", ""))) if user else "",
        default="",
        _label="auth.user_id",
    )

    span_attrs = {"django.auth.action": "login"}
    if user_id:
        span_attrs["usr.id"] = user_id
        span_attrs["enduser.id"] = user_id

    with traced_span("django.auth.login", kind=SpanKind.INTERNAL, attributes=span_attrs, tracer_name="tp_trace.django"):
        return wrapped(*args, **kwargs)


def traced_authenticate(wrapped: Callable, instance: Any, args: Any, kwargs: Any) -> Any:
    with traced_span(
        "django.auth.authenticate",
        kind=SpanKind.INTERNAL,
        attributes={"django.auth.action": "authenticate"},
        tracer_name="tp_trace.django",
    ) as span:
        res = wrapped(*args, **kwargs)
        if res:
            user_id = attempt(
                lambda: str(getattr(res, "pk", getattr(res, "id", ""))),
                default="",
                _label="auth.user_id",
            )
            if user_id:
                safe_set_attribute(span, "usr.id", user_id)
                safe_set_attribute(span, "enduser.id", user_id)
                safe_set_attribute(span, "auth.success", True)
        else:
            safe_set_attribute(span, "auth.success", False)
        return res
