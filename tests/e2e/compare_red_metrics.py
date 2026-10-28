#!/usr/bin/env python3
"""Approximate Locust/Prometheus RED reconciliation, independent of live probes.

Prometheus increases are reset-aware estimates over exported samples. They cannot
prove exact request counts at interval boundaries or recover a worker's unexported
requests. Locust failures and client latency also differ from server observations.
"""

import argparse
import json
import math
import re
import sys
import time
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def query_prometheus(base_url: str, query: str, evaluation_time: Optional[float] = None) -> list:
    """Evaluate a query at one explicit timestamp; API failures are not zero traffic."""
    params = {"query": query}
    if evaluation_time is not None:
        params["time"] = str(evaluation_time)
    url = f"{base_url.rstrip('/')}/api/v1/query?{urlencode(params)}"
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=15) as response:
        data = json.load(response)
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {data.get('error', 'unknown error')}")
    if data.get("warnings"):
        print(f"[!] Prometheus warnings: {data['warnings']}", file=sys.stderr)
    return data.get("data", {}).get("result", [])


def query_locust_stats(base_url: str) -> dict:
    """Fetch a client snapshot without changing Locust or its traffic."""
    url = f"{base_url.rstrip('/')}/stats/requests"
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=10) as response:
        return json.load(response)


def format_table(headers: list, rows: list) -> str:
    """Render a compact ASCII table."""
    widths = [max(len(str(row[i])) for row in [headers] + rows) for i in range(len(headers))]
    border = "+-" + "-+-".join("-" * width for width in widths) + "-+"

    def line(row):
        return "| " + " | ".join(f"{str(value):<{width}}" for value, width in zip(row, widths)) + " |"

    return "\n".join([border, line(headers), border] + [line(row) for row in rows] + [border])


def normalize_route_pattern(route: str) -> str:
    """Normalize parameterized Django and Locust route names."""
    return re.sub(r"<[^>]+>", "<*>", route.strip()).split("?")[0].rstrip("/")


def metric_value(results: list) -> Optional[float]:
    """An absent or non-finite sample is unavailable, not zero."""
    if not results:
        return None
    value = float(results[0]["value"][1])
    return value if math.isfinite(value) else None


def display(value: Optional[float], unit: str = "") -> str:
    return "N/A" if value is None else f"{value:,.2f}{unit}"


def aggregate(stats: dict) -> dict:
    for stat in stats.get("stats", []):
        if stat.get("name") == "Aggregated":
            return stat
    raise RuntimeError("Locust returned no aggregate statistics")


def match_endpoints(locust_stats: list, prom_routes: list) -> list:
    """Show run totals alongside window estimates without claiming a match ratio."""
    routes = {}
    for result in prom_routes:
        labels = result.get("metric", {})
        key = (labels.get("http_method", ""), normalize_route_pattern(labels.get("http_route", "")))
        routes[key] = metric_value([result])
    rows = []
    for stat in locust_stats:
        if stat.get("name") == "Aggregated":
            continue
        method, name = stat.get("method", "").upper(), stat.get("name", "")
        rows.append(
            [
                method,
                name,
                str(stat.get("num_requests", 0)),
                display(routes.get((method, normalize_route_pattern(name)))),
            ]
        )
    return rows


