"""Compatibility layer for mutating span attributes across OTel SDK versions.

OpenTelemetry Python SDK exposes no public API to mutate attributes on an ended
ReadableSpan. tp_trace only does this as a last resort for third-party
instrumentors that add attributes after our control points. All access to
private internals goes through this module with version checks and a kill-switch.
"""

import logging
import os
from typing import Any, Dict, MutableMapping, Optional, Tuple

logger = logging.getLogger("tp_trace.compat.attributes")

try:
    from importlib import metadata

    _OTEL_SDK_VERSION = metadata.version("opentelemetry-sdk")
except Exception:
    _OTEL_SDK_VERSION = "unknown"


def _parse_version(v: str) -> Optional[Tuple[int, int, int]]:
    if not v or v == "unknown":
        return None
    # Strip common suffixes like b0, rc1
    try:
        # Take only digits and dots
        parts = []
        for token in v.replace("-", ".").replace("_", ".").split("."):
            # Extract leading digits
            num = ""
            for ch in token:
                if ch.isdigit():
                    num += ch
                else:
                    break
            if num:
                parts.append(int(num))
            if len(parts) >= 3:
                break
        if len(parts) >= 2:
            while len(parts) < 3:
                parts.append(0)
            return (parts[0], parts[1], parts[2])
    except Exception:
        pass
    return None


_OTEL_SDK_VER_TUPLE = _parse_version(_OTEL_SDK_VERSION)

_ATTR_MUTATION_DISABLED = os.environ.get("TP_TRACE_DISABLE_ATTR_MUTATION", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_WARNED_DISABLED = False


def _sdk_version_ok() -> bool:
    global _WARNED_DISABLED
    if _ATTR_MUTATION_DISABLED:
        if not _WARNED_DISABLED:
            logger.debug("tp_trace: attribute mutation disabled via TP_TRACE_DISABLE_ATTR_MUTATION")
            _WARNED_DISABLED = True
        return False
    if _OTEL_SDK_VER_TUPLE is None:
        if not _WARNED_DISABLED:
            logger.debug("tp_trace: could not parse opentelemetry-sdk version; disabling ended-span attribute mutation")
            _WARNED_DISABLED = True
        return False
    try:
        major, minor, _ = _OTEL_SDK_VER_TUPLE
        if (major, minor) < (1, 20):
            if not _WARNED_DISABLED:
                logger.debug("tp_trace: opentelemetry-sdk %s too old for ended-span mutation", _OTEL_SDK_VERSION)
                _WARNED_DISABLED = True
            return False
        # Don't fail closed on newer versions - rely on structure checks
        return True
    except Exception:
        if not _WARNED_DISABLED:
            logger.debug("tp_trace: attribute mutation disabled due to version check failure")
            _WARNED_DISABLED = True
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
    # Prefer known internal names for BoundedAttributes
    for name in ("_dict",):
        try:
            cand = getattr(attrs_obj, name, None)
            if isinstance(cand, dict):
                return cand
            if isinstance(cand, MutableMapping):
                return cand
        except Exception:
            continue
    return None


def get_mutable_attributes(span: Any) -> Optional[MutableMapping]:
    """Get a mutable attribute mapping if possible."""
    if not _sdk_version_ok():
        return None
    attrs_obj = _get_attrs_obj(span)
    target = _find_mutable_dict(attrs_obj)
    if target is not None:
        return target
    return None


def set_attribute_safe(span: Any, key: str, value: Any) -> bool:
    # For recording spans, use public API. For ended spans, write via compat first.
    try:
        if _is_recording_span(span):
            try:
                if hasattr(span, "set_attribute"):
                    span.set_attribute(key, value)
                    return True
            except Exception:
                pass
            return False
    except Exception:
        pass
    target = get_mutable_attributes(span)
    if target is not None:
        try:
            target[key] = value
            return True
        except Exception:
            pass
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
