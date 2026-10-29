"""TpTrace filtering propagator respecting extract_trace_context configuration."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional, Sequence
from opentelemetry.context import Context
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import (
    Getter,
    TextMapPropagator,
    default_getter,
)

from tp_trace.config_access import get_config as _get_config
from tp_trace.safety import attempt


_django_extraction_decision: ContextVar[Optional[bool]] = ContextVar("tp_trace.django_extract", default=None)


def get_django_extraction_decision() -> Optional[bool]:
    return _django_extraction_decision.get()


@contextmanager
def django_extraction_context(request: Any) -> Iterator[None]:
    """Evaluate the Django policy once with the real request, before OTel extracts."""
    cfg = attempt(_get_config, default=None, _label="propagator_config")
    setting = getattr(cfg, "extract_trace_context", True) if cfg else True
    decision = attempt(lambda: bool(setting(request) if callable(setting) else setting), default=True)
    token = _django_extraction_decision.set(decision)
    try:
        yield
    finally:
        _django_extraction_decision.reset(token)


class TpTraceFilteringPropagator(CompositePropagator):
    """CompositePropagator that honors SDKConfig.extract_trace_context."""

    def __init__(self, propagators: Sequence[TextMapPropagator]) -> None:
        super().__init__(propagators)

    def extract(
        self,
        carrier: Any,
        context: Optional[Context] = None,
        getter: Getter[Any] = default_getter,
    ) -> Context:
        if context is None:
            context = Context()

        should_extract = get_django_extraction_decision()
        if should_extract is None:
            # Request predicates and HTTP extraction policies only apply inside
            # the Django request boundary. Non-HTTP propagation (e.g. Celery workers,
            # internal asynchronous messaging) must always extract trace context.
            should_extract = True

        if not should_extract:
            return context

        return super().extract(carrier, context=context, getter=getter)
