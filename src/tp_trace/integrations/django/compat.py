"""Compatibility helpers for the Django integration.

Tolerant shims for Django behaviours that differ across versions, kept here
so the rest of the integration can read as a single contract rather than
scattered try/except tolerance.
"""

from typing import Any

_AUTH_UNSET = object()


def user_is_authenticated(user: Any) -> bool:
    """Return whether ``user`` is authenticated, tolerating lazy users.

    ``User.is_authenticated`` is a property in Django >= 1.10 (a method in
    older versions), and reading it from a ``SimpleLazyObject`` can raise
    before the wrapped user resolves (Django ticket #26988). A raising access
    must not drop user attribution for the whole request, so failures degrade
    to "anonymous" and unwrap one lazy layer before giving up.
    """
    seen = set()
    current = user
    while current is not None and id(current) not in seen:
        seen.add(id(current))

        try:
            value = current.is_authenticated
        except Exception:
            value = _AUTH_UNSET

        if value is not _AUTH_UNSET:
            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = _AUTH_UNSET
            if value is not _AUTH_UNSET:
                try:
                    if value == True:  # noqa: E712 - tolerate lazy truthy wrappers
                        return True
                except Exception:
                    pass

        # ``SimpleLazyObject`` exposes the real object under ``_wrapped``; retry
        # once there. The ``seen`` set guards against a self-referential chain.
        current = getattr(current, "_wrapped", None)

    return False
