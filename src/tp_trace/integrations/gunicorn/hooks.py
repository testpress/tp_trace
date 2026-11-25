"""Public Gunicorn config hook callbacks for tp_trace."""

from typing import Any

from tp_trace.integrations.gunicorn.metrics import get_gunicorn_collector


def post_fork(server: Any, worker: Any) -> None:
    """Reset worker in-flight state after fork."""
    get_gunicorn_collector().reset_worker()


def pre_request(worker: Any, req: Any) -> None:
    """Mark worker busy for an incoming request."""
    get_gunicorn_collector().on_request_started()


def post_request(worker: Any, req: Any, environ: Any, resp: Any) -> None:
    """Decrement worker in-flight request count after request completion."""
    get_gunicorn_collector().on_request_finished()
