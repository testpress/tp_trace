"""Explicit, isolated old/fixed Collector regression; Python standard library only.

Runs real sample-app requests through one fan-out into both versions. File sinks
prove Collector output, not Tempo ingestion. Never invoked by ordinary pytest.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = "otel/opentelemetry-collector-contrib:"


def collector_config(fixed, expiration, limit):
    """Reduced pipeline with production buckets/units and accelerated budgets."""
    config = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {
            "memory_limiter": {"check_interval": "500ms", "limit_mib": 400, "spike_limit_mib": 80},
            "resource/drop_ephemeral": {"attributes": [
                {"key": key, "action": "delete"} for key in ("service.instance.id", "process.pid", "worker_id")
            ]},
        },
        "connectors": {"spanmetrics": {
            "namespace": "apm", "metrics_flush_interval": "1s",
            "histogram": {"unit": "ms", "explicit": {"buckets": [
                "5ms", "25ms", "100ms", "250ms", "500ms", "1s", "2.5s", "5s",
            ]}},
            "dimensions": [{"name": name, "default": ""} for name in ("normalized.operation", "http.route")],
            "exclude_dimensions": ["span.name"],
        }},
        "exporters": {
            "file/traces": {"path": "/out/traces.json", "flush_interval": "1s"},
            "file/metrics": {"path": "/out/metrics.json", "flush_interval": "1s"},
        },
        "service": {"pipelines": {
            "traces": {"receivers": ["otlp"], "processors": ["memory_limiter", "resource/drop_ephemeral"],
                       "exporters": ["file/traces"]},
            "traces/spanmetrics": {"receivers": ["otlp"], "processors": ["memory_limiter", "resource/drop_ephemeral"],
                                   "exporters": ["spanmetrics"]},
            "metrics": {"receivers": ["spanmetrics"], "exporters": ["file/metrics"]},
        }, "telemetry": {"metrics": {"address": "0.0.0.0:8888"}}},
    }
    if fixed:
        config["connectors"]["spanmetrics"].update(
            metrics_expiration="{}s".format(expiration), series_expiration="{}s".format(expiration),
            aggregation_cardinality_limit=limit,
        )
        config["processors"]["filter/internal"] = {
            "error_mode": "ignore", "traces": {"span": ["kind == SPAN_KIND_INTERNAL"]},
        }
        config["processors"]["resource/sdk"] = {"attributes": [
            {"key": "telemetry.sdk." + key, "action": "delete"} for key in ("version", "language", "name")
        ]}
        config["service"]["pipelines"]["traces/spanmetrics"]["processors"] += ["resource/sdk", "filter/internal"]
        config["service"]["telemetry"]["metrics"] = {"readers": [{"pull": {"exporter": {"prometheus": {
            "host": "0.0.0.0", "port": 8888, "without_type_suffix": False, "without_units": False,
        }}}}]}
    else:
        config["connectors"]["spanmetrics"]["dimensions_cache_size"] = 5000
    return config


def fixture(directory, args):
    services = {}
    for name, version in (("old", "0.96.0"), ("fixed", "0.153.0")):
        out = directory / name
        out.mkdir(mode=0o777)
        out.chmod(0o777)
        config = collector_config(name == "fixed", args.expiration, args.limit)
        config["processors"]["memory_limiter"].update(
            limit_mib=args.heap_limit_mib, spike_limit_mib=args.heap_spike_mib,
        )
        if args.soak_seconds:
            config["exporters"] = {
                "debug": {"verbosity": "basic"},
                "prometheus": {"endpoint": "0.0.0.0:8889", "metric_expiration": "2s",
                               "resource_to_telemetry_conversion": {"enabled": True}},
            }
            config["processors"]["filter/calls"] = {"error_mode": "ignore", "metrics": {"metric": ['name != "apm.calls"']}}
            config["service"]["pipelines"]["traces"]["exporters"] = ["debug"]
            config["service"]["pipelines"]["metrics"].update(processors=["filter/calls"], exporters=["prometheus"])
        path = directory / (name + ".json")
        path.write_text(json.dumps(config))
        services[name] = {
            "image": COLLECTOR + version, "command": ["--config=/config.json"],
            "volumes": [str(path) + ":/config.json:ro", str(out) + ":/out"],
            "ports": ["127.0.0.1::8888"] + (["127.0.0.1::8889"] if args.soak_seconds else []),
            "mem_limit": "{}m".format(args.collector_memory_mib), "cpus": 0.5,
        }
    gateway = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "exporters": {"otlphttp/" + name: {"endpoint": "http://" + name + ":4318",
                       "sending_queue": {"queue_size": 64, "num_consumers": 1},
                       "retry_on_failure": {"initial_interval": "1s", "max_interval": "2s", "max_elapsed_time": "5s"}} for name in ("old", "fixed")},
        "service": {"pipelines": {"traces": {"receivers": ["otlp"], "exporters": ["otlphttp/old", "otlphttp/fixed"]}}},
    }
    path = directory / "gateway.json"
    path.write_text(json.dumps(gateway))
    services["gateway"] = {"image": COLLECTOR + "0.153.0", "command": ["--config=/config.json"],
                           "volumes": [str(path) + ":/config.json:ro"], "depends_on": ["old", "fixed"]}
    for name in ("v1", "v2"):
        services[name] = {
            "image": args.sample_image, "working_dir": "/app", "depends_on": ["gateway"],
            "environment": {"DJANGO_SETTINGS_MODULE": "config.collector_demo", "OTEL_SERVICE_VERSION": "demo-" + name,
                            "TP_TRACE_ENDPOINT": "http://gateway:4318", "OTEL_BSP_SCHEDULE_DELAY": "200",
                            "COLLECTOR_DEMO_SOAK": "true" if args.soak_seconds else "false"},
            "command": ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1"],
            "volumes": [str(ROOT / "sample-app/api") + ":/app/api:ro",
                        str(ROOT / "sample-app/config") + ":/app/config:ro", str(ROOT / "src") + ":/app/src:ro"],
            "ports": ["127.0.0.1::8000"], "mem_limit": "256m", "cpus": 0.5,
        }
    for service in services.values():
        service["logging"] = {"driver": "json-file", "options": {"max-size": "5m", "max-file": "1"}}
    services["gateway"].update(mem_limit="256m", cpus=0.5)
    path = directory / "compose.json"
    path.write_text(json.dumps({"services": services}))
    return path


def documents(path):
    if not path.exists():
        return []
    # The file exporter appends JSON lines. Ignore only an incomplete final write.
    result = []
    for line in path.read_text().splitlines(keepends=True):
        if line.endswith("\n"):
            result.append(json.loads(line))
    return result


def attributes(items):
    return {item["key"]: next(iter(item["value"].values())) for item in items}


def snapshots(path, metric_name="apm.calls"):
    """Return call points per exported snapshot, retaining resource identity."""
    result = []
    for doc in documents(path):
        points = []
        for resource in doc.get("resourceMetrics", []):
            resource_attrs = attributes(resource.get("resource", {}).get("attributes", []))
            for scope in resource.get("scopeMetrics", []):
                for metric in scope.get("metrics", []):
                    if metric["name"] == metric_name:
                        for point in metric.get("sum", metric.get("histogram", {})).get("dataPoints", []):
                            points.append((resource_attrs, attributes(point.get("attributes", [])), point))
        result.append(points)
    return result


def span_records(path):
    result = {}
    for doc in documents(path):
        for resource in doc.get("resourceSpans", []):
            attrs = attributes(resource.get("resource", {}).get("attributes", []))
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    result[(span["traceId"], span["spanId"])] = (attrs, span)
    return result


def eventually(label, check, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            print("PASS " + label, flush=True)
            return
        time.sleep(0.5)
    raise AssertionError(label)


def latest(directory, name):
    values = snapshots(directory / name / "metrics.json")
    return values[-1] if values else []


def operations(points):
    return {attrs.get("normalized.operation") for _, attrs, _ in points
            if attrs.get("span.kind") == "SPAN_KIND_CLIENT"}


def calls(points):
    return {attrs["normalized.operation"]: int(point["asInt"]) for _, attrs, point in points
            if attrs.get("span.kind") == "SPAN_KIND_CLIENT"}


def exercise(directory, urls, args):
    sent_traces = set()

    def hit(app, operation, workload=""):
        trace_id = uuid.uuid4().hex
        suffix = "{}/".format(workload) if workload else ""
        req = Request(urls[app] + "/api/collector-demo/{}{}/".format(suffix, operation), headers={
            "traceparent": "00-{}-{}-01".format(trace_id, uuid.uuid4().hex[:16]),
        })
        with urlopen(req, timeout=10) as response:
            value = json.load(response)
        assert value["trace_id"] == trace_id, "Sample app tracing/propagation is not active"
        sent_traces.add(trace_id)

    for _ in range(2):
        for operation in range(4):
            hit("v1", operation)
    expected = {"demo.operation.{}".format(i) for i in range(4)}
    eventually("baseline CLIENT operations exported by both", lambda: all(
        expected <= operations(latest(directory, name)) and all(value >= 2 for value in calls(latest(directory, name)).values())
        for name in ("old", "fixed")))

    def metric_values(name, metric_name):
        points = snapshots(directory / name / "metrics.json", metric_name)[-1]
        result = {}
        for resource, attrs, point in points:
            if attrs.get("span.kind") not in ("SPAN_KIND_SERVER", "SPAN_KIND_CLIENT"):
                continue
            key = (resource.get("service.name"), resource.get("service.version"),
                   tuple(sorted((k, v) for k, v in attrs.items() if k != "collector.instance.id")))
            result[key] = {k: v for k, v in point.items() if k in ("asInt", "count", "sum", "bucketCounts", "explicitBounds")}
        return result

    def metric_parity():
        for metric in ("apm.calls", "apm.duration"):
            old = metric_values("old", metric)
            fixed = metric_values("fixed", metric)
            if not old or old != fixed:
                return False
            dims = [dict(key[2]) for key in old]
            if {attrs.get("span.kind") for attrs in dims} != {"SPAN_KIND_SERVER", "SPAN_KIND_CLIENT"}:
                return False
            if not expected <= {attrs.get("normalized.operation") for attrs in dims}:
                return False
        return True

    eventually("identical SERVER/CLIENT counters, histogram counts, sums and buckets", metric_parity)
    before = {name: latest(directory, name) for name in ("old", "fixed")}
    assert calls(before["old"]) == calls(before["fixed"]), "CLIENT counters changed"
    assert any(a.get("span.kind") == "SPAN_KIND_INTERNAL" for _, a, _ in before["old"])
    assert not any(a.get("span.kind") == "SPAN_KIND_INTERNAL" for _, a, _ in before["fixed"])
    print("PASS identical CLIENT counts; INTERNAL metric combinations removed", flush=True)

    def keep_alive(app, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            hit(app, 0)
            time.sleep(0.5)

    keep_alive("v1", args.expiration + 3)
    assert operations(latest(directory, "fixed")) == {"demo.operation.0"}, "Stale operations did not expire"
    assert expected <= operations(latest(directory, "old")), "Old baseline did not retain stale operations"
    print("PASS old retains stale operations; fixed expires them while its resource stays active", flush=True)

    keep_alive("v2", args.expiration + 3)
    for name in ("old", "fixed"):
        versions = {r.get("service.version") for r, _, _ in latest(directory, name)}
        assert "demo-v2" in versions, "New resource version missing"
        assert ("demo-v1" in versions) == (name == "old"), "Inactive resource expiry mismatch"
    print("PASS old retains previous deploy; fixed retires it", flush=True)

    def memory_sample(name):
        with urlopen(urls[name] + "/metrics", timeout=10) as response:
            text = response.read().decode()
        return {"rss_bytes": next(float(line.split()[-1]) for line in text.splitlines()
                                   if line.startswith("otelcol_process_memory_rss")),
                "refused_span_attempts": sum(float(line.split()[-1]) for line in text.splitlines()
                                             if line.startswith("otelcol_receiver_refused_spans"))}

    bursts = []
    previous_old_count = 0
    seen = {"demo.operation.0"}
    for burst in range(args.bursts):
        for operation in range(1 + burst, args.operations, args.bursts):
            for workload in ("catalog", "checkout", "search"):
                hit("v2", operation, workload)
            seen.add("demo.operation.{}".format(operation))
        eventually("burst {} operations exported by old".format(burst + 1), lambda: seen <= operations(latest(directory, "old")))
        eventually("overflow is observable in fixed output", lambda: any(
            a.get("otel.metric.overflow") is True for points in snapshots(directory / "fixed/metrics.json")
            for _, a, _ in points))
        # Leave one operation active between bursts so resource-level expiry
        # cannot hide a missing individual-series expiry implementation.
        keep_alive("v2", args.expiration + 3)
        old_count = len(latest(directory, "old"))
        fixed_count = len(latest(directory, "fixed"))
        assert old_count > previous_old_count, "Old retained cardinality did not grow across bursts"
        assert operations(latest(directory, "fixed")) == {"demo.operation.0"}, "Burst operations did not expire"
        previous_old_count = old_count
        bursts.append({"burst": burst + 1, "old_retained_combinations": old_count,
                       "fixed_retained_combinations": fixed_count,
                       "old_memory": memory_sample("old"), "fixed_memory": memory_sample("fixed")})
        print("PASS burst {}: old retains {} combinations, fixed returns to {}".format(
            burst + 1, old_count, fixed_count), flush=True)
    for points in snapshots(directory / "fixed/metrics.json"):
        per_resource = Counter(json.dumps(resource, sort_keys=True) for resource, _, _ in points)
        assert all(count <= args.limit + 1 for count in per_resource.values()), "Per-resource limit exceeded (plus overflow)"
    print("PASS per-resource aggregation ceiling, including overflow", flush=True)

    def trace_parity():
        old = span_records(directory / "old/traces.json")
        fixed = span_records(directory / "fixed/traces.json")
        if old.keys() != fixed.keys() or not sent_traces <= {key[0] for key in fixed}:
            return False
        for trace_id in sent_traces:
            spans = [(r, s) for (tid, _), (r, s) in fixed.items() if tid == trace_id]
            if not any(s.get("kind") == 2 for _, s in spans):
                return False
            if not any(s.get("kind") == 3 and s.get("name") == "demo.client" for _, s in spans):
                return False
            if sum(s.get("kind") == 1 and s.get("name") == "demo.internal" for _, s in spans) != 3:
                return False
            if not all("telemetry.sdk.name" in r for r, _ in spans):
                return False
        return True

    eventually("same span IDs reach both trace sinks; INTERNAL spans and SDK metadata retained", trace_parity)
    print("Waiting for silence expiry ({}s accelerated window)...".format(args.expiration), flush=True)
    time.sleep(args.expiration + 4)
    now = time.time_ns()
    counts = {}
    for name in ("old", "fixed"):
        # Empty snapshots may not be emitted. Old data must have stopped getting
        # fresh timestamps, not merely vanished from Prometheus's history API.
        counts[name] = sum(1 for _, _, p in latest(directory, name)
                           if now - int(p["timeUnixNano"]) < 3_000_000_000)
    assert counts["old"] > 0 and counts["fixed"] == 0, "Silence did not stop fixed metric re-exports"
    print("PASS old still re-exports history; fixed emits no fresh points after expiry", flush=True)
    return {"baseline_combinations": {name: len(points) for name, points in before.items()},
            "fresh_call_points_after_silence": counts, "request_traces": len(sent_traces),
            "exported_spans": len(span_records(directory / "fixed/traces.json")), "bursts": bursts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-image", default="tp-trace-dev-sample-app:latest")
    parser.add_argument("--build", action="store_true", help="Build the sample image explicitly")
    parser.add_argument("--expiration", type=int, default=6, help="Accelerated expiry seconds (6..300; production is 300)")
    parser.add_argument("--limit", type=int, default=16, help="Accelerated per-resource limit (16..128; production is 5000)")
    parser.add_argument("--bursts", type=int, default=3, help="Distinct-operation bursts across three routes (1..8)")
    parser.add_argument("--operations", type=int, default=64, help="Bounded operation corpus (must exceed limit; max 256)")
    parser.add_argument("--soak-seconds", type=int, default=0, help="Observe continuous memory history for 30..3600 seconds")
    parser.add_argument("--sample-interval", type=float, default=2, help="Memory sampling interval (1..30 seconds)")
    parser.add_argument("--soak-rate", type=int, default=20, help="Maximum requests/second during soak bursts (1..100)")
    parser.add_argument("--soak-batch", type=int, default=32, help="New operations per soak burst (4..128)")
    parser.add_argument("--collector-memory-mib", type=int, default=512, help="Equal container limits for old/fixed (192..1024 MiB)")
    parser.add_argument("--heap-limit-mib", type=int, default=400, help="memory_limiter hard heap limit (minimum 48 MiB)")
    parser.add_argument("--heap-spike-mib", type=int, default=80, help="Hard minus soft heap limit; must be positive")
    args = parser.parse_args()
    if not 192 <= args.collector_memory_mib <= 1024:
        parser.error("collector-memory-mib must be 192..1024")
    if not 48 <= args.heap_limit_mib <= args.collector_memory_mib - 64:
        parser.error("heap-limit-mib must be at least 48 and leave at least 64 MiB below the container limit")
    if not 0 < args.heap_spike_mib < args.heap_limit_mib:
        parser.error("heap-spike-mib must be positive and smaller than heap-limit-mib")
    if args.soak_seconds and not 30 <= args.soak_seconds <= 3600:
        parser.error("soak-seconds must be 30..3600")
    if not 1 <= args.sample_interval <= 30 or not 1 <= args.soak_rate <= 100 or not 4 <= args.soak_batch <= 128:
        parser.error("require sample interval 1..30, soak rate 1..100, batch 4..128")
    if not 6 <= args.expiration <= 300 or not 16 <= args.limit <= 128 or not args.limit < args.operations <= 256 or not 1 <= args.bursts <= 8:
        parser.error("require expiry 6..300, limit 16..128, limit < operations <= 256, bursts 1..8")
    minimum_operations = 1 + args.bursts * (args.limit // 3 + 1)
    if args.operations < minimum_operations:
        parser.error("operations must be at least {} for this limit/bursts combination".format(minimum_operations))
    subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=15)
    if args.build:
        subprocess.run(["docker", "build", "-f", str(ROOT / "sample-app/Dockerfile"),
                        "-t", args.sample_image, str(ROOT)], check=True, timeout=600)
    image = subprocess.run(["docker", "image", "inspect", args.sample_image], capture_output=True, timeout=15)
    if image.returncode:
        raise AssertionError("Sample image is unavailable; rerun with --build or choose --sample-image")
    directory = Path(tempfile.mkdtemp(prefix="tp-collector-demo-"))
    compose = fixture(directory, args)
    command = ["docker", "compose", "-p", "tp-demo-" + uuid.uuid4().hex[:10], "-f", str(compose)]
    print("Evidence directory: {}".format(directory), flush=True)
    print("Budgets: {}s expiry, {} combinations/resource; 1s flush. Not a production memory soak.".format(
        args.expiration, args.limit), flush=True)
    print("Equal Collector budgets: container={} MiB, heap soft={} MiB, hard={} MiB".format(
        args.collector_memory_mib, args.heap_limit_mib - args.heap_spike_mib, args.heap_limit_mib), flush=True)
    try:
        subprocess.run(command + ["up", "-d"], check=True, timeout=180)
        urls = {}
        for name, port in (("v1", 8000), ("v2", 8000), ("old", 8888), ("fixed", 8888)):
            bound = subprocess.check_output(command + ["port", name, str(port)], text=True, timeout=15).strip()
            urls[name] = "http://" + bound
            def ready(name=name):
                path = "/api/collector-demo/999/" if name in ("v1", "v2") else "/metrics"
                try:
                    with urlopen(urls[name] + path, timeout=2) as response:
                        return response.status == 200
                except HTTPError as exc:
                    return name in ("v1", "v2") and exc.code == 400
                except OSError:
                    return False
            eventually(name + " listening", ready)
        if args.soak_seconds:
            for name in ("old", "fixed"):
                bound = subprocess.check_output(command + ["port", name, "8889"], text=True, timeout=15).strip()
                urls[name + "_spanmetrics"] = "http://" + bound
            from collector_soak import run_soak
            report = run_soak(directory, urls, args)
        else:
            report = exercise(directory, urls, args)
        report.update({"old_version": "0.96.0", "fixed_version": "0.153.0", "expiry_seconds": args.expiration,
                       "aggregation_limit": args.limit, "memory_soak_verified": False, "tempo_ingestion_verified": False,
                       "collector_memory_mib": args.collector_memory_mib, "heap_limit_mib": args.heap_limit_mib,
                       "heap_soft_limit_mib": args.heap_limit_mib - args.heap_spike_mib})
        # Record memory/refusal measurements without claiming a small test must
        # reproduce the production OOM or prove memory reclamation after GC.
        for name in ("old", "fixed"):
            try:
                with urlopen(urls[name] + "/metrics", timeout=10) as response:
                    metrics = response.read().decode()
            except OSError as exc:
                if not args.soak_seconds:
                    raise
                # A pressure run may deliberately make the old Collector
                # unavailable. Preserve the sampled history and label the gap.
                report[name + "_internal_metrics_error"] = str(exc)
                continue
            (directory / (name + "-internal.prom")).write_text(metrics)
            report[name + "_memory_and_refusals"] = [line for line in metrics.splitlines() if line.startswith((
                "otelcol_process_memory_rss", "otelcol_receiver_refused_spans"))]
        (directory / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
        print("PASS memory history captured." if args.soak_seconds else
              "PASS regression demonstrated. Trace files prove Collector export, not Tempo storage.", flush=True)
    finally:
        try:
            logs = subprocess.run(command + ["logs", "--no-color"], capture_output=True, text=True, timeout=20)
            (directory / "containers.log").write_text(logs.stdout + logs.stderr)
        finally:
            subprocess.run(command + ["down", "--remove-orphans"], check=True, timeout=45)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, URLError, subprocess.SubprocessError) as exc:
        raise SystemExit("FAIL {}".format(exc))
