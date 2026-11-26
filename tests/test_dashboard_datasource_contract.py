"""Guard dashboards against hardcoded Grafana datasource UIDs."""

import json
from pathlib import Path


DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana/dashboards"
PROVISIONED_UID = "_IY366MMk"
DATASOURCE_VARIABLE = "${DS_PROMETHEUS}"


def dashboard_files():
    return sorted(path for path in DASHBOARDS.rglob("*.json") if path.name != "_folder.json")


def hardcoded_uid_paths(value, path=""):
    """Yield JSON paths of scalar values matching the provisioned Prometheus UID."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if child == PROVISIONED_UID:
                yield child_path
            yield from hardcoded_uid_paths(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from hardcoded_uid_paths(child, f"{path}[{index}]")


def _datasource_variables(dashboard):
    if "templating" in dashboard:
        return [
            (f".templating.list[{index}].current.value", item)
            for index, item in enumerate(dashboard["templating"]["list"])
            if item.get("type") == "datasource"
        ]
    return [
        (f".spec.variables[{index}].spec.current.value", {
            "name": item["spec"].get("name"),
            "query": item["spec"].get("pluginId"),
            "current": item["spec"].get("current", {}),
        })
        for index, item in enumerate(dashboard.get("spec", {}).get("variables", []))
        if item.get("kind") == "DatasourceVariable"
    ]


def test_every_dashboard_defines_the_prometheus_datasource_variable():
    for path in dashboard_files():
        dashboard = json.loads(path.read_text())
        variables = [
            item
            for _, item in _datasource_variables(dashboard)
            if item.get("name") == "DS_PROMETHEUS"
        ]
        assert len(variables) == 1, f"{path}: expected one DS_PROMETHEUS datasource variable"
        assert variables[0]["query"] == "prometheus"
        # Variable defaults must stay concrete; a self-reference cannot be resolved.
        assert variables[0]["current"]["value"] == PROVISIONED_UID


def test_dashboards_reference_prometheus_through_the_datasource_variable():
    for path in dashboard_files():
        dashboard = json.loads(path.read_text())
        allowed_defaults = {
            uid_path
            for uid_path, item in _datasource_variables(dashboard)
            if item.get("query") == "prometheus"
        }
        hardcoded = set(hardcoded_uid_paths(dashboard))
        assert hardcoded <= allowed_defaults, f"{path}: hardcoded UID at {sorted(hardcoded - allowed_defaults)}"
        assert DATASOURCE_VARIABLE in path.read_text(), f"{path}: no datasource variable references"
