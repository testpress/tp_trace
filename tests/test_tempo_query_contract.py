"""Preserve operation/error/N+1 results when staging TraceQL filters."""

import json
from pathlib import Path

import pytest


DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana/dashboards/Supporting Dashboards"
POOL = '("${db_pool:raw}" = ".*" || span.db.connection.pool =~ "${db_pool:regex}")'
SERVICE = '(resource.service.name = ${service:doublequote} || span.normalized.service = ${service:doublequote})'
OPERATION = '(name = ${operation:doublequote} || span.normalized.operation = ${operation:doublequote})'
ERROR = '(status = error || span.error = true)'
DETECTED = '(span.repeated_query.detected = true || span.n_plus_one.detected = true)'
RESOURCE = 'resource.project_name =~ "${project:regex}" && resource.cluster_name =~ "${cluster:regex}"'


@pytest.mark.parametrize(
    "filename,limit,stages,selection",
    [
        ("operation_details.json", 100, [RESOURCE, SERVICE, OPERATION, POOL],
         "span.http.status_code, span.url.full, span.url.query, span.db.statement"),
        ("service_errors.json", 50, [RESOURCE, SERVICE, ERROR, POOL], None),
        ("n_plus_one_detail.json", 1,
         [RESOURCE + ' && span.http.method =~ "${method:regex}" && span.http.route_template = "${route:raw}"',
          DETECTED],
         "span.n_plus_one.dominant_query, span.n_plus_one.dominant_count, span.django.view, "
         "span.http.route_template, span.http.method, span.normalized.service"),
    ],
)
def test_staged_search_preserves_fallbacks_fields_and_limits(filename, limit, stages, selection):
    dashboard = json.loads((DASHBOARDS / filename).read_text())
    target = next(
        target for panel in dashboard["panels"] for target in panel.get("targets", [])
        if target.get("queryType") == "traceql" and target["limit"] == limit
    )
    expected = " | ".join("{ " + stage + " }" for stage in stages)
    if selection:
        expected += " | select(" + selection + ")"
    assert target["query"] == expected
    # All stages filter the same surviving spans. Do not substitute spanset
    # &&, which can match predicates on different spans of a trace.
    assert "} && {" not in target["query"]
    assert "||" not in stages[0]
