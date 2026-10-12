"""Instrumentation transparency boundary.

tp_trace's core safety invariant, in one place:

    Instrumentation must never
      1. prevent application execution,
      2. execute application code more than once, or
      3. alter the application's result or exception.

Concretely, for a wrapper around ``wrapped(*args, **kwargs)``:

    Before the call   telemetry failure -> log and run the application anyway
    During the call   application failure -> propagate unchanged
    After the call    telemetry failure -> log and return the result unchanged

The previous contract ("telemetry must never break the host application") was
not enforceable: wrappers re-ran the application call when a telemetry step
raised, which executed application code twice. Removing that retry without
guarding the telemetry prologue just moved the failure -- a bug in span
construction would then *prevent* the application call from running at all.

Neither behaviour is acceptable, so the guarantee is enforced here instead of
in a catch-all around the wrapper:

* :func:`attempt` runs a telemetry computation and degrades to a fallback
  rather than raising, so the caller can decide to run untraced.
* :func:`safe_set_attribute` and friends keep span *enrichment* from
  becoming an application failure.
* :class:`UNTRACED` is the sentinel returned when pre-application telemetry
  could not be built.
"""

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("tp_trace.safety")


class _Untraced:
    """Sentinel type signalling that telemetry could not be prepared."""

    _instance: Optional["_Untraced"] = None

    def __new__(cls) -> "_Untraced":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNTRACED"

    def __bool__(self) -> bool:
        return False


#: Returned by :func:`attempt` when the telemetry step failed. Callers should
#: run the application untraced rather than propagate.
UNTRACED = _Untraced()


def attempt(
    fn: Callable[..., Any],
    *args: Any,
    default: Any = None,
    _label: str = "",
    **kwargs: Any,
) -> Any:
    """Run a telemetry computation, degrading to ``default`` instead of raising.

    Used for every step that must run *before* the application call. A failure
    here must never stop the application from executing.

    Args:
        fn: The telemetry computation.
        default: Returned if ``fn`` raises. Pass :data:`UNTRACED` to signal
            "telemetry unavailable, run the application untraced".
        _label: Optional name for the debug log; defaults to ``fn.__name__``.
    """
    if fn is getattr and len(args) == 2 and default is not UNTRACED:
        args = (args[0], args[1], default)
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.debug(
            "tp_trace: telemetry step %s failed (%s: %s); degrading",
            _label or getattr(fn, "__name__", repr(fn)),
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return default


def safe_set_attribute(span: Any, key: str, value: Any) -> None:
    """Set a span attribute; never raise."""
    attempt(span.set_attribute, key, value, _label=f"set_attribute({key})")


def safe_set_status(span: Any, status: Any, description: Optional[str] = None) -> None:
    """Set a span status; never raise."""
    attempt(span.set_status, status, description, _label="set_status")


def safe_record_exception(span: Any, exc: BaseException) -> None:
    """Record an exception on a span; never raise."""
    attempt(span.record_exception, exc, _label="record_exception")


def find_exception(exc: BaseException) -> BaseException:
    """Return the most specific exception inside a ``(Base)ExceptionGroup``.

    Python 3.11+ groups (``ExceptionGroup``/``BaseExceptionGroup``) wrap one or
    more children and are themselves ``BaseException`` subclasses. Recording the
    group would hide the real failure from error capture and span status, so
    walk into it and return the first leaf. A self-referential group is returned
    unchanged rather than looping forever.
    """
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        children = getattr(current, "exceptions", None)
        if not children:
            return current
        leaf = next((child for child in children if getattr(child, "exceptions", None) is None), None)
        current = leaf if leaf is not None else children[0]
    return exc


def safe_update_name(span: Any, name: str) -> None:
    """Rename a span; never raise."""
    attempt(span.update_name, name, _label="update_name")


def safe_enrich(span: Any, attributes: Any) -> None:
    """Apply attributes to a span without letting telemetry failures escape.

    Not transactional: attributes applied before a failure remain applied. A
    partially-enriched span is preferable to a lost trace, so this deliberately
    does not roll back.
    """
    if not attributes:
        return
    attempt(
        lambda: [span.set_attribute(k, v) for k, v in dict(attributes).items()],
        _label="enrich",
    )


__all__ = [
    "UNTRACED",
    "attempt",
    "find_exception",
    "safe_enrich",
    "safe_record_exception",
    "safe_set_attribute",
    "safe_set_status",
    "safe_update_name",
]
