"""Custom Celery instrumentor subclass for tp_trace.

Overrides all six upstream signal handlers to provide:
1. Enriched task attributes and bounded operations at span creation.
2. Suppression of upstream metrics (no Flower histograms or meter overhead).
3. Consistent error / retry semantics.
4. Robust cleanup on failure paths, preventing context and timing leaks.
"""

import logging
from typing import Any, Dict, Optional

from billiard import VERSION as BILLIARD_VERSION
from billiard.einfo import ExceptionInfo
from opentelemetry import context as context_api
from opentelemetry.instrumentation.celery import CeleryInstrumentor, utils
from opentelemetry.trace.status import Status, StatusCode

from tp_trace.integrations.celery.context import (
    CeleryCreationMeta,
    compute_bounded_operation,
    reset_celery_creation_meta,
    set_celery_creation_meta,
)
from tp_trace.safety import (
    find_exception,
    safe_record_exception,
    safe_set_attribute,
    safe_set_status,
    safe_update_name,
)

if BILLIARD_VERSION >= (4, 0, 1):
    from billiard.einfo import ExceptionWithTraceback
else:
    ExceptionWithTraceback = None

logger = logging.getLogger("tp_trace.integrations.celery")


class TpTraceCeleryInstrumentor(CeleryInstrumentor):
    """Subclass of CeleryInstrumentor owning distinct signal receivers and telemetry lifecycle."""

    _instance: Optional["TpTraceCeleryInstrumentor"] = None

    def __init__(self) -> None:
        super().__init__()
        self.metrics: Dict[str, Any] = {}
        self.task_id_to_start_time: Dict[str, float] = {}
        # One recovery token per running task, detached on failure or postrun.
        self._prerun_context_tokens: Dict[str, Any] = {}

    def create_celery_metrics(self, meter: Any) -> None:
        """Suppress upstream Celery metrics; tp_trace uses collector spanmetrics only."""
        self.metrics = {}

    def _record_histograms(self, task_id: Any, metric_attributes: Any) -> None:
        """No-op override to suppress Flower histogram recording."""
        pass

    def _trace_prerun(self, *args: Any, **kwargs: Any) -> None:
        task = utils.retrieve_task(kwargs)
        task_id = utils.retrieve_task_id(kwargs)
        task_name = getattr(task, "name", None)

        bounded_op = compute_bounded_operation("run", task_name, registered=task is not None)
        span_name = f"🟣 {task_name}.run" if task_name and task_name != "unknown" else "🟣 celery.run"
        meta = CeleryCreationMeta(
            action="run",
            bounded_operation=bounded_op,
            span_name=span_name,
        )

        if task is None or task_id is None:
            return
        # The upstream incoming-context token is not stored until after span
        # creation. Keep our own restoration token for failures before that point.
        restore_token = context_api.attach(context_api.get_current())
        token = set_celery_creation_meta(meta)

        # Defensive propagation support: Celery stores incoming message headers in task.request.headers.
        # Upstream CeleryGetter does getattr(task.request, key). Ensure W3C propagation attributes
        # on task.request are populated from task.request.headers if missing.
        try:
            req = getattr(task, "request", None)
            if req is not None:
                req_headers = getattr(req, "headers", None)
                if req_headers is None and isinstance(req, dict):
                    req_headers = req.get("headers")
                if isinstance(req_headers, dict):
                    for k in ("traceparent", "tracestate", "baggage"):
                        if k in req_headers and not hasattr(req, k):
                            try:
                                setattr(req, k, req_headers[k])
                            except Exception:
                                pass
        except Exception:
            pass

        try:
            try:
                super()._trace_prerun(*args, **kwargs)
            finally:
                reset_celery_creation_meta(token)
        except Exception:
            try:
                ctx = utils.retrieve_context(task, task_id)
                if ctx is not None:
                    _, activation, otel_token = ctx
                    try:
                        activation.__exit__(None, None, None)
                    finally:
                        utils.detach_context(task, task_id)
                        if otel_token is not None:
                            context_api.detach(otel_token)  # type: ignore[arg-type]
            except Exception:
                logger.debug("Celery prerun cleanup failed", exc_info=True)
            finally:
                context_api.detach(restore_token)
                self.task_id_to_start_time.pop(task_id, None)
            logger.debug("Upstream _trace_prerun failed", exc_info=True)
            return

        self._prerun_context_tokens[task_id] = restore_token

        # Enrichment
        try:
            if task and task_id:
                ctx = utils.retrieve_context(task, task_id)
                if ctx is not None:
                    span, _, _ = ctx
                    if span is not None and getattr(span, "is_recording", lambda: True)():
                        safe_update_name(span, span_name)
                        safe_set_attribute(span, "celery.task_id", task_id)
                        if task_name:
                            safe_set_attribute(span, "celery.task_name", task_name)
                        req = getattr(task, "request", None)
                        if req is not None:
                            hostname = getattr(req, "hostname", None)
                            if hostname:
                                safe_set_attribute(span, "celery.worker", str(hostname))
                            origin = getattr(req, "origin", None)
                            if origin:
                                safe_set_attribute(span, "celery.origin", str(origin))
                            delivery_info = getattr(req, "delivery_info", None)
                            if isinstance(delivery_info, dict):
                                rk = delivery_info.get("routing_key")
                                if rk:
                                    safe_set_attribute(span, "celery.queue", str(rk))
                            req_headers = getattr(req, "headers", None)
                            if req_headers is None and isinstance(req, dict):
                                req_headers = req.get("headers")
                            trigger_view = None
                            if isinstance(req_headers, dict):
                                trigger_view = req_headers.get("tp_trigger_view")
                            if not trigger_view:
                                # Fallback for tasks executed eagerly in-process within the same thread
                                # (e.g. task_always_eager=True in tests or synchronous workflows),
                                # where task request headers may not have traversed amqp serialization.
                                try:
                                    from tp_trace.route_context import get_current_view

                                    trigger_view = get_current_view()
                                except Exception:
                                    pass
                            if trigger_view:
                                safe_set_attribute(span, "celery.trigger.view", str(trigger_view))
        except Exception:
            logger.debug("Error during _trace_prerun enrichment", exc_info=True)

    def _trace_before_publish(self, *args: Any, **kwargs: Any) -> None:
        task = utils.retrieve_task_from_sender(kwargs)
        task_id = utils.retrieve_task_id_from_message(kwargs)

        task_name = getattr(task, "name", None)
        if not task_name:
            sender = kwargs.get("sender")
            if isinstance(sender, str):
                task_name = sender
            elif hasattr(sender, "name"):
                task_name = getattr(sender, "name")
            else:
                headers = kwargs.get("headers")
                if isinstance(headers, dict) and headers.get("task"):
                    task_name = str(headers.get("task"))
                else:
                    task_name = "unknown"

        bounded_op = compute_bounded_operation("apply_async", task_name, registered=task is not None)
        span_name = f"🟣 {task_name}.apply_async" if task_name and task_name != "unknown" else "🟣 celery.apply_async"
        meta = CeleryCreationMeta(
            action="apply_async",
            bounded_operation=bounded_op,
            span_name=span_name,
        )

        trigger_view = None
        try:
            from tp_trace.route_context import get_current_view

            trigger_view = get_current_view()
        except Exception:
            pass

        headers = kwargs.get("headers")
        if isinstance(headers, dict) and trigger_view:
            headers["tp_trigger_view"] = trigger_view

        token = set_celery_creation_meta(meta)
        try:
            try:
                super()._trace_before_publish(*args, **kwargs)
            finally:
                reset_celery_creation_meta(token)
        except Exception:
            logger.debug("Upstream _trace_before_publish failed", exc_info=True)
            return

        # Defensive propagation injection: ensure trace context is injected even if headers dict
        # was initially empty (upstream checks `if headers: inject(headers)`, skipping empty dicts).
        try:
            if isinstance(headers, dict) and "traceparent" not in headers:
                from opentelemetry.propagate import inject

                inject(headers)
        except Exception:
            pass

        # Only enrich a verified producer from upstream's context store.
        # Unknown-task producers can already have ended; the ambient span then
        # belongs to the application and must never be enriched or ended here.
        try:
            if task is not None and task_id:
                ctx = utils.retrieve_context(task, task_id, is_publish=True)
                span = ctx[0] if ctx is not None else None
                if span is not None and getattr(span, "is_recording", lambda: True)():
                    safe_update_name(span, span_name)
                    safe_set_attribute(span, "celery.task_id", task_id)
                    safe_set_attribute(span, "celery.task_name", task_name)
                    if trigger_view:
                        safe_set_attribute(span, "celery.trigger.view", trigger_view)

                    # Extract producer queue
                    queue_name = None
                    declare = kwargs.get("declare")
                    if declare and hasattr(declare, "__iter__"):
                        for item in declare:
                            name_val = getattr(item, "name", None)
                            if name_val and isinstance(name_val, str):
                                queue_name = name_val
                                break
                    if not queue_name:
                        rk = kwargs.get("routing_key") or (kwargs.get("properties") or {}).get("routing_key")
                        if rk:
                            queue_name = str(rk)
                    if queue_name:
                        safe_set_attribute(span, "celery.queue", queue_name)
        except Exception:
            logger.debug("Error during _trace_before_publish enrichment", exc_info=True)

    def _trace_after_publish(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        try:
            task = utils.retrieve_task_from_sender(kwargs)
            task_id = utils.retrieve_task_id_from_message(kwargs)
            ctx = utils.retrieve_context(task, task_id, is_publish=True) if (task and task_id) else None
        except Exception:
            task = None
            task_id = None
            ctx = None

        try:
            super()._trace_after_publish(*args, **kwargs)
        except Exception as exc:
            logger.debug("Upstream _trace_after_publish failed: %s", exc, exc_info=True)
            if ctx is not None:
                _, activation, _ = ctx
                try:
                    activation.__exit__(None, None, None)
                except Exception:
                    pass
                if task and task_id:
                    utils.detach_context(task, task_id, is_publish=True)

    def _trace_failure(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        upstream_failed = False
        try:
            super()._trace_failure(*args, **kwargs)
        except Exception:
            upstream_failed = True
            logger.debug("Upstream _trace_failure failed", exc_info=True)

        try:
            task = utils.retrieve_task_from_sender(kwargs)
            task_id = utils.retrieve_task_id(kwargs)
            if not task or not task_id:
                return

            ctx = utils.retrieve_context(task, task_id)
            if ctx is None:
                return

            span, _, _ = ctx
            if span is not None and getattr(span, "is_recording", lambda: True)():
                safe_set_status(span, Status(status_code=StatusCode.ERROR))
                safe_set_attribute(span, "error", True)

                # Extract exception from einfo or kwargs
                ex = kwargs.get("einfo")
                exception_obj = None
                if ex is not None:
                    if hasattr(ex, "exception") and ex.exception is not None:
                        exception_obj = ex.exception
                    elif hasattr(ex, "exc") and ex.exc is not None:
                        exception_obj = ex.exc
                    else:
                        exception_obj = ex
                if exception_obj is None:
                    exception_obj = kwargs.get("exception")

                # Unwrap billiard wrappers
                if ExceptionInfo is not None and isinstance(exception_obj, ExceptionInfo):
                    exception_obj = exception_obj.exception
                if ExceptionWithTraceback is not None and isinstance(exception_obj, ExceptionWithTraceback):
                    exception_obj = exception_obj.exc

                if exception_obj is not None:
                    unwrapped = find_exception(exception_obj)
                    if hasattr(unwrapped, "exc_cls_name") and getattr(unwrapped, "exc_cls_name"):
                        safe_set_attribute(span, "error.type", str(getattr(unwrapped, "exc_cls_name")))
                    else:
                        safe_set_attribute(span, "error.type", type(unwrapped).__name__)
                    if upstream_failed:
                        safe_record_exception(span, unwrapped)
        except Exception:
            logger.debug("Error during _trace_failure enrichment", exc_info=True)

    def _trace_retry(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        try:
            super()._trace_retry(*args, **kwargs)
        except Exception:
            logger.debug("Upstream _trace_retry failed", exc_info=True)

        try:
            task = utils.retrieve_task_from_sender(kwargs)
            task_id = utils.retrieve_task_id_from_request(kwargs)
            if not task or not task_id:
                return

            ctx = utils.retrieve_context(task, task_id)
            if ctx is None:
                return

            span, _, _ = ctx
            if span is not None and getattr(span, "is_recording", lambda: True)():
                safe_set_status(span, Status(status_code=StatusCode.UNSET))
                safe_set_attribute(span, "error", False)
                safe_set_attribute(span, "celery.task_state", "RETRY")
                reason = utils.retrieve_reason(kwargs)
                if reason is not None:
                    try:
                        safe_set_attribute(span, "celery.retry.reason", str(reason))
                    except Exception:
                        safe_set_attribute(span, "celery.retry.reason", "Unknown retry reason")
        except Exception:
            logger.debug("Error during _trace_retry enrichment", exc_info=True)

    def _trace_postrun(self, *args: Any, **kwargs: Any) -> None:
        task = utils.retrieve_task(kwargs)
        task_id = utils.retrieve_task_id(kwargs)

        ctx = utils.retrieve_context(task, task_id) if (task and task_id) else None

        # State enrichment BEFORE upstream finalization ends the span
        if ctx is not None:
            try:
                span, _, _ = ctx
                if span is not None and getattr(span, "is_recording", lambda: True)():
                    state = kwargs.get("state")
                    if state:
                        safe_set_attribute(span, "celery.task_state", str(state))

                    status = getattr(span, "status", None)
                    current_code = getattr(status, "status_code", StatusCode.UNSET)

                    if state == "SUCCESS":
                        if current_code != StatusCode.ERROR:
                            safe_set_status(span, Status(status_code=StatusCode.OK))
                            safe_set_attribute(span, "error", False)
                    elif state == "FAILURE":
                        safe_set_status(span, Status(status_code=StatusCode.ERROR))
                        safe_set_attribute(span, "error", True)
                    elif state == "RETRY":
                        safe_set_status(span, Status(status_code=StatusCode.UNSET))
                        safe_set_attribute(span, "error", False)
            except Exception:
                logger.debug("Error during _trace_postrun state enrichment", exc_info=True)

        try:
            try:
                super()._trace_postrun(*args, **kwargs)
            except Exception as exc:
                logger.debug("Upstream _trace_postrun failed: %s", exc, exc_info=True)
                # Upstream finalization failed: best-effort cleanup
                if ctx is not None:
                    span, activation, token = ctx
                    try:
                        activation.__exit__(None, None, None)
                    except Exception:
                        pass
                    if span is not None and getattr(span, "is_recording", lambda: False)():
                        try:
                            span.end()
                        except Exception:
                            pass
                    if task and task_id:
                        utils.detach_context(task, task_id)
                    if token is not None:
                        try:
                            context_api.detach(token)  # type: ignore[arg-type]
                        except Exception:
                            pass
        finally:
            restore_token = self._prerun_context_tokens.pop(task_id, None) if task_id else None
            if restore_token is not None:
                try:
                    context_api.detach(restore_token)
                except Exception:
                    pass
            # Defensive timing map cleanup against leaks across all upstream versions
            if hasattr(self, "task_id_to_start_time") and isinstance(self.task_id_to_start_time, dict):
                if task_id:
                    self.task_id_to_start_time.pop(task_id, None)

    def handle_publish_failure(self, task: Any, task_id: Optional[str], exc: Exception) -> None:
        """Clean up active producer span when broker publication fails."""
        if task is None or task_id is None:
            return
        try:
            span_dict = getattr(task, utils.CTX_KEY, {})
            for (tid, is_pub), (span, activation, token) in list(span_dict.items()):
                if is_pub and tid == task_id:
                    safe_record_exception(span, exc)
                    unwrapped = find_exception(exc)
                    safe_set_attribute(span, "error", True)
                    safe_set_attribute(span, "error.type", type(unwrapped).__name__)
                    safe_set_status(span, Status(status_code=StatusCode.ERROR))
                    try:
                        activation.__exit__(None, None, None)
                    except Exception:
                        pass
                    utils.detach_context(task, tid, is_publish=True)

        except Exception:
            logger.debug("Error during handle_publish_failure", exc_info=True)
