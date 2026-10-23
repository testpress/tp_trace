"""Pressure tests apply equal budgets and reject inconsistent memory limits."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_runner():
    path = Path(__file__).resolve().parent / "e2e/check_collector_cardinality.py"
    spec = importlib.util.spec_from_file_location("collector_runner_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pressure_fixture_applies_same_heap_and_container_budgets(tmp_path):
    runner = load_runner()
    args = SimpleNamespace(expiration=6, limit=16, soak_seconds=30, sample_image="local-sample",
                           collector_memory_mib=256, heap_limit_mib=96, heap_spike_mib=16)
    compose = json.loads(runner.fixture(tmp_path, args).read_text())
    for name in ("old", "fixed"):
        assert compose["services"][name]["mem_limit"] == "256m"
        config = json.loads((tmp_path / (name + ".json")).read_text())
        limiter = config["processors"]["memory_limiter"]
        assert limiter["limit_mib"] - limiter["spike_limit_mib"] == 80
        assert limiter["limit_mib"] == 96
    assert "metrics_expiration" not in json.loads((tmp_path / 'old.json').read_text())["connectors"]["spanmetrics"]
    assert json.loads((tmp_path / 'fixed.json').read_text())["connectors"]["spanmetrics"]["metrics_expiration"] == "6s"


@pytest.mark.parametrize("flags", [
    ["--collector-memory-mib", "64"],
    ["--collector-memory-mib", "256", "--heap-limit-mib", "240"],
    ["--heap-spike-mib", "400"],
])
def test_invalid_memory_budgets_fail_before_starting_docker(monkeypatch, flags):
    runner = load_runner()
    monkeypatch.setattr('sys.argv', ['demo'] + flags)
    def must_not_run(*args, **kwargs):
        pytest.fail('Invalid limits must not start infrastructure')
    monkeypatch.setattr(runner.subprocess, 'run', must_not_run)
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
