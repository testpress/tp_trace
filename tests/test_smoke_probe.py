"""The live smoke probe must send and retrieve evidence from the selected stack."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest


_spec = importlib.util.spec_from_file_location("smoke_probe", Path(__file__).parent / "e2e/smoke.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


@pytest.mark.parametrize("override", [False, True])
def test_smoke_routes_ingestion_storage_and_dependencies_to_selected_hosts(monkeypatch, override):
    services = ("APP", "GRAFANA", "COLLECTOR", "PROMETHEUS", "TEMPO", "REDIS", "MINIO")
    monkeypatch.setenv("SMOKE_HOST", "shared-stack")
    hosts = {}
    for service in services:
        name = "SMOKE_{}_HOST".format(service)
        monkeypatch.delenv(name, raising=False)
        hosts[service] = service.lower() + ".dev" if override else "shared-stack"
        if override:
            monkeypatch.setenv(name, hosts[service])
    for name in ("APP_PORT", "GRAFANA_PORT", "OTLP_HTTP_PORT", "OTLP_GRPC_PORT", "REDIS_PORT", "MINIO_PORT"):
        monkeypatch.delenv(name, raising=False)

    urls = []
    calls_queries = []
    dashboards = [
        {"uid": json.loads(path.read_text())["uid"]}
        for path in (Path(__file__).parents[1] / "grafana/dashboards").rglob("*.json")
        if path.name != "_folder.json"
    ]

    def request(url, payload=None, headers=None):
        urls.append(url)
        parsed = urlparse(url)
        if payload is not None:
            return {}
        if parsed.path == "/api/search":
            return dashboards
        if parsed.path.endswith("/health"):
            return {"status": "OK"}
        if parsed.path == "/api/multi-db/":
            return {"database": parse_qs(parsed.query)["db"][0], "count": 1}
        if parsed.path == "/api/external/":
            return {"status_code": 200}
        if parsed.path == "/api/s3-storage/":
            return {"ok": True, "status": "success"}
        if parsed.path.startswith("/static/"):
            return {"body": "css"}
        if parsed.path.startswith("/api/traces/"):
            return {"batches": [{}]}
        if parsed.path == "/api/v1/query":
            if 'normalized_service="django"' in parse_qs(parsed.query)["query"][0]:
                calls_queries.append(url)
            return {"data": {"result": [{"value": [0, str(len(calls_queries))]}]}}
        return {"ready": True}

    monkeypatch.setattr(probe, "request", request)
    addresses = []
    connection = MagicMock()
    connection.__enter__.return_value.recv.return_value = b"+PONG\r\n"

    def connect(address, timeout):
        addresses.append(address)
        return connection

    monkeypatch.setattr(probe.socket, "create_connection", connect)
    probe.main()

    for service, port in (("APP", 8002), ("GRAFANA", 3001), ("COLLECTOR", 4318), ("PROMETHEUS", 9090), ("TEMPO", 3200)):
        targets = [urlparse(url) for url in urls if urlparse(url).port == port]
        assert targets
        assert {target.hostname for target in targets} == {hosts[service]}
    assert "http://{}:13133/".format(hosts["COLLECTOR"]) in urls
    assert addresses == [(hosts["COLLECTOR"], 4317), (hosts["REDIS"], 6379), (hosts["MINIO"], 9002)]
    assert len(calls_queries) == 2
