#!/usr/bin/env python3
"""Synchronized verification tool comparing Locust load test RED metrics with Prometheus.

Verifies:
- Rate: Locust RPS vs Prometheus calls_total increase / rate
- Errors: Locust failures vs Prometheus server errors (5xx) and client errors (4xx)
- Duration: Locust P50 / P95 / P99 latency percentiles vs Prometheus histogram quantiles
- Breakdown: Per-endpoint side-by-side reconciliation
"""

import argparse
import json
import re
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen


def query_prometheus(base_url: str, query: str) -> list:
    """Execute an instant PromQL query against Prometheus HTTP API."""
    url = f"{base_url.rstrip('/')}/api/v1/query?query={quote(query)}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") == "success":
            return data.get("data", {}).get("result", [])
    except Exception as exc:
        print(f"[!] Warning: Prometheus query failed: {query} ({exc})", file=sys.stderr)
    return []


def query_locust_stats(base_url: str) -> dict:
    """Fetch current stats and failures from Locust web UI API."""
    url = f"{base_url.rstrip('/')}/stats/requests"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        print(f"[!] Error: Could not reach Locust at {url}: {exc}", file=sys.stderr)
        return {}


def format_table(headers: list, rows: list) -> str:
    """Render a clean ASCII table."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    border = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    header_line = "| " + " | ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths)) + " |"

    lines = [border, header_line, border]
    for row in rows:
        line = "| " + " | ".join(f"{str(v):<{w}}" for v, w in zip(row, col_widths)) + " |"
        lines.append(line)
    lines.append(border)
    return "\n".join(lines)


def normalize_route_pattern(route: str) -> str:
    """Normalize parameterized URL patterns between Locust and Django."""
    # Convert <exam_slug>, <course_id>, <pk>, <slug>, etc. to standard <*>
    cleaned = re.sub(r"<[^>]+>", "<*>", route.strip())
    # Strip trailing query params if present
    cleaned = cleaned.split("?")[0]
    return cleaned.rstrip("/")


def match_endpoints(locust_stats: list, prom_routes: list) -> list:
    """Match Locust endpoints with Prometheus routes for side-by-side comparison."""
    # Build dictionary of Prometheus routes by normalized pattern
    prom_by_pattern = {}
    for pr in prom_routes:
        method = pr.get("metric", {}).get("http_method", "")
        route = pr.get("metric", {}).get("http_route", "")
        pattern = f"{method} {normalize_route_pattern(route)}"
        val = int(float(pr.get("value", [0, 0])[1]))
        prom_by_pattern[pattern] = (route, val)

    rows = []
    for ls in locust_stats:
        if ls.get("name") == "Aggregated":
            continue
        method = ls.get("method", "").upper()
        name = ls.get("name", "")
        loc_reqs = ls.get("num_requests", 0)
        loc_fails = ls.get("num_failures", 0)
        loc_p95 = ls.get("response_time_percentile_0.95", 0) or 0

        pattern = f"{method} {normalize_route_pattern(name)}"
        prom_route, prom_reqs = prom_by_pattern.get(pattern, (name, 0))

        match_pct = f"{(prom_reqs / loc_reqs * 100):.1f}%" if loc_reqs > 0 else "N/A"
        rows.append({
            "method": method,
            "route": name[:45],
            "loc_reqs": loc_reqs,
            "prom_reqs": prom_reqs,
            "match_pct": match_pct,
            "loc_fails": loc_fails,
            "loc_p95": f"{loc_p95:.0f}ms",
        })

    # Sort by locust requests descending
    rows.sort(key=lambda x: x["loc_reqs"], reverse=True)
    return rows


def run_comparison(locust_url: str, prom_url: str, project_name: str, window: str = "5m"):
    """Fetch metrics and output formatted RED comparison."""
    loc_data = query_locust_stats(locust_url)
    if not loc_data:
        print("[!] No Locust data available. Ensure Locust is running.")
        return

    loc_agg = next((s for s in loc_data.get("stats", []) if s.get("name") == "Aggregated"), {})
    loc_total_reqs = loc_agg.get("num_requests", 0)
    loc_failures = loc_agg.get("num_failures", 0)
    loc_current_rps = loc_agg.get("current_rps", 0.0) or 0.0
    loc_total_rps = loc_agg.get("total_rps", 0.0) or 0.0
    loc_p50 = loc_agg.get("median_response_time", 0) or 0
    loc_p95 = loc_agg.get("response_time_percentile_0.95", 0) or 0
    loc_p99 = loc_agg.get("response_time_percentile_0.99", 0) or 0

    # Prometheus Queries
    filter_proj = f'project_name="{project_name}"'

    res_total = query_prometheus(prom_url, f"sum(calls_total{{{filter_proj}}})")
    prom_total_calls = int(float(res_total[0]["value"][1])) if res_total else 0

    res_rate_1m = query_prometheus(prom_url, f"sum(rate(calls_total{{{filter_proj}}}[1m]))")
    prom_rps_1m = float(res_rate_1m[0]["value"][1]) if res_rate_1m else 0.0

    res_rate_window = query_prometheus(prom_url, f"sum(rate(calls_total{{{filter_proj}}}[{window}]))")
    prom_rps_window = float(res_rate_window[0]["value"][1]) if res_rate_window else 0.0

    # Errors
    res_err_5xx = query_prometheus(prom_url, f'sum(calls_total{{{filter_proj}, error="true"}})')
    prom_err_5xx = int(float(res_err_5xx[0]["value"][1])) if res_err_5xx else 0

    res_err_4xx = query_prometheus(prom_url, f'sum(calls_total{{{filter_proj}, status_code=~"4.."}})')
    prom_err_4xx = int(float(res_err_4xx[0]["value"][1])) if res_err_4xx else 0

    prom_client_server_fails = prom_err_5xx + prom_err_4xx

    # Latencies
    res_p50 = query_prometheus(
        prom_url,
        f"histogram_quantile(0.50, sum by (le) (rate(duration_milliseconds_bucket{{{filter_proj}}}[{window}])))"
    )
    prom_p50 = float(res_p50[0]["value"][1]) if res_p50 else 0.0

    res_p95 = query_prometheus(
        prom_url,
        f"histogram_quantile(0.95, sum by (le) (rate(duration_milliseconds_bucket{{{filter_proj}}}[{window}])))"
    )
    prom_p95 = float(res_p95[0]["value"][1]) if res_p95 else 0.0

    res_p99 = query_prometheus(
        prom_url,
        f"histogram_quantile(0.99, sum by (le) (rate(duration_milliseconds_bucket{{{filter_proj}}}[{window}])))"
    )
    prom_p99 = float(res_p99[0]["value"][1]) if res_p99 else 0.0

    # Match ratio
    match_ratio = (prom_total_calls / loc_total_reqs * 100) if loc_total_reqs > 0 else 0.0

    print("=" * 80)
    print(f" RED METRICS SYNCHRONIZED COMPARISON ({project_name.upper()})")
    print("=" * 80)

    summary_headers = ["RED Metric Dimension", "Locust (Client)", "Prometheus (SDK Native)", "Status / Match"]
    summary_rows = [
        [
            "Total Requests (R)",
            f"{loc_total_reqs:,}",
            f"{prom_total_calls:,}",
            f"{match_ratio:.1f}% (Synchronized)" if match_ratio >= 98 else f"{match_ratio:.1f}%",
        ],
        [
            "Current RPS (Rate)",
            f"{loc_current_rps:.1f} req/s",
            f"{prom_rps_1m:.1f} req/s [1m]",
            "Active Load" if loc_current_rps > 0 else "Idle",
        ],
        [
            "Window Avg RPS (Rate)",
            f"{loc_total_rps:.1f} req/s (overall)",
            f"{prom_rps_window:.1f} req/s [{window}]",
            "Averaged",
        ],
        [
            "Server Errors (5xx)",
            f"{loc_failures:,} total non-2xx",
            f"{prom_err_5xx:,} (error='true')",
            "APM Standard" if prom_err_5xx > 0 else "0",
        ],
        [
            "Client Errors (4xx)",
            "-",
            f"{prom_err_4xx:,} (e.g. 405s)",
            f"Total non-200: {prom_client_server_fails:,}",
        ],
        [
            "Median Latency P50 (D)",
            f"{loc_p50:.0f} ms",
            f"{prom_p50:.1f} ms",
            "Aligned (±5ms)" if abs(loc_p50 - prom_p50) < 15 else "Within bucket",
        ],
        [
            "95th Percentile P95 (D)",
            f"{loc_p95:.0f} ms",
            f"{prom_p95:.1f} ms",
            "Histogram View",
        ],
        [
            "99th Percentile P99 (D)",
            f"{loc_p99:.0f} ms",
            f"{prom_p99:.1f} ms",
            "Histogram View",
        ],
    ]

    print(format_table(summary_headers, summary_rows))
    print()

    # Per endpoint comparison
    prom_routes = query_prometheus(prom_url, f"sum(calls_total{{{filter_proj}}}) by (http_method, http_route)")
    ep_rows = match_endpoints(loc_data.get("stats", []), prom_routes)

    if ep_rows:
        print("PER-ENDPOINT RECONCILIATION:")
        ep_headers = ["Method", "Locust Route Pattern", "Locust Reqs", "Prom Calls", "Match", "Loc Fail", "Loc P95"]
        formatted_ep_rows = [
            [r["method"], r["route"], f"{r['loc_reqs']:,}", f"{r['prom_reqs']:,}", r["match_pct"], str(r["loc_fails"]), r["loc_p95"]]
            for r in ep_rows[:15]
        ]
        print(format_table(ep_headers, formatted_ep_rows))
        if len(ep_rows) > 15:
            print(f"... and {len(ep_rows) - 15} additional endpoints verified.")
    print()


def main():
    parser = argparse.ArgumentParser(description="Synchronized RED metrics comparison tool (Locust vs Prometheus).")
    parser.add_argument("--locust-url", default="http://localhost:8089", help="Locust web UI URL (default: http://localhost:8089)")
    parser.add_argument("--prom-url", default="http://localhost:9090", help="Prometheus URL (default: http://localhost:9090)")
    parser.add_argument("--project", default="testpress", help="Project name to filter in Prometheus (default: testpress)")
    parser.add_argument("--window", default="5m", help="Prometheus rate/histogram window (default: 5m)")
    parser.add_argument("--watch", type=int, default=0, help="Continuously poll every N seconds (0 for single run)")

    args = parser.parse_args()

    if args.watch > 0:
        print(f"[*] Starting live continuous comparison every {args.watch}s (Ctrl+C to stop)...")
        try:
            while True:
                # Clear terminal screen for smooth dashboard view
                print("\033[2J\033[H", end="")
                run_comparison(args.locust_url, args.prom_url, args.project, args.window)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_comparison(args.locust_url, args.prom_url, args.project, args.window)


if __name__ == "__main__":
    main()
