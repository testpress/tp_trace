"""Base integration class for wrapping and monkey-patching libraries."""

import abc
import importlib
import logging
from typing import Any, Callable, List, Optional, Tuple, Union

import wrapt

from tp_trace.config import SDKConfig

logger = logging.getLogger("tp_trace.integrations")


class BaseIntegration(abc.ABC):
    """
    Abstract base class for all library integrations.

    Provides thread-safe monkey-patching via wrapt, deferred import hooks,
    and reversible unwrapping.
    """

    name: str = "base"

    def __init__(self, config: Optional[SDKConfig] = None) -> None:
        self._config = config
        self._instrumented: bool = False
        self._wrapped_targets: List[Tuple[Any, str, Any]] = []
        self._wrapped_keys: set = set()
        self._when_imported_hooks: List[Tuple[str, Callable]] = []

    @property
    def config(self) -> Optional[SDKConfig]:
        return self._config

    @config.setter
    def config(self, value: Optional[SDKConfig]) -> None:
        self._config = value

    @abc.abstractmethod
    def is_installed(self) -> bool:
        """Check if target library or framework is installed in the current environment."""
        pass

    @abc.abstractmethod
    def _apply_patch(self) -> None:
        """Apply monkey-patches to target library classes and functions."""
        pass

    def _remove_patch(self) -> None:
        """Undo all monkey patches registered via self.wrap()."""
        self.unwrap_all()

    def wrap(
        self,
        target: Union[str, Any],
        attribute_name: str,
        wrapper: Callable[..., Any],
    ) -> None:
        """
        Safely wraps a function/method while tracking original for clean uninstrumentation.

        Guarantees that any internal SDK failure will never crash the wrapped host
        method. Telemetry failures are absorbed at their source -- :func:`traced_span`
        degrades to a no-op span when the TracerProvider is unusable, and span
        enrichment is individually guarded -- so an exception escaping ``wrapper`` is
        the application's own and MUST be propagated.

        This wrapper deliberately does not retry the call. The wrapped method may
        already have run, and re-invoking it would execute application code twice
        (duplicate writes, repeated outbound requests).

        Args:
            target: Either an imported class/module object or a fully-qualified string (e.g. 'django.views.View')
            attribute_name: Name of the method/attribute to wrap
            wrapper: The wrapper function with signature (wrapped, instance, args, kwargs)
        """
        try:
            if isinstance(target, str):
                module_name, _, class_or_fn = target.rpartition(".")
                target_mod = importlib.import_module(module_name)
                target_obj = getattr(target_mod, class_or_fn)
            else:
                target_obj = target

            key = (id(target_obj), attribute_name)
            if key in self._wrapped_keys:
                return

            original = getattr(target_obj, attribute_name, None)
            wrapt.wrap_function_wrapper(target_obj, attribute_name, wrapper)
            self._wrapped_keys.add(key)
            self._wrapped_targets.append((target_obj, attribute_name, original))
        except Exception as exc:
            logger.debug(
                "Failed to wrap %s on %s in %s integration: %s",
                attribute_name,
                target,
                self.name,
                exc,
            )

    def unwrap_all(self) -> None:
        """Restores all wrapped targets to their original unwrapped states in reverse order."""
        failed_targets = []
        while self._wrapped_targets:
            target_obj, attr_name, original = self._wrapped_targets.pop()
            key = (id(target_obj), attr_name)
            try:
                current = getattr(target_obj, attr_name, None)
                if current is not None and hasattr(current, "__wrapped__"):
                    setattr(target_obj, attr_name, current.__wrapped__)
                elif original is not None:
                    setattr(target_obj, attr_name, original)
                elif hasattr(target_obj, attr_name):
                    delattr(target_obj, attr_name)
                self._wrapped_keys.discard(key)
            except Exception as exc:
                logger.debug(
                    "Error unwrapping %s on %s in %s integration: %s",
                    attr_name,
                    target_obj,
                    self.name,
                    exc,
                )
                failed_targets.append((target_obj, attr_name, original))

        if failed_targets:
            self._wrapped_targets.extend(failed_targets)

    def when_imported(self, module_name: str, callback: Callable[..., Any]) -> None:
        """
        Registers a hook to patch a module when it is imported, or immediately if already loaded.
        """
        try:
            wrapt.when_imported(module_name)(callback)
            self._when_imported_hooks.append((module_name, callback))
        except Exception as exc:
            logger.debug(
                "Failed to register when_imported hook for %s in %s integration: %s",
                module_name,
                self.name,
                exc,
            )

    def instrument(self) -> bool:
        """Instrument the library safely, ensuring idempotency."""
        if self._instrumented:
            return True

        if not self.is_installed():
            logger.debug("Integration %s not installed, skipping.", self.name)
            return False

        try:
            self._apply_patch()
            self._instrumented = True
            logger.debug("Successfully instrumented integration: %s", self.name)
            return True
        except Exception as exc:
            logger.warning(
                "Failed to instrument %s: %s",
                self.name,
                exc,
                exc_info=True,
            )
            return False

    def on_unavailable(self) -> None:
        """Called when the target library is not installed at apply time.

        Subclasses can override to register deferred re-arm hooks so that
        instrumentation runs once the dependency becomes importable.
        """
        pass

    def uninstrument(self) -> bool:
        """Uninstrument the library and restore original functions."""
        if not self._instrumented:
            return True
        try:
            self._remove_patch()
            if self._wrapped_targets:
                logger.debug(
                    "Failed to completely uninstrument %s; %d targets remained wrapped",
                    self.name,
                    len(self._wrapped_targets),
                )
                return False
            self._instrumented = False
            logger.debug("Successfully uninstrumented integration: %s", self.name)
            return True
        except Exception as exc:
            logger.debug("Failed to uninstrument %s: %s", self.name, exc)
            return False
