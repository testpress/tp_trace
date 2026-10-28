"""Evaluate dashboard queries against synthetic sampled/native populations."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana/dashboards"
FILES = ["service_catalog.json", "Supporting Dashboards/operational_issues.json"]


def panel(filename, panel_id):
    return next(p for p in json.loads((DASHBOARDS / filename).read_text())["panels"] if p["id"] == panel_id)


def expression(filename, panel_id, ref):
    expr = next(t["expr"] for t in panel(filename, panel_id)["targets"] if t["refId"] == ref)
    return re.sub(r"\$\{[^}]+\}", ".*", expr).replace("$__rate_interval", "5m").replace("$__range", "1h")


def promtool():
    executable = shutil.which("promtool")
    if not executable:
        pytest.skip("promtool is required for offline PromQL evaluation")
    return executable


def evaluate(tmp_path, expr, series, expected):
    fixture = tmp_path / "queries.yml"
    fixture.write_text(yaml.safe_dump({
        "rule_files": [], "evaluation_interval": "1m", "tests": [{
            "interval": "1m", "input_series": series,
            "promql_expr_test": [{"expr": expr, "eval_time": "30m", "exp_samples": expected}],
        }],
    }))
    result = subprocess.run([promtool(), "test", "rules", str(fixture)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_migrated_dashboard_queries_parse(tmp_path):
    rules = []
    for filename in FILES:
        dashboard = json.loads((DASHBOARDS / filename).read_text())
        for item in dashboard["panels"]:
            for target in item.get("targets", []):
                if "expr" in target:
                    rules.append({"record": "review_query_" + str(len(rules)),
                                  "expr": expression(filename, item["id"], target["refId"])})
    fixture = tmp_path / "rules.yml"
    fixture.write_text(yaml.safe_dump({"groups": [{"name": "dashboard_queries", "rules": rules}]}))
    result = subprocess.run([promtool(), "check", "rules", str(fixture)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("native_errors", [False, True])
def test_native_errors_do_not_fall_back_to_overlapping_legacy_errors(tmp_path, native_errors):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER",http_route="/test/",http_method="GET"'
    series = [
        {"series": 'calls_total{' + labels + ',worker_id="host:1",metric_source="tp_trace_sdk",error="false"}', "values": "0+100x31"},
        {"series": 'apm_calls_total{' + labels + ',instance="collector",span_name="GET /test/",error="true"}', "values": "0+10x31"},
    ]
    if native_errors:
        series.append({"series": 'calls_total{' + labels + ',worker_id="host:1",metric_source="tp_trace_sdk",error="true"}', "values": "0+1x31"})
    evaluate(tmp_path, expression(FILES[1], 11, "A"), series, [{"labels": "{}", "value": 0}])


def test_legacy_error_ratio_remains_available_without_native_metrics(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER",http_route="/test/",http_method="GET",instance="collector",error="true"'
    evaluate(tmp_path, expression(FILES[1], 11, "A"), [
        {"series": 'apm_calls_total{' + labels + '}', "values": "0+10x31"},
    ], [{"labels": "{}", "value": 1}])


def test_catalog_zero_error_native_requests_do_not_inherit_historical_errors(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER"'
    expr = expression(FILES[0], 5, "B")
    evaluate(tmp_path, f'count(({expr}) > 0) or vector(0)', [
        {"series": 'calls_total{' + labels + ',worker_id="host:1",error="false"}', "values": "0+100x31"},
        {"series": 'apm_calls_total{' + labels + ',instance="collector",error="true"}', "values": "0+10x31"},
    ], [{"labels": "{}", "value": 0}])


def test_sampled_dependency_latency_warning_survives_unsampled_request_volume(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",instance="collector",db_system="postgresql"'
    series = [
        {"series": 'apm_duration_milliseconds_bucket{' + labels + ',le="100"}', "values": "0+1x25 25+0x5"},
        {"series": 'apm_duration_milliseconds_bucket{' + labels + ',le="1000"}', "values": "0+1x31"},
        {"series": 'apm_duration_milliseconds_bucket{' + labels + ',le="+Inf"}', "values": "0+1x31"},
        {"series": 'apm_duration_milliseconds_sum{' + labels + '}', "values": "0+100x31"},
    ]
    native = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER",worker_id="host:1"'
    for boundary in ["1000", "+Inf"]:
        series.append({"series": 'duration_milliseconds_bucket{' + native + ',le="' + boundary + '"}', "values": "0+10000x31"})
    series.append({"series": 'duration_milliseconds_sum{' + native + '}', "values": "0+10000000x31"})
    evaluate(tmp_path, expression(FILES[1], 12, "A"), series, [{"labels": "{}", "value": 1}])


def test_dependency_chart_reports_sampled_latency_instead_of_request_share():
    item = panel(FILES[0], 10)
    assert item["fieldConfig"]["defaults"]["unit"] == "ms"
    assert item["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"] == "none"
    assert "Sampled" in item["title"]
    for target in item["targets"]:
        assert "histogram_quantile(0.95" in target["expr"]
        assert "apm_duration_milliseconds_bucket" in target["expr"]
        assert not re.search(r"(?<!apm_)duration_milliseconds", target["expr"])
    for filename, panel_id in [(FILES[0], 160), (FILES[1], 20)]:
        assert not any(t["refId"].endswith("_contrib") for t in panel(filename, panel_id)["targets"])
