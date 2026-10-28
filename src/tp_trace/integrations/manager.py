"""Integration manager for discovery, registration, and activation of library integrations."""

import importlib
import logging
import threading
from typing import Any, Dict, List, Optional, Type, Union

from tp_trace.config import SDKConfig
from tp_trace.integrations.base import BaseIntegration

logger = logging.getLogger("tp_trace.integrations")

# Default mapping of canonical integration names to their module and class paths
_BUILTIN_INTEGRATIONS: Dict[str, str] = {
    "django": "tp_trace.integrations.django.DjangoIntegration",
    "postgres": "tp_trace.integrations.postgres.PostgresIntegration",
    "redis": "tp_trace.integrations.redis.RedisIntegration",
    "requests": "tp_trace.integrations.requests.RequestsIntegration",
    "boto": "tp_trace.integrations.boto.BotoIntegration",
    "celery": "tp_trace.integrations.celery.CeleryIntegration",
}

_INTEGRATION_DEFAULTS: Dict[str, bool] = {
    "celery": True,
}

_INTEGRATION_ALIASES: Dict[str, str] = {
    "postgresql": "postgres",
    "psycopg2": "postgres",
    # Driver name, not a support claim: Django projects on the psycopg3 driver
    # are traced through the Django seams. A non-Django project using raw psycopg3
    # is NOT traced (PostgresIntegration.is_installed does not probe it).
    "psycopg": "postgres",
    "http": "requests",
    "urllib3": "requests",
    "boto3": "boto",
    "botocore": "boto",
    "aws": "boto",
    "s3": "boto",
}


class IntegrationManager:
    """Manages discovery, lifecycle, and activation of framework integrations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registered_classes: Dict[str, Union[Type[BaseIntegration], str]] = dict(_BUILTIN_INTEGRATIONS)
        self._aliases: Dict[str, str] = dict(_INTEGRATION_ALIASES)
        for alias, canonical in self._aliases.items():
            if canonical in _BUILTIN_INTEGRATIONS and alias not in self._registered_classes:
                self._registered_classes[alias] = _BUILTIN_INTEGRATIONS[canonical]
        self._active_instances: Dict[str, BaseIntegration] = {}

    def register(
        self,
        name: str,
        integration: Union[Type[BaseIntegration], str],
        alias_for: Optional[str] = None,
    ) -> None:
        """Register a new or custom integration class."""
        key = name.lower()
        with self._lock:
            if alias_for:
                self._aliases[key] = alias_for.lower()
            else:
                self._registered_classes[key] = integration

    def get(self, name: str) -> Optional[BaseIntegration]:
        """Get an active integration instance by name or alias."""
        with self._lock:
            canonical = self._aliases.get(name.lower(), name.lower())
            return self._active_instances.get(canonical)

    def _resolve_class(self, entry: Union[Type[BaseIntegration], str]) -> Optional[Type[BaseIntegration]]:
        """Resolve a class reference from string or return class directly."""
        if isinstance(entry, str):
            try:
                mod_name, _, cls_name = entry.rpartition(".")
                mod = importlib.import_module(mod_name)
                return getattr(mod, cls_name)
            except (ImportError, AttributeError) as exc:
                logger.debug("Could not import integration class %s: %s", entry, exc)
                return None
        return entry

    def apply_integrations(
        self,
        config: Optional[SDKConfig] = None,
        **kwargs: Any,
    ) -> List[str]:
        """
        Apply all enabled integrations based on environment, configuration, and kwargs.

        Args:
            config: Active SDKConfig
            **kwargs: Direct overrides for integrations (e.g. django=False, redis=True)

        Returns:
            List of successfully instrumented integration names.
        """
        with self._lock:
            instrumented_names: List[str] = []
            user_overrides: Dict[str, bool] = {}

            # Fold every user-supplied key (canonical or alias) to its canonical name
            raw_settings: Dict[str, Any] = {}
            if config and config.integrations:
                raw_settings.update(config.integrations)
            raw_settings.update(kwargs)

            for key, value in raw_settings.items():
                canonical = self._aliases.get(key.lower(), key.lower())
                if key.lower() != canonical:
                    user_overrides[canonical] = bool(value)  # alias wins (e.g. psycopg2=False)
                else:
                    user_overrides.setdefault(canonical, bool(value))

            canonical_order = ["django", "postgres", "redis", "requests", "boto", "celery"]
            seen_canonical = set()
            all_keys = [k for k in canonical_order if k in self._registered_classes]
            all_keys += [k for k in self._registered_classes if k not in self._aliases and k not in all_keys]

            for canonical in all_keys:
                if canonical in seen_canonical:
                    continue
                seen_canonical.add(canonical)

                entry = self._registered_classes.get(canonical)
                if entry is None:
                    continue

                if not user_overrides.get(canonical, _INTEGRATION_DEFAULTS.get(canonical, True)):
                    logger.debug("Integration '%s' is disabled by configuration.", canonical)
                    continue

                # Instantiate integration if not already active
                if canonical not in self._active_instances:
                    cls = self._resolve_class(entry)
                    if cls is None:
                        continue
                    self._active_instances[canonical] = cls(config=config)
                elif config is not None:
                    self._active_instances[canonical].config = config

                instance = self._active_instances[canonical]

                if instance.is_installed():
                    success = instance.instrument()
                    if success:
                        instrumented_names.append(canonical)
                else:
                    try:
                        instance.on_unavailable()
                    except Exception:
                        logger.debug("on_unavailable hook failed for '%s'", canonical)
                    logger.debug("Library for integration '%s' is not installed, skipping.", canonical)

            # For backwards compatibility with tests asserting on alias names:
            for alias, can in self._aliases.items():
                if can in instrumented_names and alias not in instrumented_names:
                    instrumented_names.append(alias)

            return instrumented_names

    def uninstrument_all(self) -> None:
        """Uninstrument all active integrations and restore original behaviors."""
        with self._lock:
            for name, instance in list(self._active_instances.items()):
                try:
                    instance.uninstrument()
                except Exception as exc:
                    logger.debug("Error uninstrumenting %s: %s", name, exc)
            self._active_instances.clear()


_GLOBAL_INTEGRATION_MANAGER = IntegrationManager()


def get_integration_manager() -> IntegrationManager:
    """Return the global IntegrationManager singleton."""
    global _GLOBAL_INTEGRATION_MANAGER
    return _GLOBAL_INTEGRATION_MANAGER
