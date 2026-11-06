"""Dashboard contracts for the repeated-query signal."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "grafana/dashboards"


def _load(relative):
    return json.loads((DASHBOARDS / relative).read_text())


def _all_targets(dashboard):
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            yield panel, target


def test_n_plus_one_detail_has_live_metric_and_tempo_evidence():
    dashboard = _load("Supporting Dashboards/n_plus_one_detail.json")
    targets = list(_all_targets(dashboard))
    prometheus = [t[1] for t in targets if "apm_repeated_query_affected_requests_total" in t[1].get("expr", "")]
    tempo = [t[1] for t in targets if t[1].get("queryType") == "traceql"]

    assert prometheus
    assert tempo
    affected_panel = next(p for p in dashboard["panels"] if p["title"] == "Affected Requests")
    assert affected_panel["datasource"]["type"] == "prometheus"
    assert "apm_repeated_query_affected_requests_total" in affected_panel["targets"][0]["expr"]

    problem = next(p for p in dashboard["panels"] if p["title"] == "Problem")
    trace_query = problem["targets"][0]["query"]
    assert "span.n_plus_one.dominant_query" in trace_query
    assert "span.n_plus_one.dominant_count" in trace_query
    for predicate in (
        'resource.project_name =~ "${project:regex}"',
        'resource.cluster_name =~ "${cluster:regex}"',
        "span.repeated_query.detected = true",
        'span.http.method =~ "${method:regex}"',
        'span.http.route_template = "${route:raw}"',
    ):
        assert predicate in trace_query

    assert "${method:regex}" in prometheus[0]["expr"]
    assert "${route:raw}" in prometheus[0]["expr"]
    # Grafana regex formatter escapes route slashes as \/; PromQL rejects
    # that escape inside a quoted matcher.
    assert all('http_route="${route:raw}"' in t["expr"] for t in prometheus)
    assert all("${route:regex}" not in t["expr"] for t in prometheus)

    # Ensure rate query calculates percentage safely
    rate_panel = next(p for p in dashboard["panels"] if p.get("title") == "Affected Rate")
    rate_query = rate_panel["targets"][0]["expr"]
    assert 'calls_total{metric_source="tp_trace_sdk"' not in rate_query
    assert "apm_calls_total" in rate_query
    assert "100 *" in rate_query


def test_operational_issue_link_passes_method_and_route_separately():
    dashboard = _load("Supporting Dashboards/operational_issues.json")
    for panel in dashboard.get("panels", []):
        if panel.get("title") != "All Detected Operational Issues":
            continue
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            for prop in override.get("properties", []):
                if prop.get("id") == "links":
                    url = prop["value"][0]["url"]
                    assert "var-method=${__data.fields.http_method:percentencode}" in url
                    assert "var-route=${__data.fields.http_route:percentencode}" in url
                    return
    raise AssertionError("operational issue panel link override not found")


def test_dynamic_issue_cards_pass_route_and_method_independently():
    for relative in ("Supporting Dashboards/operational_issues.json", "service_catalog.json"):
        dashboard = _load(relative)
        panels = [p for p in dashboard["panels"] if p.get("options", {}).get("content")]
        assert panels
        content = "\n".join(p["options"]["content"] for p in panels)
        assert "var-method={{http_method}}&var-route={{http_route}}" in content
        assert "var-operation={{http_method}}%20{{http_route}}" not in content


def test_n_plus_one_targets_use_primary_metric():
    dashboards = [
        _load("Supporting Dashboards/operational_issues.json"),
        _load("service_catalog.json"),
        _load("Supporting Dashboards/n_plus_one_detail.json"),
    ]
    n1_targets = [
        t
        for d in dashboards
        for _, t in _all_targets(d)
        if "apm_repeated_query_affected_requests_total" in t.get("expr", "")
    ]
    assert n1_targets
    assert all("apm_n_plus_one_total" not in t["expr"] for t in n1_targets)


def test_service_catalog_django_queries_use_server_span_kind():
    """Ensure Django targets in service_catalog.json do not match internal spans."""
    dashboard = _load("service_catalog.json")
    for panel, target in _all_targets(dashboard):
        expr = target.get("expr", "")
        if 'normalized_service="django"' in expr:
            assert 'span_kind!="SPAN_KIND_INTERNAL"' not in expr, (
                f"Panel '{panel.get('title')}' target '{target.get('refId')}' uses "
                "span_kind!='SPAN_KIND_INTERNAL' which aggregates internal spans."
            )


def test_service_overview_counts_collector_server_spans():
    """Collector totals include server spans and exclude internal spans."""
    dashboard = _load("Supporting Dashboards/service_overview.json")
    panel = next(p for p in dashboard["panels"] if p.get("title") == "Requests and Errors")
    targets = {target["refId"]: target["expr"] for target in panel["targets"]}

    assert 'error="true"' in targets["B"]
    for expr in targets.values():
        assert 'span_kind!~"SPAN_KIND_INTERNAL"' in expr


def test_service_overview_error_rate_selects_whole_metric_source():
    """Error numerator and denominator must use the same metric source."""
    dashboard = _load("Supporting Dashboards/service_overview.json")
    panel = next(p for p in dashboard["panels"] if p.get("title") == "Error Rate Over Time")
    expr = panel["targets"][0]["expr"]

    assert "rate(calls_total{" not in expr
    assert "rate(apm_calls_total{" in expr
    assert 'span_kind!~"SPAN_KIND_INTERNAL"' in expr
    assert expr.count('error="true"') == 1


def test_service_overview_graphs_use_export_aware_windows_for_sparse_counters():
    """Short refresh intervals must not hide counters exported less frequently."""
    dashboard = _load("Supporting Dashboards/service_overview.json")
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    for title in ("Requests and Errors", "Error Rate Over Time", "Latency Percentiles (P50 / P95 / P99)"):
        expressions = [target.get("expr", "") for target in panels[title]["targets"]]
        assert expressions
        assert all("$__rate_interval" in expr for expr in expressions)
        assert all("$__range" not in expr for expr in expressions)
        assert all(target.get("interval") == "1m" for target in panels[title]["targets"])


def test_collector_spanmetrics_pipeline_and_filters():
    """All recording spans reach RED aggregation before independent storage filtering."""
    import yaml

    cfg = yaml.safe_load((ROOT / "config/otel-collector.yaml").read_text())
    pipelines = cfg["service"]["pipelines"]
    metrics = pipelines["traces/spanmetrics"]
    assert metrics["receivers"] == ["otlp"]
    assert metrics["exporters"] == ["spanmetrics"]
    assert metrics["processors"] == [
        "memory_limiter", "resource/drop_ephemeral", "resource/drop_metric_institute", "filter/spanmetrics_drop_internal",
        "transform/spanmetrics_route", "transform/spanmetrics_sanitize", "batch",
    ]
    assert "filter/drop_unsampled" not in metrics["processors"]
    assert "filter/drop_unsampled" in pipelines["traces"]["processors"]
    assert pipelines["traces"]["exporters"] == ["otlp/tempo"]
    assert "spanmetrics" in pipelines["metrics"]["receivers"]
    connector = cfg["connectors"]["spanmetrics"]
    assert connector["aggregation_temporality"] == "AGGREGATION_TEMPORALITY_CUMULATIVE"
    assert connector["histogram"]["unit"] == "ms"
    assert connector["metrics_expiration"] == connector["series_expiration"] == "5m"
    assert connector["aggregation_cardinality_limit"] == 5000
    assert "span.name" in connector["exclude_dimensions"]
    assert "institute" not in [dimension["name"] for dimension in connector["dimensions"]]
    assert cfg["processors"]["filter/spanmetrics_drop_internal"]["trace_conditions"] == [
        "span.kind == SPAN_KIND_INTERNAL",
    ]


def test_dashboard_links_use_queryparam_not_raw_for_project():
    """Ensure dashboard links pass ${project:queryparam} instead of ${project:raw}."""
    for p in DASHBOARDS.rglob("*.json"):
        content = p.read_text()
        assert "var-project=${project:raw}" not in content, (
            f"{p.name} contains 'var-project=${{project:raw}}' which breaks 'All' selection."
        )


def test_shared_delta_pipeline_preserves_worker_streams():
    import yaml
    import re

    # Read native Collector environment fallbacks; validate actual expansion with
    # the pinned Collector binary in the explicit configuration check.
    text = (ROOT / "config/otel-collector.yaml").read_text()
    text = re.sub(r"\$\{env:[A-Z_]+(?::-([^}]*))?\}", lambda match: match.group(1) or "test", text)
    cfg = yaml.safe_load(text)
    processors = cfg["service"]["pipelines"]["metrics"]["processors"]
    assert processors.index("memory_limiter") < processors.index("deltatocumulative") < processors.index("batch")
    assert cfg["processors"]["deltatocumulative"] == {"max_stale": "5m", "max_streams": 50000}
    # Only resource attributes may be stripped; worker_id on datapoints isolates writers.
    assert not any(name.startswith("transform/") for name in processors)
    # Verify SDK version/language/name attributes are stripped from metric resources
    dropped_keys = [attr["key"] for attr in cfg["processors"]["resource/drop_ephemeral"]["attributes"]]
    for sdk_key in ("telemetry.sdk.version", "telemetry.sdk.language", "telemetry.sdk.name"):
        assert sdk_key in dropped_keys
    for name in ("prometheusremotewrite", "prometheusremotewrite/internal"):
        assert cfg["exporters"][name]["remote_write_queue"] == {
            "enabled": True, "queue_size": 1000, "num_consumers": 1,
        }
    text = re.sub(r"\$\{env:[A-Z_]+(?::-([^}]*))?\}", lambda match: match.group(1) or "test", text)
    cfg = yaml.safe_load(text)
    processors = cfg["service"]["pipelines"]["metrics"]["processors"]
    assert processors.index("memory_limiter") < processors.index("batch")
    # Verify SDK version/language/name attributes are stripped from metric resources
    dropped_keys = [attr["key"] for attr in cfg["processors"]["resource/drop_ephemeral"]["attributes"]]
    for sdk_key in ("telemetry.sdk.version", "telemetry.sdk.language", "telemetry.sdk.name"):
        assert sdk_key in dropped_keys
    for name in ("prometheusremotewrite", "prometheusremotewrite/internal"):
        assert cfg["exporters"][name]["remote_write_queue"] == {
            "enabled": True, "queue_size": 1000, "num_consumers": 1,
        }


def test_n_plus_one_detail_handles_flat_new_series_for_apm_calls(tmp_path):
    """Verify that N+1 Detail Panels 10 and 12 handle flat new route counters on apm_calls_total."""
    import re
    from tests.test_red_dashboard_contract import evaluate

    dashboard = _load("Supporting Dashboards/n_plus_one_detail.json")
    p10 = next(p for p in dashboard["panels"] if p["id"] == 10)
    expr_p10 = re.sub(r"\$\{[^}]+\}", ".*", p10["targets"][0]["expr"]).replace("$__range", "1h")
    expr_p10 = expr_p10.replace('http_route=".*"', 'http_route="/new-route/"')

    p12 = next(p for p in dashboard["panels"] if p["id"] == 12)
    expr_p12 = re.sub(r"\$\{[^}]+\}", ".*", p12["targets"][0]["expr"]).replace("$__range", "1h")
    expr_p12 = expr_p12.replace('http_route=".*"', 'http_route="/new-route/"')

    series = [
        {
            "series": (
                'apm_repeated_query_affected_requests_total{metric_source="tp_trace_sdk",'
                'worker_id="w1",instance="i1",institute="demo",project_name="test",'
                'cluster_name="dev",normalized_service="django",http_method="GET",'
                'http_route="/new-route/"}'
            ),
            "values": "10x30",
        },
        {
            "series": (
                'apm_calls_total{instance="i1",institute="demo",project_name="test",'
                'cluster_name="dev",normalized_service="django",http_method="GET",'
                'http_route="/new-route/",span_kind="SPAN_KIND_SERVER"}'
            ),
            "values": "10x30",
        },
    ]

    evaluate(tmp_path, expr_p10, [series[1]], [{"labels": "{}", "value": 10}])
    evaluate(tmp_path, expr_p12, series, [{"labels": "{}", "value": 100}])


def test_collector_red_queries_do_not_require_removed_sdk_labels_or_scopes():
    """Collector RED remains queryable without legacy SDK RED datapoints."""
    import re

    for path in DASHBOARDS.rglob("*.json"):
        dashboard = json.loads(path.read_text())
        for _, target in _all_targets(dashboard):
            expr = target.get("expr", "")
            for labels in re.findall(r'apm_(?:calls_total|duration_milliseconds\w*)\{([^}]+)\}', expr):
                assert "worker_id" not in labels, path
                assert "metric_source" not in labels, path
            assert not re.search(
                r'apm_cardinality_(?:limit|tracked)\{[^}]*scope\s*(?:=|=~)\s*"(?:route|service|operation)',
                expr,
            ), path



def test_dashboards_only_query_collector_red_metrics():
    import re

    for path in DASHBOARDS.rglob("*.json"):
        text = path.read_text()
        assert not re.search(r"(?<!apm_)\bcalls_total\b|(?<!apm_)\bduration_milliseconds", text), path
        for _, target in _all_targets(json.loads(text)):
            if "apm_calls_total" in target.get("expr", ""):
                assert "span_name" not in target["expr"], path


def test_institute_is_trace_only_and_does_not_filter_metric_queries():
    for path in DASHBOARDS.rglob("*.json"):
        text = path.read_text()
        dashboard = json.loads(text)
        for _, target in _all_targets(dashboard):
            expr = target.get("expr", "")
            assert "institute" not in expr, path
        assert "${institute" not in text, path
        assert "institute" not in {v["name"] for v in dashboard.get("templating", {}).get("list", [])}, path
    cfg = __import__("yaml").safe_load((ROOT / "config/otel-collector.yaml").read_text())
    assert "resource/drop_metric_institute" not in cfg["service"]["pipelines"]["traces"]["processors"]
    assert "resource/drop_metric_institute" in cfg["service"]["pipelines"]["metrics"]["processors"]


def test_celery_dashboard_lists_execution_metrics_and_focuses_selected_span():
    overview = _load("Supporting Dashboards/service_overview.json")
    operations = next(p for p in overview["panels"] if p["id"] == 9)
    for target in operations["targets"]:
        assert 'normalized_service="celery",span_kind="SPAN_KIND_CONSUMER"' in target["expr"]
        assert 'normalized_service!="celery"' in target["expr"]
    details = _load("Supporting Dashboards/operation_details.json")
    panel = next(p for p in details["panels"] if p["id"] == 301)
    operation = next(v for v in details["templating"]["list"] if v["name"] == "operation")
    assert 'normalized_service="celery",span_kind="SPAN_KIND_CONSUMER"' in operation["query"]
    assert "apply_async" not in operation["query"]
    for transform in panel["transformations"]:
        if transform["id"] == "organize":
            assert not transform["options"]["excludeByName"].get("spanID", False)
    assert any(
        override["matcher"] == {"id": "byName", "options": "spanID"}
        and {"id": "custom.hidden", "value": True} in override["properties"]
        for override in panel["fieldConfig"]["overrides"]
    )


def test_detail_route_options_use_positive_affected_counts():
    dashboard = _load("Supporting Dashboards/n_plus_one_detail.json")
    variables = {variable["name"]: variable for variable in dashboard["templating"]["list"]}
    affected = next(panel for panel in dashboard["panels"] if panel["title"] == "Affected Requests")
    expr = affected["targets"][0]["expr"]
    grouped = expr.rsplit(" or vector(0)", 1)[0].replace("sum(", "sum by (http_route) (", 1)
    grouped = grouped.replace('http_route="${route:raw}"', 'http_route!=""')
    assert variables["route"]["query"] == f"query_result({grouped} > 0)"
    assert variables["route"]["refresh"] == 2
    assert "${route:" not in variables["method"]["query"]


def test_operational_issue_endpoint_queries_apply_route_and_method():
    dashboard = _load("Supporting Dashboards/operational_issues.json")
    variables = {variable["name"] for variable in dashboard["templating"]["list"]}
    assert {"route", "method"} <= variables

    for _, target in _all_targets(dashboard):
        expr = target.get("expr", "")
        if "http_route" in expr:
            assert 'http_route=~`${route:regex}`' in expr
            assert 'http_method=~"${method:regex}"' in expr


def test_operational_repeated_query_counts_match_drilldown_window():
    detail = _load("Supporting Dashboards/n_plus_one_detail.json")
    affected = next(panel for panel in detail["panels"] if panel["title"] == "Affected Requests")
    expr = affected["targets"][0]["expr"].rsplit(" or vector(0)", 1)[0]
    grouped = expr.replace("sum(", "sum by (project_name, cluster_name, http_route, http_method) (", 1)
    grouped = grouped.replace('http_route="${route:raw}"', 'http_route=~`${route:regex}`, http_route!=""')

    overview = _load("Supporting Dashboards/operational_issues.json")
    targets = [target for _, target in _all_targets(overview)
               if "apm_repeated_query_affected_requests_total" in target["expr"]]
    assert len(targets) == 2
    assert all(f"{grouped} > 0" in target["expr"] for target in targets)
