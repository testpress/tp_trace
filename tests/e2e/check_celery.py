"""Live acceptance probe for Celery integration across Tempo and Prometheus."""

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid


def request(url, payload=None, headers=None, timeout=10):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(req, timeout=timeout) as response:
        body = response.read()
    try:
        return json.loads(body)
    except ValueError:
        return body.decode()


def eventually(label, check, timeout=90, interval=2):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            res = check()
            if res:
                print(f"PASS {label}", flush=True)
                return res
        except (HTTPError, URLError, OSError, AssertionError) as exc:
            last = str(exc)
        time.sleep(interval)
    raise RuntimeError(f"{label} failed after {timeout}s: {last or 'no matching data'}")


def query_prometheus(prometheus_url, query_str):
    url = f"{prometheus_url}/api/v1/query?" + urlencode({"query": query_str})
    res = request(url)
    return res.get("data", {}).get("result", [])


def get_tempo_trace(tempo_url, trace_id):
    try:
        data = request(f"{tempo_url}/api/traces/{trace_id}")
        return data.get("batches", [])
    except Exception:
        return []


def extract_spans_from_tempo_batches(batches):
    spans = []
    for b in batches:
        resource_attrs = {
            a["key"]: a.get("value", {}).get("stringValue", a.get("value", {}).get("intValue", ""))
            for a in b.get("resource", {}).get("attributes", [])
        }
        for ss in b.get("scopeSpans", []):
            scope_name = ss.get("scope", {}).get("name", "")
            for s in ss.get("spans", []):
                span_attrs = {
                    a["key"]: a.get("value", {}).get(
                        "stringValue",
                        a.get("value", {}).get(
                            "boolValue",
                            a.get("value", {}).get("intValue", a.get("value", {}).get("doubleValue", "")),
                        ),
                    )
                    for a in s.get("attributes", [])
                }
                spans.append(
                    {
                        "name": s.get("name"),
                        "traceId": s.get("traceId"),
                        "spanId": s.get("spanId"),
                        "parentSpanId": s.get("parentSpanId"),
                        "kind": s.get("kind"),
                        "status": s.get("status", {}),
                        "attributes": span_attrs,
                        "resourceAttributes": resource_attrs,
                        "scopeName": scope_name,
                        "startTimeUnixNano": int(s.get("startTimeUnixNano", 0)),
                        "endTimeUnixNano": int(s.get("endTimeUnixNano", 0)),
                    }
                )
    return spans


def task_span_pair(spans, task_id):
    """Require exactly one finished producer and consumer for a fresh task."""
    matching = [s for s in spans if s["attributes"].get("celery.task_id") == task_id]
    producers = [s for s in matching if s["name"].lstrip("🟣 ") == "celery.apply_async"]
    consumers = [s for s in matching if s["name"].lstrip("🟣 ") == "celery.run"]
    assert len(producers) <= 1 and len(consumers) <= 1, "Duplicate task spans"
    if not producers or not consumers:
        return None
    producer, consumer = producers[0], consumers[0]
    assert consumer["parentSpanId"] == producer["spanId"], "Consumer parent is not producer"
    assert consumer["traceId"] == producer["traceId"]
    return producer, consumer


def counter_snapshot(prometheus, queries):
    """Keep each label set separate to detect counter resets between samples."""
    snapshot = {}
    for name, query in queries.items():
        series = query_prometheus(prometheus, query)
        snapshot[name] = {tuple(sorted(item["metric"].items())): float(item["value"][1]) for item in series}
    return snapshot


def counter_increments(before, after):
    increments = {}
    for name, series in after.items():
        old = before[name]
        if not old.keys() <= series.keys():
            raise RuntimeError("Metric series disappeared; rerun probe with a stable collector")
        changes = [value - old.get(labels, 0) for labels, value in series.items()]
        if any(change < 0 for change in changes):
            raise RuntimeError("Counter reset during probe; rerun after collector stabilizes")
        increments[name] = sum(changes)
    return increments


def check_metric_increments(before, after, task_count):
    delta = counter_increments(before, after)
    assert delta["producer"] >= task_count, "No fresh publication increments"
    assert delta["success"] >= task_count - 1, "No fresh successful execution increments"
    assert delta["error"] >= 1, "No fresh failed execution increment"
    assert delta["count"] >= task_count, "No fresh duration observations"
    assert delta["inf"] == delta["count"], "Histogram +Inf does not match count"
    assert delta["finite"] >= task_count, "No fresh observations in finite 5s bucket"
    assert delta["tiny"] < delta["count"], "All durations below 5ms; check units"
    mean_ms = delta["sum"] / delta["count"]
    assert 400 <= mean_ms <= 3000, f"Unexpected mean task duration: {mean_ms}ms"
    return True


