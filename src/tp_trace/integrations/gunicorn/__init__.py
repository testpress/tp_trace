"""Gunicorn integration for tp_trace."""

from tp_trace.integrations.gunicorn.integration import GunicornIntegration

__all__ = [
    "GunicornIntegration",
    "TPTraceGunicornLogger",
]


def __getattr__(name: str):
    # Lazy import avoids requiring gunicorn at package import time.
    if name == "TPTraceGunicornLogger":
        from tp_trace.integrations.gunicorn.logger import TPTraceGunicornLogger

        return TPTraceGunicornLogger
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
