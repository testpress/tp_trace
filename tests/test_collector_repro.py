"""Contracts for the local opt-in collector swap/OOM reproduction setup."""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPRO_SCRIPT = ROOT / "tests" / "e2e" / "repro_collector_swap.sh"


def test_repro_collector_service_is_opt_in_and_mirrors_monday_limits():
    compose = yaml.safe_load((ROOT / "dev" / "docker-compose.yml").read_text())
    services = compose["services"]
    assert "otel-collector-repro" in services

    repro = services["otel-collector-repro"]
    assert repro["profiles"] == ["repro"]
    assert repro["image"] == services["otel-collector"]["image"]
    assert "../config/otel-collector.yaml:/etc/otelcol-contrib/config.yaml:ro" in repro["volumes"]
    assert repro["cpus"] == "${REPRO_COLLECTOR_CPUS:-0.5}"
    assert repro["mem_limit"] == "${REPRO_COLLECTOR_MEMORY_LIMIT:-512m}"
    assert repro["memswap_limit"] == "${REPRO_COLLECTOR_MEMSWAP_LIMIT:-1024m}"

    env = repro["environment"]
    assert env["GOMEMLIMIT"] == "${REPRO_COLLECTOR_GOMEMLIMIT:-380MiB}"
    assert env["OTEL_COLLECTOR_MEMORY_LIMIT_MIB"] == "${REPRO_COLLECTOR_MEMORY_LIMIT_MIB:-400}"
    assert env["OTEL_COLLECTOR_MEMORY_SPIKE_LIMIT_MIB"] == "${REPRO_COLLECTOR_MEMORY_SPIKE_LIMIT_MIB:-80}"
    assert env["OTEL_COLLECTOR_ALIAS"] != services["otel-collector"]["environment"]["OTEL_COLLECTOR_ALIAS"]

    default_ports = set(services["otel-collector"]["ports"])
    repro_ports = set(repro["ports"])
    assert default_ports.isdisjoint(repro_ports)

    # Fleet collectors for multi-collector load
    assert "otel-collector-repro-c1" in services
    assert "otel-collector-repro-c2" in services
    c1 = services["otel-collector-repro-c1"]
    c2 = services["otel-collector-repro-c2"]
    assert c1["profiles"] == ["repro"]
    assert c2["profiles"] == ["repro"]
    c1_ports = set(c1["ports"])
    c2_ports = set(c2["ports"])
    assert repro_ports.isdisjoint(c1_ports)
    assert repro_ports.isdisjoint(c2_ports)
    assert c1_ports.isdisjoint(c2_ports)


def test_repro_script_syntax_and_finite_bound_validation():
    assert REPRO_SCRIPT.is_file()
    syntax = subprocess.run(["bash", "-n", str(REPRO_SCRIPT)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    help_res = subprocess.run(["bash", str(REPRO_SCRIPT), "--help"], capture_output=True, text=True)
    assert help_res.returncode == 0
    assert "--mode <swap|oom|fixed>" in help_res.stdout

    inf_res = subprocess.run(["bash", str(REPRO_SCRIPT), "--duration", "inf"], capture_output=True, text=True)
    assert inf_res.returncode != 0

    zero_rate = subprocess.run(["bash", str(REPRO_SCRIPT), "--rate", "0"], capture_output=True, text=True)
    assert zero_rate.returncode != 0

    bad_mode = subprocess.run(["bash", str(REPRO_SCRIPT), "--mode", "invalid"], capture_output=True, text=True)
    assert bad_mode.returncode != 0