def worker_processes_for_tasks(logs, task_ids):
    """Use Celery completion records as process evidence without adding metric labels."""
    processes = {}
    for line in logs.splitlines():
        worker = re.search(r"INFO/(ForkPoolWorker-\d+)\]", line)
        task = re.search(r"Task sample_app\.tasks\.demo_task\[([^]]+)\] succeeded", line)
        if worker and task and task.group(1) in task_ids:
            processes[task.group(1)] = worker.group(1)
    return processes


def worker_logs(since):
    result = subprocess.run(
        ["docker", "compose", "logs", "--no-color", "--since", since, "celery-worker"],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError("Cannot read celery-worker logs; run probe with local Docker access")
    return result.stdout + result.stderr


def main():
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

    host = os.getenv("SMOKE_HOST", "localhost")
    app = f"http://{host}:{os.getenv('APP_PORT', '8002')}"
    tempo = f"http://{host}:3200"
    prometheus = f"http://{host}:9090"

    print("Verifying stack readiness...", flush=True)
    eventually("Tempo readiness", lambda: bool(request(f"{tempo}/ready")))
    eventually("Prometheus readiness", lambda: bool(request(f"{prometheus}/-/ready")))
    eventually("Sample app readiness", lambda: bool(request(f"{app}/api/")))

    # Match the sample's real dashboard resource/instance/All-institute filters.
    project = json.dumps(os.getenv("CELERY_PROBE_PROJECT", "otel-sample"))
    cluster = json.dumps(os.getenv("CELERY_PROBE_CLUSTER", "Dummy"))
    common = (
        f'project_name={project},cluster_name={cluster},instance!="",'
        'institute_subdomain=~".*",normalized_service="celery"'
    )
    consumer = common + ',span_kind="SPAN_KIND_CONSUMER",normalized_operation="celery.run.sample_app.tasks.demo_task"'
    queries = {
        "producer": "apm_calls_total{"
        + common
        + ',span_kind="SPAN_KIND_PRODUCER",normalized_operation="celery.apply_async.sample_app.tasks.demo_task"}',
        "success": "apm_calls_total{" + consumer + ',error="false"}',
        "error": "apm_calls_total{" + consumer + ',error="true"}',
        "count": "apm_duration_milliseconds_count{" + consumer + "}",
        "sum": "apm_duration_milliseconds_sum{" + consumer + "}",
        "inf": "apm_duration_milliseconds_bucket{" + consumer + ',le="+Inf"}',
        "finite": "apm_duration_milliseconds_bucket{" + consumer + ',le="5000"}',
        "tiny": "apm_duration_milliseconds_bucket{" + consumer + ',le="5"}',
    }
    baseline = counter_snapshot(prometheus, queries)
    started = datetime.now(timezone.utc).isoformat()

    # Step 1: POST a successful task and a failing task
    print("\n1. Publishing test tasks via POST /api/celery/publish/ ...", flush=True)

    success_trace_id = uuid.uuid4().hex
    success_headers = {
        "traceparent": f"00-{success_trace_id}-{uuid.uuid4().hex[:16]}-01",
    }
    success_resp = request(
        f"{app}/api/celery/publish/",
        payload={"value": 21, "fail": False},
        headers=success_headers,
    )
    assert success_resp.get("task_id"), f"Invalid success response: {success_resp}"
    assert success_resp.get("task_name") == "sample_app.tasks.demo_task"
    success_task_id = success_resp["task_id"]
    print(f"PASS Published successful task {success_task_id} with trace {success_trace_id}")

    fail_trace_id = uuid.uuid4().hex
    fail_headers = {
        "traceparent": f"00-{fail_trace_id}-{uuid.uuid4().hex[:16]}-01",
    }
    fail_resp = request(
        f"{app}/api/celery/publish/",
        payload={"value": 99, "fail": True},
        headers=fail_headers,
    )
    assert fail_resp.get("task_id"), f"Invalid failure response: {fail_resp}"
    fail_task_id = fail_resp["task_id"]
    print(f"PASS Published failing task {fail_task_id} with trace {fail_trace_id}")

    # Step 2: Find their spans in Tempo and verify topologies
    print("\n2. Verifying traces in Tempo...", flush=True)

    def check_success_trace():
        batches = get_tempo_trace(tempo, success_trace_id)
        spans = extract_spans_from_tempo_batches(batches)
        pair = task_span_pair(spans, success_task_id)
        server = next((s for s in spans if s.get("kind") in (2, "SPAN_KIND_SERVER")), None)
        if not pair or server is None:
            return False
        producer, consumer = pair

        # Parent relationships: consumer -> producer -> server request
        assert consumer["parentSpanId"] == producer["spanId"]
        if server:
            # Verify producer descends from the HTTP server span
            parent_map = {s["spanId"]: s.get("parentSpanId") for s in spans if s.get("spanId")}
            curr = producer.get("parentSpanId")
            ancestors = set()
            while curr and curr not in ancestors:
                ancestors.add(curr)
                curr = parent_map.get(curr)
            assert server["spanId"] in ancestors, f"Producer span not descendant of server span {server['spanId']}"

        # Attributes on producer
        pattrs = producer["attributes"]
        assert pattrs.get("normalized.service") == "celery"
        assert pattrs.get("normalized.operation") == "celery.apply_async.sample_app.tasks.demo_task"
        assert pattrs.get("celery.task_name") == "sample_app.tasks.demo_task"
        assert pattrs.get("error") in (False, "false")
        for bad in ("http.route", "http.method", "http.request.method", "http.route_template", "django.route"):
            assert bad not in pattrs, f"Leaked HTTP attribute '{bad}' on producer span"

        # Attributes on consumer
        cattrs = consumer["attributes"]
        assert cattrs.get("normalized.service") == "celery"
        assert cattrs.get("normalized.operation") == "celery.run.sample_app.tasks.demo_task"
        assert cattrs.get("celery.task_name") == "sample_app.tasks.demo_task"
        assert cattrs.get("celery.task_state") == "SUCCESS"
        assert cattrs.get("error") in (False, "false")
        for bad in ("http.route", "http.method", "http.request.method", "http.route_template", "django.route"):
            assert bad not in cattrs, f"Leaked HTTP attribute '{bad}' on consumer span"

        # Duration check around demo sleep (approx 0.5s = 500_000_000 ns)
        dur_ms = (consumer["endTimeUnixNano"] - consumer["startTimeUnixNano"]) / 1_000_000
        assert 400 <= dur_ms <= 3000, f"Unexpected consumer duration: {dur_ms}ms"
        return True

    eventually(f"Successful task trace stored in Tempo: {success_trace_id}", check_success_trace)

    def check_fail_trace():
        batches = get_tempo_trace(tempo, fail_trace_id)
        spans = extract_spans_from_tempo_batches(batches)
        pair = task_span_pair(spans, fail_task_id)
        if not pair:
            return False
        _, consumer = pair

        cattrs = consumer["attributes"]
        assert cattrs.get("normalized.service") == "celery"
        assert cattrs.get("error") in (True, "true"), f"Expected error=True on failed task, got {cattrs.get('error')}"
        assert "ValueError" in str(cattrs.get("error.type")), (
            f"Expected ValueError error.type, got {cattrs.get('error.type')}"
        )
        assert consumer["status"].get("code") in (2, "STATUS_CODE_ERROR", "ERROR")
        return True

    eventually(f"Failing task trace stored in Tempo: {fail_trace_id}", check_fail_trace)

    # Step 3: Prefork multi-child export verification
    print("\n3. Testing concurrent worker prefork execution...", flush=True)
    prefork_tasks = {}
    for i in range(4):
        tid = uuid.uuid4().hex
        headers = {"traceparent": f"00-{tid}-{uuid.uuid4().hex[:16]}-01"}
        response = request(f"{app}/api/celery/publish/", payload={"value": i, "fail": False}, headers=headers)
        assert response.get("task_id"), "No task ID returned for prefork probe"
        prefork_tasks[response["task_id"]] = tid

    def check_prefork():
        for task_id, tid in prefork_tasks.items():
            spans = extract_spans_from_tempo_batches(get_tempo_trace(tempo, tid))
            pair = task_span_pair(spans, task_id)
            if not pair:
                return False
            assert pair[1]["attributes"].get("celery.task_state") == "SUCCESS"
        processes = worker_processes_for_tasks(worker_logs(started), prefork_tasks)
        assert set(processes) == set(prefork_tasks), "Missing completion evidence for probe tasks"
        assert len(set(processes.values())) >= 2, "No evidence that two prefork children executed probe tasks"
        return True

    eventually("Fresh task exports from both prefork children, without duplicates", check_prefork)

    print("\n4. Verifying fresh RED metrics with dashboard selectors...", flush=True)
    eventually(
        "Fresh calls/error/duration increments and millisecond bucket units",
        lambda: check_metric_increments(baseline, counter_snapshot(prometheus, queries), 6),
    )

    # Step 6: Verify Celery spans do NOT enter Django SERVICE series
    print("\n5. Verifying isolation: Celery spans do not contaminate Django SERVICE series...", flush=True)
    res_django_celery = query_prometheus(
        prometheus,
        'apm_calls_total{normalized_service="django",normalized_operation=~"^celery.*"}',
    )
    assert len(res_django_celery) == 0, f"Found Celery operations under django service: {res_django_celery}"
    print("PASS No Celery operations recorded under normalized_service='django'")

    print("\nALL CELERY LIVE ACCEPTANCE CHECKS PASSED!", flush=True)


if __name__ == "__main__":
    main()
