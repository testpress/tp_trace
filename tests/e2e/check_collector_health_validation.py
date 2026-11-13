"""Exercise Collector role validation locally; requires ansible-playbook on PATH."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
role = root / 'ansible/roles/otel-collector'
cases = [
    ('loopback defaults', {}, True),
    ('missing OTLP sources', {'otel_collector_allowed_hosts_common': [], 'otel_collector_allowed_hosts_extra': []}, False),
    ('missing ingress ports', {'otel_collector_otlp_ingress_ports': []}, False),
    ('unverified remote source', {'otel_collector_health_bind': '0.0.0.0', 'otel_collector_health_allowed_hosts': []}, False),
    ('loopback-only remote source', {'otel_collector_health_bind': '0.0.0.0'}, False),
    ('explicit remote source', {'otel_collector_health_bind': '0.0.0.0', 'otel_collector_health_allowed_hosts': ['192.0.2.10/32']}, True),
    ('disabled firewall', {'otel_collector_firewall_enabled': False, 'otel_collector_allowed_hosts_common': [], 'otel_collector_otlp_ingress_ports': []}, True),
    ('disabled health', {'otel_collector_health_enabled': False, 'otel_collector_health_bind': '0.0.0.0', 'otel_collector_health_allowed_hosts': []}, True),
]
with tempfile.TemporaryDirectory(prefix='tp-health-validation-') as tmp:
    play = Path(tmp) / 'check.yml'
    play.write_text(json.dumps([{'hosts': 'localhost', 'gather_facts': False, 'vars_files': [str(role / 'defaults/main.yml')], 'tasks': [{'ansible.builtin.import_tasks': str(role / 'tasks/validate.yml')}]}]))
    env = dict(os.environ, ANSIBLE_LOCAL_TEMP=tmp)
    for name, overrides, expected in cases:
        result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(play), '-e', json.dumps(overrides)], env=env, text=True, capture_output=True)
        actual = result.returncode == 0
        message = ('firewall variables are incomplete' if name in ['missing OTLP sources', 'missing ingress ports'] else 'verified Kuma probe source')
        if actual != expected or (not expected and message not in result.stdout + result.stderr):
            print(result.stdout, result.stderr)
            raise SystemExit('FAIL: ' + name)
        print('PASS: ' + name + (' accepted' if actual else ' rejected'))
