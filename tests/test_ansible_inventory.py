"""Contracts for Collector firewall chain creation and local inventory isolation."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "ansible"


def test_docker_user_chain_is_created_before_push_rules():
    tasks = yaml.safe_load((ROOT / "roles/otel-collector/tasks/firewall.yml").read_text())
    create = next(t for t in tasks if t["name"] == "Ensure Docker user chain exists")
    assert create["ansible.builtin.iptables"]["chain_management"] is True
    assert create["ansible.builtin.iptables"]["state"] == "present"
    push = next(t for t in tasks if t["name"].endswith("present in Docker user chain"))
    assert tasks.index(create) < tasks.index(push)


def test_local_inventory_uses_loopback_without_host_firewall_changes():
    local = yaml.safe_load((ROOT / "inventories/local/group_vars/app_servers.yml").read_text())
    assert local["otel_collector_otlp_grpc_bind"] == "127.0.0.1"
    assert local["otel_collector_otlp_http_bind"] == "127.0.0.1"
    assert local["otel_collector_firewall_enabled"] is False
    assert local["otel_collector_observability_host"] == ""


def test_production_allowed_hosts_matches_inventory():
    def extract_hosts(data):
        hosts = []
        if isinstance(data, dict):
            if "hosts" in data:
                for name, details in data["hosts"].items():
                    if isinstance(details, dict) and "ansible_host" in details:
                        hosts.append(details["ansible_host"])
            if "children" in data:
                for child in data["children"].values():
                    hosts.extend(extract_hosts(child))
        return hosts

    inventory = yaml.safe_load((ROOT / "inventories/production/hosts.yml").read_text())
    host_ips = set(extract_hosts(inventory["all"]))
    group_vars = yaml.safe_load((ROOT / "inventories/production/group_vars/app_servers.yml").read_text())
    allowed = {item.split("/")[0] for item in group_vars["otel_collector_allowed_hosts_common"]}
    assert allowed == host_ips | {"127.0.0.1"}
    assert "157.180.105.111" not in allowed
    assert "157.180.105.111" not in host_ips