def wait_for_exports(seconds: float) -> None:
    """Allow ingestion to catch up without moving the evaluation timestamp."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def run_benchmark(
    locust_url: str,
    prom_url: str,
    project_name: str,
    duration: int,
    export_interval: float = 60,
    export_wait: Optional[float] = None,
) -> None:
    """Compare client deltas with approximate reset-aware server window increases."""
    if duration < 2 * export_interval:
        raise ValueError("Benchmark duration must cover at least two metric export intervals")
    print(f"[*] Measuring approximately {duration}s; traffic continues unchanged.")
    initial = aggregate(query_locust_stats(locust_url))
    start = time.monotonic()
    wait_for_exports(duration)
    final = aggregate(query_locust_stats(locust_url))
    elapsed = time.monotonic() - start
    evaluation_time = time.time()
    requests = final.get("num_requests", 0) - initial.get("num_requests", 0)
    failures = final.get("num_failures", 0) - initial.get("num_failures", 0)
    if requests < 0 or failures < 0:
        raise RuntimeError("Locust counters reset during the benchmark; repeat with stable client statistics")

    # Waiting helps late ingestion, but cannot recover increments between the
    # last SDK export and the boundary, nor pre-export worker exits.
    delay = export_interval if export_wait is None else export_wait
    print(f"[*] Waiting {delay:g}s for ingestion; the measurement window is already closed.")
    wait_for_exports(delay)
    window = f"{max(1, round(elapsed * 1000))}ms"
    selector = f'project_name={json.dumps(project_name)}, normalized_service="django"'

    def value(expr):
        return metric_value(query_prometheus(prom_url, expr, evaluation_time))

    calls = value(f"sum(increase(calls_total{{{selector}}}[{window}]))")
    server_rps = calls / elapsed if calls is not None else None
    ratio = calls / requests * 100 if calls is not None and requests > 0 else None
    p50 = value(f"histogram_quantile(0.50, sum by (le) (rate(duration_milliseconds_bucket{{{selector}}}[{window}])))")
    p95 = value(f"histogram_quantile(0.95, sum by (le) (rate(duration_milliseconds_bucket{{{selector}}}[{window}])))")
    print(f"APPROXIMATE BENCHMARK ({elapsed:.2f}s, evaluated at {evaluation_time:.3f})")
    print(
        format_table(
            ["Metric", "Locust interval", "Prometheus estimate"],
            [
                ["Requests", str(requests), display(calls)],
                ["Throughput", display(requests / elapsed, " req/s"), display(server_rps, " req/s")],
                ["Client failures", str(failures), "Different server error semantics"],
                ["Server P50", "N/A", display(p50, " ms")],
                ["Server P95", "N/A", display(p95, " ms")],
                ["Server/client count ratio", "N/A", display(ratio, "% (approximate)")],
            ],
        )
    )
    print(
        "Not an exact-count assertion: export boundaries, worker exits, new series, other traffic, "
        "and client/server observation differences affect reconciliation."
    )


def run_comparison(locust_url: str, prom_url: str, project_name: str, window: str = "5m") -> None:
    """Display distinct observation windows without claiming they are synchronized."""
    locust = query_locust_stats(locust_url)
    client = aggregate(locust)
    evaluation_time = time.time()
    selector = f'project_name={json.dumps(project_name)}, normalized_service="django"'

    def value(expr):
        return metric_value(query_prometheus(prom_url, expr, evaluation_time))

    calls = value(f"sum(increase(calls_total{{{selector}}}[{window}]))")
    rps = value(f"sum(rate(calls_total{{{selector}}}[{window}]))")
    errors = value(f'sum(increase(calls_total{{{selector}, error="true"}}[{window}]))')
    print(f"RED METRICS OBSERVATIONS ({project_name}, Prometheus window {window})")
    print("Locust run totals and percentiles cover a different interval; no match percentage is inferred.")
    rows = [
        ["Requests", str(client.get("num_requests", 0)), display(calls)],
        ["RPS", display(client.get("total_rps"), " req/s"), display(rps, " req/s")],
        ["Client failures / server errors", str(client.get("num_failures", 0)), display(errors)],
    ]
    for quantile, key in [
        (0.50, "median_response_time"),
        (0.95, "response_time_percentile_0.95"),
        (0.99, "response_time_percentile_0.99"),
    ]:
        latency = value(
            f"histogram_quantile({quantile}, sum by (le) (rate(duration_milliseconds_bucket{{{selector}}}[{window}])))"
        )
        rows.append([f"P{int(quantile * 100)}", display(client.get(key), " ms"), display(latency, " ms")])
    print(format_table(["Metric", "Locust run", f"Prometheus [{window}] estimate"], rows))
    routes = query_prometheus(
        prom_url,
        f"sum by (http_method, http_route) (increase(calls_total{{{selector}}}[{window}]))",
        evaluation_time,
    )
    rows = match_endpoints(locust.get("stats", []), routes)
    if rows:
        print(format_table(["Method", "Route", "Locust run requests", "Prometheus window estimate"], rows))


def main() -> int:
    parser = argparse.ArgumentParser(description="Approximate Locust/Prometheus RED reconciliation.")
    parser.add_argument("--locust-url", default="http://localhost:8089")
    parser.add_argument("--prom-url", default="http://localhost:9090")
    parser.add_argument("--project", default="testpress")
    parser.add_argument("--window", default="5m", help="Prometheus rate/histogram window")
    parser.add_argument("--benchmark", type=int, default=0, help="Measure client deltas over N seconds")
    parser.add_argument(
        "--export-interval", type=float, default=60, help="SDK metric export interval in seconds (default: 60)"
    )
    parser.add_argument(
        "--export-wait", type=float, help="Ingestion wait after benchmark; defaults to one export interval"
    )
    parser.add_argument("--watch", type=int, default=0, help="Observation refresh interval in seconds")
    args = parser.parse_args()
    if args.benchmark < 0 or args.watch < 0 or not math.isfinite(args.export_interval) or args.export_interval <= 0:
        parser.error("Benchmark/watch must be non-negative and export interval must be positive and finite")
    if args.export_wait is not None and (not math.isfinite(args.export_wait) or args.export_wait < 0):
        parser.error("Export wait must be non-negative and finite")
    try:
        if args.benchmark:
            run_benchmark(
                args.locust_url, args.prom_url, args.project, args.benchmark, args.export_interval, args.export_wait
            )
        else:
            while True:
                run_comparison(args.locust_url, args.prom_url, args.project, args.window)
                if not args.watch:
                    break
                wait_for_exports(args.watch)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"[!] Reconciliation unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
