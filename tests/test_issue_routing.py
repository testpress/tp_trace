"""Execute the dashboard URL helper against service and endpoint issue rows."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('relative', ['service_catalog.json', 'Supporting Dashboards/operational_issues.json'])
def test_issue_investigation_routes(relative):
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to execute Grafana dashboard helpers")
    dashboard = json.loads((ROOT / 'grafana/dashboards' / relative).read_text())
    panel = next(p for p in dashboard['panels'] if p.get('id') == (160 if relative == 'service_catalog.json' else 20))
    script = r"""
const assert = require('node:assert/strict');
const helpers = {};
const context = {
  handlebars: {registerHelper: (name, fn) => helpers[name] = fn},
  grafana: {replaceVariables: s => s === '${server:queryparam}' ? 'var-server=host%3A8000' :
    s === '${__url_time_range}' ? 'from=123&to=456' : 'All'}
};
""" + panel['options'].get('helpers', '') + r"""
assert.equal(typeof helpers.issueUrl, 'function');
for (const service of ['django', 'postgres', 'redis']) {
  const row = {TargetUrl: '/d/generic-service-overview?var-service=' + service + '&',
    project_name: 'test & project', cluster_name: 'prod/east'};
  const url = new URL(helpers.issueUrl(row), 'http://grafana');
  assert.equal(url.pathname, '/d/generic-service-overview');
  assert.equal(url.searchParams.get('var-service'), service);
  assert.equal(url.searchParams.get('var-project'), row.project_name);
  assert.equal(url.searchParams.get('var-cluster'), row.cluster_name);
  assert.equal(url.searchParams.get('var-server'), 'host:8000');
  assert.equal(url.searchParams.get('from'), '123');
  assert.equal(url.searchParams.has('var-operation'), false);
}
for (const target of ['generic-operation-details', 'n-plus-one-detail']) {
  const row = {TargetUrl: '/d/' + target + '?var-service=django&',
    http_method: 'GET', http_route: '/courses/<slug>/?a=1&b=2+#', project_name: 'test', cluster_name: 'dev'};
  const url = new URL(helpers.issueUrl(row), 'http://grafana');
  assert.equal(url.pathname, '/d/' + target);
  if (target === 'generic-operation-details') {
    assert.equal(url.searchParams.get('var-operation'), 'GET ' + row.http_route);
  } else {
    assert.equal(url.searchParams.get('var-method'), 'GET');
    assert.equal(url.searchParams.get('var-route'), row.http_route);
  }
}
"""
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    assert '{{issueUrl this}}' in panel['options']['content']
    latency = next(t['expr'] for t in panel['targets'] if t['refId'] == 'C')
    assert '/d/generic-service-overview?var-service=django&' in latency
