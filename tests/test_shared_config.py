"""Ensure local and deployment entry points consume the canonical configs."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _yaml(name):
    return yaml.safe_load((ROOT / name).read_text())


def test_tempo_and_prometheus_mount_shared_files():
    local = _yaml("dev/docker-compose.yml")["services"]
    deployed = _yaml("setup/docker-compose.yml")["services"]
    for service, target, source in (
        (local["tempo"], "/etc/tempo/tempo.yaml", "../config/tempo.yaml"),
        (deployed["tempo"], "/etc/tempo/tempo.yaml", "../config/tempo.yaml"),
        (local["prometheus"], "/etc/prometheus/prometheus.yml", "../config/prometheus.yml"),
        (local["otel-collector"], "/etc/otelcol-contrib/config.yaml", "../config/otel-collector.yaml"),
    ):
        assert source + ":" + target + ":ro" in service["volumes"]
        assert (ROOT / "dev" / source).resolve().is_file()


def test_ansible_copies_same_collector_file_and_local_only_changes_addresses():
    tasks = _yaml("ansible/roles/otel-collector/tasks/configure.yml")
    copies = [task["ansible.builtin.copy"] for task in tasks if "ansible.builtin.copy" in task]
    assert any(copy["src"] == "{{ role_path }}/../../../config/otel-collector.yaml" for copy in copies)
    role = ROOT / "ansible/roles/otel-collector"
    assert (role / "../../../config/otel-collector.yaml").resolve() == ROOT / "config/otel-collector.yaml"
    assert not any("ansible.builtin.template" in task for task in tasks)

    env = _yaml("dev/docker-compose.yml")["services"]["otel-collector"]["environment"]
    allowed = {"GOMEMLIMIT", "GOGC", "OTEL_COLLECTOR_INSTANCE", "OTEL_COLLECTOR_ALIAS"}
    assert all(key in allowed or key.endswith(("_BIND", "_ENDPOINT")) for key in env)
    assert not (ROOT / "dev/otel-collector").exists()
    assert not (ROOT / "dev/tempo").exists()
    assert not (ROOT / "dev/prometheus").exists()
