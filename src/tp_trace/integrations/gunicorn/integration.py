"""GunicornIntegration — instruments Gunicorn worker state and native StatsD metrics."""

import importlib
import logging
from typing import Any, Callable, Tuple

from tp_trace.integrations.base import BaseIntegration
from tp_trace.integrations.gunicorn.metrics import get_gunicorn_collector

logger = logging.getLogger("tp_trace.integrations.gunicorn")


class GunicornIntegration(BaseIntegration):
    """Instruments Gunicorn worker busy/idle state and lifecycle events."""

    name = "gunicorn"

    def is_installed(self) -> bool:
        try:
            importlib.import_module("gunicorn")
            return True
        except ImportError:
            return False

    def _apply_patch(self) -> None:
        collector = get_gunicorn_collector()
        collector.set_config(self._config)

        # Wrap worker request handling so preload_app=True setups still track
        # busy/idle state even when gunicorn.conf.py hooks are not wired.
        self.wrap(
            "gunicorn.workers.sync.SyncWorker",
            "handle_request",
            self._wrap_handle_request,
        )
        self.wrap(
            "gunicorn.workers.gthread.ThreadWorker",
            "handle_request",
            self._wrap_handle_request,
        )
        self.wrap(
            "gunicorn.workers.base_async.AsyncWorker",
            "handle_request",
            self._wrap_handle_request,
        )

    @staticmethod
    def _wrap_handle_request(
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: dict,
    ) -> Any:
        collector = get_gunicorn_collector()
        collector.on_request_started()
        try:
            return wrapped(*args, **kwargs)
        finally:
            collector.on_request_finished()
