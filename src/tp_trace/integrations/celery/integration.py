"""CeleryIntegration for tp_trace."""

import importlib
import logging
import weakref
from typing import Any, Optional

from tp_trace.config import SDKConfig
from tp_trace.config_access import set_config
from tp_trace.integrations.base import BaseIntegration
from tp_trace.integrations.celery.context import (
    clear_admitted_task_names,
    set_admitted_task_names,
)

logger = logging.getLogger("tp_trace.integrations.celery")


class CeleryIntegration(BaseIntegration):
    """Integrates Celery with tp_trace, adding producer and consumer tracing."""

    name = "celery"

    def __init__(self, config: Optional[SDKConfig] = None) -> None:
        super().__init__(config=config)
        self._our_instrumentor: Any = None
        self._finalization_apps: Any = weakref.WeakSet()

    def is_installed(self) -> bool:
        """Check if both celery and opentelemetry-instrumentation-celery are importable."""
        try:
            importlib.import_module("celery")
            importlib.import_module("opentelemetry.instrumentation.celery")
            return True
        except ImportError:
            return False

    def _check_compatibility(self) -> bool:
        """Verify presence of required upstream symbols and hooks."""
        try:
            from celery import _state, signals

            for name in ("connect_on_app_finalize", "_on_app_finalizers", "_apps"):
                if not hasattr(_state, name):
                    logger.warning("tp_trace: Celery lifecycle API '%s' missing, skipping.", name)
                    return False
            from opentelemetry.instrumentation.celery import CeleryInstrumentor, utils

            required_signals = (
                signals.task_prerun,
                signals.task_postrun,
                signals.before_task_publish,
                signals.after_task_publish,
                signals.task_failure,
                signals.task_retry,
            )
            for sig in required_signals:
                if sig is None:
                    logger.warning("tp_trace: Celery signal missing, skipping integration.")
                    return False

            required_utils = (
                "retrieve_task",
                "retrieve_task_id",
                "retrieve_task_from_sender",
                "retrieve_task_id_from_message",
                "retrieve_task_id_from_request",
                "retrieve_context",
                "attach_context",
                "detach_context",
                "retrieve_reason",
                "CTX_KEY",
            )
            for u in required_utils:
                if not hasattr(utils, u):
                    logger.warning("tp_trace: Celery utility '%s' missing, skipping integration.", u)
                    return False

            for name in (
                "_trace_prerun",
                "_trace_postrun",
                "_trace_before_publish",
                "_trace_after_publish",
                "_trace_failure",
                "_trace_retry",
                "create_celery_metrics",
                "_record_histograms",
                "update_task_duration_time",
                "_uninstrument",
            ):
                if not callable(getattr(CeleryInstrumentor, name, None)):
                    logger.warning("tp_trace: CeleryInstrumentor hook '%s' missing, skipping.", name)
                    return False

            return True
        except Exception as exc:
            logger.warning("tp_trace: Celery compatibility check failed: %s", exc)
            return False

    def _build_task_snapshot(self, app: Any = None) -> None:
        """Read existing registries without finalizing apps or importing tasks."""
        try:
            from celery import _state

            apps = [app] if app is not None else list(_state._apps)
            names = set()
            for candidate in apps:
                if candidate.finalized:
                    names.update(candidate._tasks.keys())
            set_admitted_task_names(names, extend=True)
        except Exception:
            logger.debug("tp_trace: Error building Celery task snapshot", exc_info=True)

    def _watch_app_finalization(self, app: Any) -> None:
        """Wait until Celery itself has evaluated pending task decorators."""
        try:
            app.on_after_finalize.connect(self._on_app_finalized, weak=False)
            self._finalization_apps.add(app)
        except Exception:
            logger.debug("tp_trace: Could not observe Celery finalization", exc_info=True)

    def _on_app_finalized(self, sender: Any, **kwargs: Any) -> None:
        self._build_task_snapshot(sender)

    def _remove_patch(self) -> None:
        from celery import _state

        if self._our_instrumentor is not None:
            instrumentor = self._our_instrumentor
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
            else:
                # A signal connection can fail before upstream sets its active
                # flag. The public uninstrument() skips cleanup in that state.
                instrumentor._uninstrument()
            self._our_instrumentor = None
            clear_admitted_task_names()

        _state._on_app_finalizers.discard(self._watch_app_finalization)
        for app in list(self._finalization_apps):
            app.on_after_finalize.disconnect(self._on_app_finalized)
        self._finalization_apps.clear()
        super()._remove_patch()

    def instrument(self) -> bool:
        """Instrument Celery safely with explicit activation and coexistence checks."""
        if self._instrumented:
            return True

        if not self.is_installed():
            logger.debug("Celery or opentelemetry-instrumentation-celery not installed, skipping.")
            return False

        if not self._check_compatibility():
            return False

        # Coexistence guard: if upstream instrumentor is already active, do not overwrite or interfere
        try:
            from opentelemetry.instrumentation.celery import CeleryInstrumentor

            upstream = CeleryInstrumentor()
            if getattr(upstream, "is_instrumented_by_opentelemetry", False):
                logger.debug("Upstream CeleryInstrumentor is already active; skipping tp_trace Celery integration.")
                return False
        except Exception as exc:
            logger.debug("Error checking upstream Celery instrumentor state: %s", exc)

        try:
            self._apply_patch()
            if self._our_instrumentor and getattr(self._our_instrumentor, "is_instrumented_by_opentelemetry", False):
                self._instrumented = True
                logger.debug("tp_trace: Celery integration successfully instrumented.")
                return True
            else:
                self._remove_patch()
                self._instrumented = False
                return False
        except Exception as exc:
            logger.warning("Failed to instrument Celery: %s", exc, exc_info=True)
            self._remove_patch()
            self._instrumented = False
            return False

    def _apply_patch(self) -> None:
        set_config(self._config)

        from tp_trace.integrations.celery.instrumentor import TpTraceCeleryInstrumentor

        existing = TpTraceCeleryInstrumentor._instance
        if existing is not None and existing.is_instrumented_by_opentelemetry:
            logger.warning("tp_trace Celery instrumentor already has an owner; skipping activation.")
            return
        self._our_instrumentor = TpTraceCeleryInstrumentor()
        if not self._our_instrumentor.is_instrumented_by_opentelemetry:
            self._our_instrumentor.instrument()

        # Never access current_app.tasks here: that property finalizes the app
        # and can prevent tasks imported later on another app from registering.
        from celery import _state

        _state.connect_on_app_finalize(self._watch_app_finalization)
        for app in list(_state._apps):
            self._watch_app_finalization(app)
        self._build_task_snapshot()

        # Cached AMQP senders call Producer.publish dynamically. Wrapping this
        # method covers senders cached before activation without replacing Celery's
        # cached_property descriptor or its application-specific closures.
        def _publish_error_cleanup_wrapper(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
            try:
                return wrapped(*args, **kwargs)
            except Exception as exc:
                if self._our_instrumentor is not None:
                    try:
                        from opentelemetry.instrumentation.celery import utils

                        headers = kwargs.get("headers") or {}
                        body = args[0] if args else kwargs.get("body")
                        # Protocol 2 carries identifiers in headers; protocol 1
                        # carries them in the body. Other Kombu traffic is ignored.
                        identifiers = headers if headers.get("id") else body
                        if isinstance(identifiers, dict):
                            task_id = identifiers.get("id")
                            name = identifiers.get("task")
                            if task_id and name:
                                task = utils.retrieve_task_from_sender({"sender": name})
                                self._our_instrumentor.handle_publish_failure(task, task_id, exc)
                    except Exception:
                        logger.debug("Celery publish cleanup failed", exc_info=True)
                raise

        self.wrap("kombu.messaging.Producer", "publish", _publish_error_cleanup_wrapper)

    def uninstrument(self) -> bool:
        """Uninstrument Celery and restore original state."""
        if not self._instrumented:
            return True

        return super().uninstrument()
