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
    fixture.write_text(
        yaml.safe_dump(
            {
                "rule_files": [],
                "evaluation_interval": "1m",
                "tests": [
                    {
                        "interval": "1m",
                        "input_series": series,
                        "promql_expr_test": [{"expr": expr, "eval_time": "30m", "exp_samples": expected}],
                    }
                ],
            }
        )
    )
    result = subprocess.run([promtool(), "test", "rules", str(fixture)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_migrated_dashboard_queries_parse(tmp_path):
    rules = []
    for filename in FILES:
        dashboard = json.loads((DASHBOARDS / filename).read_text())
        for item in dashboard["panels"]:
            for target in item.get("targets", []):
                if "expr" in target:
                    rules.append(
                        {
                            "record": "review_query_" + str(len(rules)),
                            "expr": expression(filename, item["id"], target["refId"]),
                        }
                    )
    fixture = tmp_path / "rules.yml"
    fixture.write_text(yaml.safe_dump({"groups": [{"name": "dashboard_queries", "rules": rules}]}))
    result = subprocess.run([promtool(), "check", "rules", str(fixture)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("native_errors", [False, True])
def test_native_errors_do_not_fall_back_to_overlapping_legacy_errors(tmp_path, native_errors):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER",http_route="/test/",http_method="GET"'
    series = [
        {
            "series": "calls_total{" + labels + ',worker_id="host:1",metric_source="tp_trace_sdk",error="false"}',
            "values": "0+100x31",
        },
        {
            "series": "apm_calls_total{" + labels + ',instance="collector",span_name="GET /test/",error="true"}',
            "values": "0+10x31",
        },
    ]
    if native_errors:
        series.append(
            {
                "series": "calls_total{" + labels + ',worker_id="host:1",metric_source="tp_trace_sdk",error="true"}',
                "values": "0+1x31",
            }
        )
    evaluate(tmp_path, expression(FILES[1], 11, "A"), series, [{"labels": "{}", "value": 0}])


def test_legacy_error_ratio_remains_available_without_native_metrics(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER",http_route="/test/",http_method="GET",instance="collector",error="true"'
    evaluate(
        tmp_path,
        expression(FILES[1], 11, "A"),
        [
            {"series": "apm_calls_total{" + labels + "}", "values": "0+10x31"},
        ],
        [{"labels": "{}", "value": 1}],
    )


def test_catalog_zero_error_native_requests_do_not_inherit_historical_errors(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER"'
    expr = expression(FILES[0], 5, "B")
    evaluate(
        tmp_path,
        f"count(({expr}) > 0) or vector(0)",
        [
            {"series": "calls_total{" + labels + ',worker_id="host:1",error="false"}', "values": "0+100x31"},
            {"series": "apm_calls_total{" + labels + ',instance="collector",error="true"}', "values": "0+10x31"},
        ],
        [{"labels": "{}", "value": 0}],
    )


def test_sampled_dependency_latency_warning_survives_unsampled_request_volume(tmp_path):
    labels = (
        'project_name="test",cluster_name="dev",institute_subdomain="demo",instance="collector",db_system="postgresql"'
    )
    series = [
        {"series": "apm_duration_milliseconds_bucket{" + labels + ',le="100"}', "values": "0+1x25 25+0x5"},
        {"series": "apm_duration_milliseconds_bucket{" + labels + ',le="1000"}', "values": "0+1x31"},
        {"series": "apm_duration_milliseconds_bucket{" + labels + ',le="+Inf"}', "values": "0+1x31"},
        {"series": "apm_duration_milliseconds_sum{" + labels + "}", "values": "0+100x31"},
    ]
    native = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER",worker_id="host:1"'
    for boundary in ["1000", "+Inf"]:
        series.append(
            {"series": "duration_milliseconds_bucket{" + native + ',le="' + boundary + '"}', "values": "0+10000x31"}
        )
    series.append({"series": "duration_milliseconds_sum{" + native + "}", "values": "0+10000000x31"})
    evaluate(tmp_path, expression(FILES[1], 12, "A"), series, [{"labels": "{}", "value": 1}])


def test_native_dependency_latency_warning_fires_without_apm_metrics(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",db_system="postgresql"'
    series = [
        {"series": "duration_milliseconds_bucket{" + labels + ',le="100"}', "values": "0+1x25 25+0x5"},
        {"series": "duration_milliseconds_bucket{" + labels + ',le="1000"}', "values": "0+1x31"},
        {"series": "duration_milliseconds_bucket{" + labels + ',le="+Inf"}', "values": "0+1x31"},
        {"series": "duration_milliseconds_sum{" + labels + "}", "values": "0+100x31"},
    ]
    evaluate(tmp_path, expression(FILES[1], 12, "A"), series, [{"labels": "{}", "value": 1}])


def test_dependency_chart_reports_sampled_latency_instead_of_request_share():
    item = panel(FILES[0], 10)
    assert item["fieldConfig"]["defaults"]["unit"] == "ms"
    assert item["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"] == "none"
    assert "Sampled" in item["title"]
    for target in item["targets"]:
        assert "histogram_quantile(0.95" in target["expr"]
        assert "duration_milliseconds_bucket" in target["expr"]
    for filename, panel_id in [(FILES[0], 160), (FILES[1], 20)]:
        assert not any(t["refId"].endswith("_contrib") for t in panel(filename, panel_id)["targets"])


def test_catalog_total_calls_handles_single_sample_and_flat_new_workers(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django"'
    series = [
        {"series": "calls_total{" + labels + ',worker_id="w1"}', "values": "_x59 1"},
        {"series": "calls_total{" + labels + ',worker_id="w2"}', "values": "_x50 5x10"},
        {"series": "calls_total{" + labels + ',worker_id="w3"}', "values": "20x61"},
    ]
    expr = expression(FILES[0], 5, "D")
    fixture = tmp_path / "catalog_total_calls.yml"
    fixture.write_text(
        yaml.safe_dump(
            {
                "rule_files": [],
                "evaluation_interval": "1m",
                "tests": [
                    {
                        "interval": "1m",
                        "input_series": series,
                        "promql_expr_test": [
                            {
                                "expr": expr,
                                "eval_time": "60m",
                                "exp_samples": [{"labels": '{Service="Django"}', "value": 6}],
                            }
                        ],
                    }
                ],
            }
        )
    )
    result = subprocess.run([promtool(), "test", "rules", str(fixture)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_catalog_total_calls_handles_counter_resets(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django"'
    series = [
        {"series": "calls_total{" + labels + ',worker_id="restarted"}', "values": "_x10 10 0 2 5x47"},
    ]
    expr = expression(FILES[0], 5, "D")
    fixture = tmp_path / "catalog_resets.yml"
    fixture.write_text(
        yaml.safe_dump(
            {
                "rule_files": [],
                "evaluation_interval": "1m",
                "tests": [
                    {
                        "interval": "1m",
                        "input_series": series,
                        "promql_expr_test": [
                            {
                                "expr": expr,
                                "eval_time": "60m",
                                "exp_samples": [{"labels": '{Service="Django"}', "value": 5.05}],
                            }
                        ],
                    }
                ],
            }
        )
    )
    result = subprocess.run([promtool(), "test", "rules", str(fixture)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_service_overview_and_n_plus_one_total_calls_handle_new_workers(tmp_path):
    def prep_expr(filename, panel_id, ref):
        expr = next(t["expr"] for t in panel(filename, panel_id)["targets"] if t["refId"] == ref)
        return re.sub(r"\$\{[^}]+\}", ".*", expr).replace("$__rate_interval", "5m").replace("$__range", "1h")

    expr_overview = prep_expr("Supporting Dashboards/service_overview.json", 9, "B")
    expr_n1 = prep_expr("Supporting Dashboards/n_plus_one_detail.json", 10, "A")
    labels = 'normalized_service="django",project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_operation="GET /test/"'
    series = [
        {
            "series": "calls_total{"
            + labels
            + ',worker_id="w1",metric_source="tp_trace_sdk",instance="host:1",http_method="GET",http_route=".*",span_kind="SPAN_KIND_SERVER"}',
            "values": "_x59 3",
        },
    ]
    fixture = tmp_path / "overview_and_n1.yml"
    fixture.write_text(
        yaml.safe_dump(
            {
                "rule_files": [],
                "evaluation_interval": "1m",
                "tests": [
                    {
                        "interval": "1m",
                        "input_series": series,
                        "promql_expr_test": [
                            {
                                "expr": expr_overview,
                                "eval_time": "60m",
                                "exp_samples": [{"labels": '{normalized_operation="GET /test/"}', "value": 3}],
                            },
                            {"expr": expr_n1, "eval_time": "60m", "exp_samples": [{"labels": "{}", "value": 3}]},
                        ],
                    }
                ],
            }
        )
    )
    result = subprocess.run([promtool(), "test", "rules", str(fixture)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_variable_queries_parse_and_resolve_single_samples(tmp_path):
    dashboards = [
        "Supporting Dashboards/service_overview.json",
        "Supporting Dashboards/operation_details.json",
        "Supporting Dashboards/service_errors.json",
    ]
    rules = []
    for rel in dashboards:
        d = json.loads((DASHBOARDS / rel).read_text())
        for v in d.get("templating", {}).get("list", []):
            query = v.get("query", "")
            if isinstance(query, str) and query.startswith("query_result(") and query.endswith(")"):
                inner = query[len("query_result(") : -1]
                expr = re.sub(r"\$\{[^}]+\}", ".*", inner).replace("$__range", "1h").replace("$__rate_interval", "5m")
                rules.append({"record": f"var_query_{len(rules)}", "expr": expr})

    # Validate all variable query rules parse
    rule_fixture = tmp_path / "var_rules.yml"
    rule_fixture.write_text(yaml.safe_dump({"groups": [{"name": "var_queries", "rules": rules}]}))
    res = subprocess.run([promtool(), "check", "rules", str(rule_fixture)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr

    # Test single-sample resolution for service and operation dropdowns across dashboards
    def prep_var_expr(rel, var_name):
        d_json = json.loads((DASHBOARDS / rel).read_text())
        target_var = next(v for v in d_json["templating"]["list"] if v["name"] == var_name)
        return re.sub(r"\$\{[^}]+\}", ".*", target_var["query"][len("query_result(") : -1]).replace("$__range", "1h")

    expr_so_service = prep_var_expr("Supporting Dashboards/service_overview.json", "service")
    expr_od_service = prep_var_expr("Supporting Dashboards/operation_details.json", "service")
    expr_od_operation = prep_var_expr("Supporting Dashboards/operation_details.json", "operation")
    expr_se_service = prep_var_expr("Supporting Dashboards/service_errors.json", "service")

    labels = 'normalized_service="django",project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_operation="GET /test/"'
    eval_fixture = tmp_path / "var_eval.yml"
    eval_fixture.write_text(
        yaml.safe_dump(
            {
                "rule_files": [],
                "evaluation_interval": "1m",
                "tests": [
                    {
                        "interval": "1m",
                        "input_series": [{"series": "calls_total{" + labels + "}", "values": "_x59 1"}],
                        "promql_expr_test": [
                            {
                                "expr": expr_so_service,
                                "eval_time": "60m",
                                "exp_samples": [{"labels": '{normalized_service="django"}', "value": 1}],
                            },
                            {
                                "expr": expr_od_service,
                                "eval_time": "60m",
                                "exp_samples": [{"labels": '{normalized_service="django"}', "value": 1}],
                            },
                            {
                                "expr": expr_od_operation,
                                "eval_time": "60m",
                                "exp_samples": [{"labels": '{normalized_operation="GET /test/"}', "value": 1}],
                            },
                            {
                                "expr": expr_se_service,
                                "eval_time": "60m",
                                "exp_samples": [{"labels": '{normalized_service="django"}', "value": 1}],
                            },
                        ],
                    }
                ],
            }
        )
    )
    res = subprocess.run([promtool(), "test", "rules", str(eval_fixture)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr


def test_all_explore_urls_expand_to_valid_json():
    import urllib.parse

    for p in DASHBOARDS.rglob("*.json"):
        content = p.read_text()
        d = json.loads(content)

        def check_urls(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k == "url" and isinstance(v, str) and "/explore?left=" in v:
                        test_v = v.replace("${__from}", "now-1h").replace("${__to}", "now").replace("${DS_TEMPO}", "tempo")
                        test_v = test_v.replace("${service:raw}", "django").replace("${operation:raw}", "GET /test/")
                        test_v = test_v.replace("${project:regex}", ".*").replace("${cluster:regex}", ".*")
                        test_v = test_v.replace("${method:regex}", "GET").replace("${route:raw}", "/test/")
                        test_v = test_v.replace("${__value.raw}", "1234567890abcdef")
                        qs = urllib.parse.parse_qs(test_v.split("?")[1])
                        left_json = qs["left"][0]
                        parsed = json.loads(left_json)
                        assert isinstance(parsed, list), f"Expected list in {p.name}, got {type(parsed)}"
                    else:
                        check_urls(v)
            elif isinstance(obj, list):
                for item in obj:
                    check_urls(item)

        check_urls(d)


def test_collector_monitoring_cardinality_diagnostics_table(tmp_path):
    """Verify that Collector Monitoring Panel 403 reports the service and rate of cardinality grouping."""
    p403 = next(p for p in json.loads((DASHBOARDS / "Supporting Dashboards/collector_monitoring.json").read_text())["panels"] if p["id"] == 403)
    t_rate = next(t for t in p403["targets"] if t.get("refId") == "GroupedRate")
    expr_rate = re.sub(r"\$\{[^}]+\}", ".*", t_rate["expr"]).replace("$__rate_interval", "5m")

    series = [
        {
            "series": 'apm_cardinality_grouping_total{collector_alias="dev",normalized_service="postgres",scope="operation",cause="operation_limit"}',
            "values": "0+60x31",
        }
    ]

    evaluate(
        tmp_path,
        expr_rate.replace(" or (0 * max by (normalized_service, scope) (apm_cardinality_limit{collector_alias=~\".*\", normalized_service=~\"django|postgres\", scope=~\"route|operation\"} or apm_cardinality_limit_ratio{collector_alias=~\".*\", normalized_service=~\"django|postgres\", scope=~\"route|operation\"}))", "").replace(" or (sum by (normalized_service, scope) (rate(apm_cardinality_grouping_total{collector_alias=~\".*\", normalized_service!~\"all|__other__\"}[2m])))", ""),
        series,
        [{"labels": '{normalized_service="postgres",scope="operation"}', "value": 1}],
    )


def test_collector_monitoring_distinguishes_grouping_from_stream_cap_drops(tmp_path):
    """Verify Collector Monitoring distinguishes SDK grouping from collector drops."""
    p10 = next(p for p in json.loads((DASHBOARDS / "Supporting Dashboards/collector_monitoring.json").read_text())["panels"] if p["id"] == 10)
    t_sdk = next(t for t in p10["targets"] if t.get("refId") == "SDK_Grouping")
    t_col = next(t for t in p10["targets"] if t.get("refId") == "Stream_Drops")
    expr_sdk = re.sub(r"\$\{[^}]+\}", ".*", t_sdk["expr"]).replace("$__rate_interval", "5m")
    expr_col = re.sub(r"\$\{[^}]+\}", ".*", t_col["expr"]).replace("$__rate_interval", "5m")

    series = [
        {
            "series": 'apm_cardinality_grouping_total{collector_alias="dev",net_host_name="127.0.0.1",project_name="test",cluster_name="dev",normalized_service="django",scope="route",cause="route_limit",worker_id="w1"}',
            "values": "0+60x31",
        },
        {
            "series": 'otelcol_deltatocumulative_dropped_datapoints{collector_alias="dev",net_host_name="127.0.0.1",instance="dev-collector"}',
            "values": "0+120x31",
        },
    ]

    # Evaluate SDK grouping expression (60/60 = 1 req/s)
    evaluate(
        tmp_path,
        expr_sdk.replace(" or (0 * max by (net_host_name, collector_alias) ({__name__=~\"otelcol_process_uptime(_seconds)?_total\",collector_alias=~\".*\"}))", ""),
        series,
        [{"labels": '{collector_alias="dev",net_host_name="127.0.0.1"}', "value": 1}],
    )

    # Evaluate Collector stream-cap drop expression (120/60 = 2 req/s)
    evaluate(
        tmp_path,
        expr_col.replace(" or (0 * max by (net_host_name, collector_alias) ({__name__=~\"otelcol_process_uptime(_seconds)?_total\",collector_alias=~\".*\"}))", ""),
        series,
        [{"labels": '{collector_alias="dev",net_host_name="127.0.0.1"}', "value": 2}],
    )


def test_collector_status_reports_up_state(tmp_path):
    """Verify that a collector reports UP status based on healthy telemetry."""
    p10 = next(p for p in json.loads((DASHBOARDS / "Supporting Dashboards/collector_monitoring.json").read_text())["panels"] if p["id"] == 10)
    t_status = next(t for t in p10["targets"] if t.get("refId") == "Status")
    expr_status = re.sub(r"\$\{[^}]+\}", ".*", t_status["expr"]).replace("$__rate_interval", "5m")

    series = [
        {
            "series": 'up{source="otelcol-telemetry",collector_alias="dev",net_host_name="127.0.0.1"}',
            "values": "1x31",
        }
    ]

    evaluate(
        tmp_path,
        expr_status,
        series,
        [{"labels": '{collector_alias="dev",net_host_name="127.0.0.1"}', "value": 1}],
    )


