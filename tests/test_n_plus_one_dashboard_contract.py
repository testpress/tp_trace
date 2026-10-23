"""Dashboard contracts for the repeated-query signal."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "grafana/dashboards"


def _load(relative):
    return json.loads((DASHBOARDS / relative).read_text())


def _all_targets(dashboard):
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            yield panel, target


def test_n_plus_one_detail_has_live_metric_and_tempo_evidence():
    dashboard = _load("Supporting Dashboards/n_plus_one_detail.json")
    targets = list(_all_targets(dashboard))
    prometheus = [t[1] for t in targets if "apm_repeated_query_affected_requests_total" in t[1].get("expr", "")]
    tempo = [t[1] for t in targets if t[1].get("queryType") == "traceql"]

    assert prometheus
    assert tempo
    trace_query = tempo[0]["query"]
    for predicate in (
        'resource.project_name =~ "${project:regex}"', 'resource.cluster_name =~ "${cluster:regex}"', "span.repeated_query.detected = true",
        'span.http.method =~ "${method:regex}"', 'span.http.route_template = "${route:raw}"', "span.n_plus_one.dominant_query",
        "span.n_plus_one.dominant_count", "span.n_plus_one.dominant_duration_ms",
        "span.repeated_query.candidate_queries", "span.repeated_query.candidate_counts",
        "span.repeated_query.candidate_duration_ms", "span.repeated_query.candidate_avg_duration_ms",
        "span.repeated_query.omitted_candidate_count", "span.n_plus_one.severity",
        "span.n_plus_one.tracker_overflowed", "span.db.query_count",
        "span.http.request.duration_ms",
    ):
        assert predicate in trace_query
    assert "${method:regex}" in prometheus[0]["expr"]
    assert "${route:raw}" in prometheus[0]["expr"]
    # Grafana's regex formatter escapes route slashes as \/; PromQL rejects
    # that escape inside a quoted matcher.
    assert all('http_route="${route:raw}"' in t["expr"] for t in prometheus)
    assert all('${route:regex}' not in t["expr"] for t in prometheus)
    assert "Example Affected Traces" in next(p["title"] for p, _ in targets if p.get("title", "").startswith("Example Affected Traces"))
    detail_panel = dashboard["panels"][0]
    content = detail_panel["options"]["content"]
    assert "Affected request totals and affected-request rate are queried live" in content
    assert "m-affected-requests" not in content
    assert "Fetching query fingerprint" not in content
    assert "Searching Tempo" not in content
    assert "span.http.request.duration_ms" in trace_query
    assert "trace:duration" not in trace_query
    explore = next(link["url"] for link in dashboard["links"] if link["title"] == "Open in Tempo Explore")
    for variable in ("project", "cluster", "method"):
        assert f"${{{variable}:regex}}" in explore
        assert f"%5C%22${{{variable}:regex}}%5C%22" in explore
    assert "${route:raw}" in explore
    assert "span.http.route_template%20%3D%20" in explore
    assert 'doublequote' not in explore
    assert "span.repeated_query.detected%20%3D%20true" in explore
    trace_panel = next(p for p, t in targets if t.get("queryType") == "traceql")
    assert "arrays share an index" in trace_panel["description"]
    trace_links = [prop["value"] for override in trace_panel["fieldConfig"]["overrides"]
                   for prop in override["properties"] if prop["id"] == "links"]
    assert trace_links and "${__value.raw}" in trace_links[0][0]["url"]
    metric_panel = dashboard["panels"][1]
    assert metric_panel["transformations"][0]["options"]["byField"] == "n_plus_one_operation"
    assert metric_panel["fieldConfig"]["overrides"][0]["matcher"]["options"] == "Affected Request Rate %"
    rate_query = next(t["expr"] for t in metric_panel["targets"] if t["refId"] == "B")
    assert 'calls_total{metric_source="tp_trace_sdk"' in rate_query
    assert "apm_calls_total" not in rate_query


def test_operational_issue_link_passes_method_and_route_separately():
    dashboard = _load("Supporting Dashboards/operational_issues.json")
    for panel in dashboard["panels"]:
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


def test_n_plus_one_targets_use_primary_metric_and_service_overview_has_rate():
    dashboards = [
        _load("Supporting Dashboards/operational_issues.json"),
        _load("service_catalog.json"),
    ]
    n1_targets = [t for d in dashboards for _, t in _all_targets(d)
                  if "apm_repeated_query_affected_requests_total" in t.get("expr", "")]
    assert n1_targets
    assert all("apm_n_plus_one_total" not in t["expr"] for t in n1_targets)

    overview = _load("Supporting Dashboards/service_overview.json")
    ops = next(p for p in overview["panels"] if p.get("title") == "Operations Summary")
    rate_targets = [t for t in ops["targets"] if t.get("refId") == "H"]
    assert rate_targets
    assert "apm_repeated_query_affected_requests_total" in rate_targets[0]["expr"]
    assert 'calls_total{metric_source="tp_trace_sdk"' in rate_targets[0]["expr"]
    assert "apm_calls_total" not in rate_targets[0]["expr"]
    assert "100 *" in rate_targets[0]["expr"]
