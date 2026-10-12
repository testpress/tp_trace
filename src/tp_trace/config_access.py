"""Shared config seam for integration hook modules.

Integration hooks are module-level functions that cannot reach the
integration instance, so each hook module needs access to the active
SDKConfig. This is the single shared accessor for that pattern: integrations
push their config in via :func:`set_config` at patch time, hooks read it via
:func:`get_config` at call time, and everything falls back to the active
``tp_trace._ACTIVE_CONFIG`` when no integration has pushed one.
"""

from typing import Optional

import tp_trace
from tp_trace.config import SDKConfig

_config: Optional[SDKConfig] = None


def set_config(config: Optional[SDKConfig]) -> None:
    """Set the active SDKConfig via the integration seam."""
    global _config
    _config = config


def get_config() -> Optional[SDKConfig]:
    """Return the config received through the integration seam, or active config."""
    if _config is not None:
        return _config
    return getattr(tp_trace, "_ACTIVE_CONFIG", None)
