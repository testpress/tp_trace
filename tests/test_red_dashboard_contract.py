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
    expr = expr.replace("${operation:doublequote}", json.dumps("GET /test/"))
    return re.sub(r"\$\{[^}]+\}", ".*", expr).replace("$__rate_interval", "5m").replace("$__range", "1h")


def promtool():
    executable = shutil.which("promtool")
    if not executable:
        pytest.skip("promtool is required for offline PromQL evaluation")
    return executable


def collector_fixture(series):
    """Supply Collector identity/kind labels on synthetic RED populations."""
    result = []
    for item in series:
        item = dict(item)
        name = item["series"]
        if name.startswith(("apm_calls_total{", "apm_duration_milliseconds")):
            labels = []
            if 'instance=' not in name:
                labels.append('instance="collector"')
            if 'normalized_service="django"' in name and 'span_kind=' not in name:
                labels.append('span_kind="SPAN_KIND_SERVER"')
            if labels:
                name = name.replace("{", "{" + ",".join(labels) + ",", 1)
        item["series"] = name
        result.append(item)
    return result


def evaluate(tmp_path, expr, series, expected, interval="1m"):
    fixture = tmp_path / "queries.yml"
    fixture.write_text(
        yaml.safe_dump(
            {
                "rule_files": [],
                "evaluation_interval": "1m",
                "tests": [
                    {
                        "interval": interval,
                        "input_series": collector_fixture(series),
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
    for path in DASHBOARDS.rglob("*.json"):
        filename = str(path.relative_to(DASHBOARDS))
        dashboard = json.loads((DASHBOARDS / filename).read_text())
        for item in dashboard.get("panels", []):
            for target in item.get("targets", []):
                if "apm_calls_total" in target.get("expr", "") or "apm_duration_milliseconds" in target.get("expr", ""):
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
def test_collector_errors_ignore_obsolete_native_population(tmp_path, native_errors):
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
    evaluate(tmp_path, expression(FILES[1], 11, "A"), series, [{"labels": "{}", "value": 1}])


def test_collector_error_ratio_without_native_metrics(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER",http_route="/test/",http_method="GET",instance="collector",error="true"'
    evaluate(
        tmp_path,
        expression(FILES[1], 11, "A"),
        [
            {"series": "apm_calls_total{" + labels + "}", "values": "0+10x31"},
        ],
        [{"labels": "{}", "value": 1}],
    )


def test_catalog_uses_collector_errors_despite_old_native_requests(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django",span_kind="SPAN_KIND_SERVER"'
    expr = expression(FILES[0], 5, "B")
    evaluate(
        tmp_path,
        f"count(({expr}) > 0) or vector(0)",
        [
            {"series": "calls_total{" + labels + ',worker_id="host:1",error="false"}', "values": "0+100x31"},
            {"series": "apm_calls_total{" + labels + ',instance="collector",error="true"}', "values": "0+10x31"},
        ],
        [{"labels": "{}", "value": 1}],
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
            {"series": "apm_duration_milliseconds_bucket{" + native + ',le="' + boundary + '"}', "values": "0+10000x31"}
        )
    series.append({"series": "apm_duration_milliseconds_sum{" + native + "}", "values": "0+10000000x31"})
    evaluate(tmp_path, expression(FILES[1], 12, "A"), series, [{"labels": "{}", "value": 1}])


def test_collector_dependency_latency_warning(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",db_system="postgresql"'
    series = [
        {"series": "apm_duration_milliseconds_bucket{" + labels + ',le="100"}', "values": "0+1x25 25+0x5"},
        {"series": "apm_duration_milliseconds_bucket{" + labels + ',le="1000"}', "values": "0+1x31"},
        {"series": "apm_duration_milliseconds_bucket{" + labels + ',le="+Inf"}', "values": "0+1x31"},
        {"series": "apm_duration_milliseconds_sum{" + labels + "}", "values": "0+100x31"},
    ]
    evaluate(tmp_path, expression(FILES[1], 12, "A"), series, [{"labels": "{}", "value": 1}])


def test_dependency_chart_reports_all_span_latency_instead_of_request_share():
    item = panel(FILES[0], 10)
    assert item["fieldConfig"]["defaults"]["unit"] == "ms"
    assert item["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"] == "none"
    assert "All Recorded Spans" in item["title"]
    for target in item["targets"]:
        assert "histogram_quantile(0.95" in target["expr"]
        assert "apm_duration_milliseconds_bucket" in target["expr"]
    for filename, panel_id in [(FILES[0], 160), (FILES[1], 20)]:
        assert not any(t["refId"].endswith("_contrib") for t in panel(filename, panel_id)["targets"])


def test_catalog_total_calls_handles_single_sample_and_flat_new_workers(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute_subdomain="demo",normalized_service="django"'
    series = [
        {"series": "apm_calls_total{" + labels + ',instance="collector-1"}', "values": "_x59 1"},
        {"series": "apm_calls_total{" + labels + ',instance="collector-2"}', "values": "_x50 5x10"},
        {"series": "apm_calls_total{" + labels + ',instance="collector-3"}', "values": "20x61"},
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
                        "input_series": collector_fixture(series),
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
        {"series": "apm_calls_total{" + labels + ',instance="restarted"}', "values": "_x10 10 0 2 5x47"},
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
                        "input_series": collector_fixture(series),
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
            "series": "apm_calls_total{"
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
                        "input_series": collector_fixture(series),
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
                        "input_series": collector_fixture([{"series": "apm_calls_total{" + labels + "}", "values": "_x59 1"}]),
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


@pytest.mark.parametrize("values", ["_x28 1 1", "_x28 0 1"])
def test_n_plus_one_counts_sparse_workers_and_excludes_colliding_legacy_stream(tmp_path, values):
    labels = (
        'project_name="test",cluster_name="dev",normalized_service="django",'
        'metric_source="tp_trace_sdk",instance="host",http_method="GET",'
        'http_route=".*"'
    )
    series = []
    for worker in range(12):
        series.append(
            {
                "series": "apm_calls_total{" + labels + ',span_kind="SPAN_KIND_SERVER",worker_id="w%d"}' % worker,
                "values": values,
            }
        )
        if worker < 6:
            series.append(
                {
                    "series": "apm_repeated_query_affected_requests_total{" + labels + ',worker_id="w%d"}' % worker,
                    "values": values,
                }
            )
    # Pre-fix exports from multiple workers collided here; resets manufacture requests.
    series.append(
        {
            "series": "apm_repeated_query_affected_requests_total{" + labels + "}",
            "values": "0 10 1 10 1 10 1 10 1 10 1 10 1 10 1 10 1 10 1 10 1 10 1 10 1 10 1 10 1 10 1",
        }
    )
    dashboard = json.loads((DASHBOARDS / "Supporting Dashboards/n_plus_one_detail.json").read_text())
    for title, expected in [("Total Requests", 12), ("Affected Requests", 6), ("Affected Rate", 50)]:
        item = next(p for p in dashboard["panels"] if p.get("title") == title)
        evaluate(
            tmp_path,
            expression("Supporting Dashboards/n_plus_one_detail.json", item["id"], "A"),
            series,
            [{"labels": "{}", "value": expected}],
        )


@pytest.mark.parametrize(
    "panel_id,ref,expected", [(201, "A", 1 / 60), (201, "B", 1 / 60), (202, "A", 4), (203, "A", 0.05)]
)
def test_operation_graphs_handle_one_minute_exports(tmp_path, panel_id, ref, expected):
    """Grafana's min step must keep rate windows above the SDK export cadence."""
    target = next(
        t for t in panel("Supporting Dashboards/operation_details.json", panel_id)["targets"] if t["refId"] == ref
    )
    # At a short dashboard range Grafana's display interval is 15 seconds.
    # $__rate_interval = max(display interval + min step, 4 * min step).
    min_step = 60 if target.get("interval") == "1m" else 15
    rate_window = "{}s".format(max(15 + min_step, 4 * min_step))
    expr = target["expr"].replace("${operation:doublequote}", '"GET /test/"')
    expr = re.sub(r"\$\{[^}]+\}", ".*", expr)
    expr = expr.replace("$__rate_interval", rate_window).replace("$__interval", "15s")
    labels = (
        'project_name="test",cluster_name="dev",normalized_service="django",'
        'normalized_operation="GET /test/",metric_source="tp_trace_sdk",'
        'instance="collector",worker_id="w1",span_kind="SPAN_KIND_SERVER",'
        'error="true",http_status_code="500"'
    )
    # OTLP exports once per minute; there are no intervening scrape samples.
    values = " ".join(str(i // 4) if i % 4 == 0 else "_" for i in range(121))
    series = [{"series": "apm_calls_total{" + labels + "}", "values": values}]
    for le in ["100", "+Inf"]:
        series.append({"series": "apm_duration_milliseconds_bucket{" + labels + ',le="' + le + '"}', "values": values})
    result_labels = '{http_status_code="500",status_label="HTTP 500"}' if panel_id == 202 else "{}"
    evaluate(
        tmp_path,
        "round((" + expr + "), 0.000001)",
        series,
        [{"labels": result_labels, "value": round(expected, 6)}],
        interval="15s",
    )


@pytest.mark.parametrize("panel_id,expected", [(6, 1.25), (7, 20)])
def test_service_graphs_preserve_recent_error_spike(tmp_path, panel_id, expected):
    target = panel("Supporting Dashboards/service_overview.json", panel_id)["targets"][0]
    min_step = 60 if target.get("interval") == "1m" else 15
    expr = re.sub(r"\$\{[^}]+\}", ".*", target["expr"])
    expr = expr.replace("$__range", "1h").replace("$__rate_interval", str(max(15 + min_step, 4 * min_step)) + "s")
    labels = (
        'project_name="test",cluster_name="dev",normalized_service="django",'
        'normalized_operation="GET /test/",metric_source="tp_trace_sdk",'
        'instance="collector",worker_id="w1",span_kind="SPAN_KIND_SERVER"'
    )
    series = [
        {"series": "apm_calls_total{" + labels + ',error="false"}', "values": "0+60x30"},
        {"series": "apm_calls_total{" + labels + ',error="true"}', "values": "0x27 60x2"},
    ]
    evaluate(tmp_path, "round((" + expr + "), 0.000001)", series, [{"labels": "{}", "value": expected}])


def test_collector_monitoring_breaks_down_components_without_inventing_budgets(tmp_path):
    components = panel("Supporting Dashboards/collector_monitoring.json", 403)
    targets = {t["refId"] for t in components["targets"]}
    assert targets == {"Tracked", "GroupedRate"}
    labels = 'collector_alias="c1",service_name="testpress",project_name="testpress",cluster_name="prod"'
    series = [
        {"series": 'apm_calls_total{' + labels + ',normalized_service="django",normalized_operation="GET /courses",error="false"}', "values": "0+60x31"},
        {"series": 'apm_calls_total{' + labels + ',normalized_service="django",normalized_operation="GET /courses",error="true"}', "values": "0+60x31"},
        {"series": 'apm_calls_total{' + labels + ',normalized_service="postgres",normalized_operation="SELECT courses"}', "values": "0+60x31"},
        # Real Collector overflow loses the component dimensions; never
        # attribute it to Django or PostgreSQL using a fabricated label.
        {"series": 'apm_calls_total{' + labels + ',otel_metric_overflow="true"}', "values": "0+120x31"},
        {"series": 'apm_calls_total{collector_alias="c2",service_name="testpress",project_name="testpress",cluster_name="prod",normalized_service="django"}', "values": "0+60x31"},
        {"series": 'apm_cardinality_grouping_total{' + labels + ',normalized_service="django",scope="repeated_queries",cause="repeated_query_series_limit"}', "values": "0+60x31"},
    ]
    def result(collector, service, value):
        return {"labels": '{cluster_name="prod",collector_alias="' + collector + '",normalized_service="' + service + '",project_name="testpress",service_name="testpress"}', "value": value}

    evaluate(tmp_path, expression("Supporting Dashboards/collector_monitoring.json", 403, "Tracked"), series, [
        result("c1", "django", 2), result("c1", "postgres", 1), result("c2", "django", 1),
    ])
    evaluate(tmp_path, expression("Supporting Dashboards/collector_monitoring.json", 403, "GroupedRate"), series, [
        result("c1", "django", 1), result("c1", "postgres", 0), result("c2", "django", 0),
    ])


def test_collector_overflow_summary_preserves_unattributed_overflow(tmp_path):
    summary = panel("Supporting Dashboards/collector_monitoring.json", 404)
    assert "normalized_service" not in next(t["expr"] for t in summary["targets"] if t["refId"] == "CollectorOverflow")
    series = [{
        "series": 'apm_calls_total{collector_alias="c1",service_name="testpress",project_name="testpress",cluster_name="prod",otel_metric_overflow="true"}',
        "values": "0+120x31",
    }]
    labels = '{cluster_name="prod",collector_alias="c1",project_name="testpress",service_name="testpress"}'
    evaluate(tmp_path, expression("Supporting Dashboards/collector_monitoring.json", 404, "CollectorOverflow"), series, [{"labels": labels, "value": 2}])
    evaluate(tmp_path, expression("Supporting Dashboards/collector_monitoring.json", 404, "Limit"), series, [{"labels": labels, "value": 5000}])
    assert "UsagePct" not in {t["refId"] for t in summary["targets"]}


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



def test_catalog_keeps_zero_error_rows_for_collector_only_services(tmp_path):
    labels = 'project_name="test",cluster_name="dev",institute="demo",instance="collector",normalized_service="postgres",db_system="postgresql",db_instance="defaultdb",error="false",span_kind="SPAN_KIND_CLIENT"'
    evaluate(
        tmp_path, expression(FILES[0], 5, "B"),
        [{"series": "apm_calls_total{" + labels + "}", "values": "0+10x31"}],
        [{"labels": '{Service="defaultdb"}', "value": 0}],
    )
