"""Regression coverage for sample bootstrap retries and settings re-entry."""

import importlib.util
from pathlib import Path
from unittest.mock import patch


def load_bootstrap():
    path = Path(__file__).resolve().parents[1] / "sample-app/config/otel.py"
    spec = importlib.util.spec_from_file_location("sample_otel_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_retries_after_failure(monkeypatch):
    monkeypatch.delenv("TP_TRACE_DISABLED", raising=False)
    module = load_bootstrap()
    with patch("tp_trace.init", side_effect=[RuntimeError("setup failed"), None]) as initialize:
        assert module.setup_telemetry() is False
        assert module.setup_telemetry() is True
        assert initialize.call_count == 2
        assert module.setup_telemetry() is True
        assert initialize.call_count == 2


def test_disabled_bootstrap_can_be_enabled(monkeypatch):
    module = load_bootstrap()
    monkeypatch.setenv("TP_TRACE_DISABLED", "true")
    assert module.setup_telemetry() is False
    monkeypatch.delenv("TP_TRACE_DISABLED")
    with patch("tp_trace.init") as initialize:
        assert module.setup_telemetry() is True
        initialize.assert_called_once()


def test_bootstrap_reentry_does_not_claim_success(monkeypatch):
    monkeypatch.delenv("TP_TRACE_DISABLED", raising=False)
    module = load_bootstrap()

    def reenter(**kwargs):
        assert module.setup_telemetry() is False

    with patch("tp_trace.init", side_effect=reenter) as initialize:
        assert module.setup_telemetry() is True
        initialize.assert_called_once()
