"""Tests for Boto / S3 / AWS SDK integration."""

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import pytest
import tp_trace
from tp_trace.integrations.boto import BotoIntegration
from tp_trace.integrations.manager import get_integration_manager

pytest.importorskip("botocore")


def setup_function():
    tp_trace._reset_for_testing()


def teardown_function():
    tp_trace._reset_for_testing()


def test_boto_integration_discovery():
    """Verify BotoIntegration is registered and auto-detected."""
    mgr = get_integration_manager()
    mgr.apply_integrations()
    boto_inst = mgr.get("boto")
    assert boto_inst is not None
    assert isinstance(boto_inst, BotoIntegration)
    assert boto_inst.is_installed() is True

    # Test aliases
    assert mgr.get("boto3") is boto_inst
    assert mgr.get("botocore") is boto_inst
    assert mgr.get("aws") is boto_inst
    assert mgr.get("s3") is boto_inst


def test_boto_integration_instrument_lifecycle():
    """Verify BotoIntegration instrument and uninstrument calls."""
    exporter = InMemorySpanExporter()
    tp_trace.init(
        project_name="boto-test-service",
        exporter=exporter,
        export_batch=False,
    )

    mgr = get_integration_manager()
    boto_inst = mgr.get("boto")

    assert boto_inst._instrumented is True

    # Uninstrument cleanly
    boto_inst.uninstrument()
    assert boto_inst._instrumented is False

    # Re-instrument
    boto_inst.instrument()
    assert boto_inst._instrumented is True


def test_boto_request_hook_s3_icon():
    """Verify _tp_trace_boto_request_hook prefixes S3 spans with 🪣."""
    from tp_trace.integrations.boto.integration import (
        _tp_trace_boto_request_hook,
        _tp_trace_boto_response_hook,
    )

    class MockSpan:
        def __init__(self, name):
            self.name = name

        def is_recording(self):
            return True

        def update_name(self, new_name):
            self.name = new_name

    s3_span = MockSpan("S3.ListObjectsV2")
    _tp_trace_boto_request_hook(s3_span, "s3", "ListObjectsV2", {})
    assert s3_span.name == "🪣 S3.ListObjectsV2"

    # Idempotent: should not double-prefix
    _tp_trace_boto_response_hook(s3_span, "s3", "ListObjectsV2", {})
    assert s3_span.name == "🪣 S3.ListObjectsV2"

    # Non-S3 AWS call gets ☁️
    sqs_span = MockSpan("SQS.SendMessage")
    _tp_trace_boto_request_hook(sqs_span, "sqs", "SendMessage", {})
    assert sqs_span.name == "☁️ SQS.SendMessage"
