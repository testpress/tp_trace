"""Compatibility layer for mutating span attributes across OTel SDK versions.

OpenTelemetry Python SDK exposes no public API to mutate attributes on an ended
ReadableSpan. tp_trace only does this as a last resort for third-party
instrumentors that add attributes after our control points. All access to
private internals goes through this module with version checks and a kill-switch.
"""

import logging
import os
from typing import Any, Dict, Mapping, MutableMapping, Optional

logger = logging.getLogger("tp_trace.compat.attributes")

try:
    from importlib import metadata

    _OTEL_SDK_VERSION = metadata.version("opentelemetry-sdk")
except Exception:
    _OTEL_SDK_VERSION = "unknown"

try:
    from packaging.version import Version

    _OTEL_SDK_VER = Version(_OTEL_SDK_VERSION) if _OTEL_SDK_VERSION != "unknown" else None
except Exception:
    _OTEL_SDK_VER = None

_ATTR_MUTATION_DISABLED = os.environ.get("TP_TRACE_DISABLE_ATTR_MUTATION", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _sdk_version_ok() -> bool:
    if _ATTR_MUTATION_DISABLED:
        return False
    if _OTEL_SDK_VER is None:
        return False
    try:
        return (1, 20) <= (_OTEL_SDK_VER.major, _OTEL_SDK_VER.minor) <= (1, 60)
    except Exception:
        return False


def _is_recording_span(span: Any) -> bool:
    try:
        if hasattr(span, "is_recording") and span.is_recording():
            return True
    except Exception:
        pass
    return False


def _get_attrs_obj(span: Any) -> Any:
    attrs = getattr(span, "_attributes", None)
    if attrs is not None:
        return attrs
    attrs = getattr(span, "attributes", None)
    return attrs


def _find_mutable_dict(attrs_obj: Any) -> Optional[MutableMapping]:
    if attrs_obj is None:
        return None
    if isinstance(attrs_obj, dict):
        return attrs_obj
    for name in ("_dict", "_attributes", "attributes", "_attrs"):
        try:
            cand = getattr(attrs_obj, name, None)
            if isinstance(cand, dict):
                return cand
            if cand is not None and hasattr(cand, "__setitem__"):
                # Some objects are dict-like
                if isinstance(cand, MutableMapping):
                    return cand
        except Exception:
            continue
    # Try __dict__
    try:
        for v in attrs_obj.__dict__.values():
            if isinstance(v, dict):
                return v
            if isinstance(v, MutableMapping):
                return v
    except Exception:
        pass
    return None


def get_mutable_attributes(span: Any) -> Optional[MutableMapping]:
    """Get a mutable attribute mapping if possible. Prefer public API."""
    if _is_recording_span(span):
        # Use public API on recording spans
        return None
    if not _sdk_version_ok():
        return None
    attrs_obj = _get_attrs_obj(span)
    target = _find_mutable_dict(attrs_obj)
    if target is not None:
        return target
    return None


def set_attribute_safe(span: Any, key: str, value: Any) -> bool:
    # Prefer public API
    try:
        if hasattr(span, "set_attribute"):
            try:
                span.set_attribute(key, value)
                return True
            except Exception:
                pass
    except Exception:
        pass
    target = get_mutable_attributes(span)
    if target is None:
        return False
    try:
        target[key] = value
        return True
    except Exception:
        return False


def delete_attribute_safe(span: Any, key: str) -> bool:
    target = get_mutable_attributes(span)
    if target is None:
        return False
    try:
        if key in target:
            del target[key]
            return True
    except Exception:
        pass
    return False


def get_attributes_view(span: Any) -> Dict[str, Any]:
    try:
        attrs = getattr(span, "attributes", None)
        if attrs is not None:
            try:
                return dict(attrs)
            except Exception:
                pass
    except Exception:
        pass
    try:
        attrs_obj = _get_attrs_obj(span)
        if attrs_obj is not None:
            try:
                return dict(attrs_obj)
            except Exception:
                pass
    except Exception:
        pass
    return {}
