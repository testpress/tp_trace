"""Host-to-collector-to-storage smoke test; uses only the Python standard library."""

import json
import os
from pathlib import Path
import socket
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request(url, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(req, timeout=10) as response:
        body = response.read()
    try:
        return json.loads(body)
    except ValueError:
        return body.decode()


def eventually(label, check):
    deadline = time.monotonic() + 90
    last = None
    while time.monotonic() < deadline:
        try:
            if check():
                print("PASS", label, flush=True)
                return
        except (HTTPError, URLError, OSError) as exc:
            last = str(exc)
        time.sleep(2)
    raise RuntimeError("{} failed: {}".format(label, last or "no matching data"))


def main():
    # Match Compose's root .env defaults without requiring python-dotenv.
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    host = os.getenv("SMOKE_HOST", "localhost")
    app = "http://{}:{}".format(host, os.getenv("APP_PORT", "8002"))
    grafana = "http://{}:{}".format(host, os.getenv("GRAFANA_PORT", "3001"))
    collector = "http://{}:{}".format(host, os.getenv("OTLP_HTTP_PORT", "4318"))
    prometheus = "http://{}:9090".format(host)
    tempo = "http://{}:3200".format(host)
    for label, url in [
        ("collector health", "http://{}:13133/".format(host)),
        ("Tempo readiness", tempo + "/ready"),
        ("Prometheus readiness", prometheus + "/-/ready"),
        ("Grafana database", grafana + "/api/health"),
    ]:
        eventually(label, lambda url=url: bool(request(url)))
    with socket.create_connection((host, int(os.getenv("OTLP_GRPC_PORT", "4317"))), timeout=5):
        print("PASS OTLP gRPC port reachable", flush=True)
    with socket.create_connection((host, int(os.getenv("REDIS_PORT", "6379"))), timeout=5) as conn:
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        assert conn.recv(128).startswith(b"+PONG"), "Redis PING failed"
    print("PASS host Redis PING", flush=True)
    minio_port = int(os.getenv("MINIO_PORT", "9002"))
    with socket.create_connection((host, minio_port), timeout=5):
        print("PASS host MinIO port reachable", flush=True)

    def get_app_calls() -> float:
        res = request(prometheus + "/api/v1/query?" + urlencode({"query": 'sum(calls_total{normalized_service="django"})'}))
        results = res.get("data", {}).get("result", [])
        return float(results[0]["value"][1]) if results else 0.0

    baseline_calls = get_app_calls()
    app_trace_id = uuid.uuid4().hex
    headers = {"traceparent": "00-{}-{}-01".format(app_trace_id, uuid.uuid4().hex[:16])}
    for db in ("default", "slave1", "slave2", "slave3"):
        result = request(app + "/api/multi-db/?" + urlencode({"db": db}), headers=headers)
        assert result["database"] == db and result["count"] > 0, result
    assert request(app + "/api/external/", headers=headers)["status_code"] == 200
    request(app + "/api/cache-stats/")
    request(app + "/api/products/cache/", headers=headers)
    s3_result = request(app + "/api/s3-storage/", headers=headers)
    assert s3_result.get("ok") is True and s3_result.get("status") == "success", s3_result
    assert "body" in request(app + "/static/admin/css/base.css")
    print("PASS sample app: four databases, HTTP dependency, cache, S3 storage", flush=True)
    for uid in ("tempo", "_IY366MMk"):
        assert request(grafana + "/api/datasources/uid/" + uid + "/health")["status"] == "OK"
    expected = {
        json.loads(p.read_text())["uid"]
        for p in (Path(__file__).resolve().parents[2] / "grafana/dashboards").rglob("*.json")
        if p.name != "_folder.json"
    }
    actual = {item["uid"] for item in request(grafana + "/api/search?type=dash-db")}
    assert expected <= actual, "Missing dashboards: {}".format(expected - actual)
    print("PASS Grafana datasources and {} dashboards".format(len(expected)), flush=True)
    trace_id = uuid.uuid4().hex
    now = time.time_ns()
    resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "host-smoke"}}]}
    result = request(
        collector + "/v1/traces",
        {
            "resourceSpans": [
                {
                    "resource": resource,
                    "scopeSpans": [
                        {
                            "scope": {"name": "dev-smoke"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": uuid.uuid4().hex[:16],
                                    "name": "host-smoke",
                                    "kind": 2,
                                    "startTimeUnixNano": str(now - 10000000),
                                    "endTimeUnixNano": str(now),
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )
    assert not result.get("partialSuccess"), result
    metric = "dev_smoke_" + trace_id
    smoke_worker = "smoke-" + trace_id
    result = request(
        collector + "/v1/metrics",
        {
            "resourceMetrics": [
                {
                    "resource": resource,
                    "scopeMetrics": [
                        {
                            "scope": {"name": "dev-smoke"},
                            "metrics": [
                                {
                                    "name": metric,
                                    "gauge": {"dataPoints": [{"timeUnixNano": str(now), "asDouble": 42}]},
                                },
                                {
                                    "name": "calls_total",
                                    "sum": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": str(now),
                                                "asInt": 1,
                                                "attributes": [
                                                    {"key": "normalized_service", "value": {"stringValue": "host-smoke"}},
                                                    {"key": "worker_id", "value": {"stringValue": smoke_worker}},
                                                ],
                                            }
                                        ],
                                        "aggregationTemporality": 2,
                                        "isMonotonic": True,
                                    },
                                },
                            ],
                        }
                    ],
                }
            ]
        },
    )
    assert not result.get("partialSuccess"), result
    eventually(
        "host trace stored in Tempo: " + trace_id,
        lambda: bool(request(tempo + "/api/traces/" + trace_id).get("batches")),
    )
    eventually(
        "sample application trace stored in Tempo: " + app_trace_id,
        lambda: bool(request(tempo + "/api/traces/" + app_trace_id).get("batches")),
    )
    eventually(
        "host metric stored in Prometheus",
        lambda: bool(request(prometheus + "/api/v1/query?" + urlencode({"query": metric}))["data"]["result"]),
    )
    eventually(
        "host RED metric stored in Prometheus",
        lambda: bool(
            request(prometheus + "/api/v1/query?" + urlencode({"query": 'calls_total{worker_id="' + smoke_worker + '"}'}))[
                "data"
            ]["result"]
        ),
    )
    eventually(
        "sample app RED metrics increased in Prometheus",
        lambda: get_app_calls() > baseline_calls,
    )


if __name__ == "__main__":
    main()
