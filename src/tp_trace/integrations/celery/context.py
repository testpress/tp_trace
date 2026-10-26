"""Celery creation context and bounded operation utilities.

This module is dependency-free: it does not import celery or opentelemetry,
ensuring safe imports even when optional dependencies are absent.
"""

import contextvars
import threading
from typing import FrozenSet, Iterable, NamedTuple, Optional


class CeleryCreationMeta(NamedTuple):
    action: str  # "apply_async" or "run"
    bounded_operation: str  # "celery.apply_async.<admitted_name or unknown>"
    span_name: str  # "🟣 <task_name>.apply_async" or "🟣 <task_name>.run" (fallback generic)


_CURRENT_CELERY_CREATION_META: contextvars.ContextVar[Optional[CeleryCreationMeta]] = contextvars.ContextVar(
    "tp_trace.celery_creation_meta", default=None
)

_ADMITTED_TASK_NAMES: FrozenSet[str] = frozenset()
_ADMISSION_LOCK = threading.Lock()


def set_celery_creation_meta(meta: Optional[CeleryCreationMeta]) -> Optional[contextvars.Token]:
    """Publish Celery creation metadata for RouteEnrichingSpanProcessor.on_start."""
    try:
        return _CURRENT_CELERY_CREATION_META.set(meta)
    except Exception:
        return None


def get_celery_creation_meta() -> Optional[CeleryCreationMeta]:
    """Retrieve in-flight Celery creation metadata, if any."""
    try:
        return _CURRENT_CELERY_CREATION_META.get()
    except LookupError:
        return None


def reset_celery_creation_meta(token: Optional[contextvars.Token]) -> None:
    """Reset the Celery creation metadata context variable using the provided token."""
    if token is not None:
        try:
            _CURRENT_CELERY_CREATION_META.reset(token)
            return
        except Exception:
            pass
    _CURRENT_CELERY_CREATION_META.set(None)


def set_admitted_task_names(names: Iterable[str], extend: bool = False) -> None:
    """Admit up to 256 names, without evicting names when another app finalizes.

    Task names are sorted within each finalized registry and limited to 200
    characters. Extending never replaces an admitted name, keeping the total
    metric operation cardinality bounded for the entire activation lifecycle.
    """
    global _ADMITTED_TASK_NAMES
    with _ADMISSION_LOCK:
        try:
            candidates = sorted({n for n in names if isinstance(n, str) and 0 < len(n) <= 200})
            if extend:
                remaining = 256 - len(_ADMITTED_TASK_NAMES)
                additions = [n for n in candidates if n not in _ADMITTED_TASK_NAMES][:remaining]
                _ADMITTED_TASK_NAMES = _ADMITTED_TASK_NAMES.union(additions)
            else:
                _ADMITTED_TASK_NAMES = frozenset(candidates[:256])
        except Exception:
            if not extend:
                _ADMITTED_TASK_NAMES = frozenset()


def clear_admitted_task_names() -> None:
    """Clear the admitted task snapshot on uninstrument."""
    global _ADMITTED_TASK_NAMES
    with _ADMISSION_LOCK:
        _ADMITTED_TASK_NAMES = frozenset()


def get_admitted_task_names() -> FrozenSet[str]:
    """Return the currently admitted set of task names."""
    return _ADMITTED_TASK_NAMES


def get_bounded_task_label(task_name: Optional[str]) -> str:
    """Return the admitted task name or the fallback label 'unknown'."""
    if task_name and isinstance(task_name, str) and task_name in _ADMITTED_TASK_NAMES:
        return task_name
    return "unknown"


def compute_bounded_operation(action: str, task_name: Optional[str], registered: bool = False) -> str:
    """Normalize a name, admitting late-registered tasks only within the bound.

    Callers set registered only after resolving an actual Celery task object;
    arbitrary sender strings must not populate the metric name set.
    """
    if (
        registered
        and isinstance(task_name, str)
        and 0 < len(task_name) <= 200
        and task_name not in _ADMITTED_TASK_NAMES
        and len(_ADMITTED_TASK_NAMES) < 256
    ):
        set_admitted_task_names([task_name], extend=True)
    label = get_bounded_task_label(task_name)
    return f"celery.{action}.{label}"
