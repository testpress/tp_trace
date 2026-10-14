#!/usr/bin/env python3
# ==============================================================================
# loadtest_s3.py - S3 / Boto3 load generator + APM verification for tp_trace
# ==============================================================================
# Drives the sample app's S3 endpoint under controlled concurrency and reports
# per-operation latency, then verifies that the corresponding boto spans
# actually reached Tempo.
#
# It reads the JSON body's "ok" field rather than only the HTTP status code.
# That distinction matters: S3StorageView swallows backend errors and returns
# HTTP 200 with status "traced_s3_call (...)", so an HTTP-only check would
# report 100% success against a completely dead MinIO.
#
# Usage:
#   ./loadtest_s3.py                          # 10s, 4 workers, put/get/delete
#   ./loadtest_s3.py -d 60 -c 16 -r 50        # 60s, 16 workers, capped at 50 rps
#   ./loadtest_s3.py -n 200 --ops put,get     # exactly 200 requests
#   ./loadtest_s3.py --ops list --require-traces   # fail if no S3 spans land
#
# Exit codes:
#   0  load ran and (if requested) traces were confirmed
#   1  bad usage / app unreachable / S3 operations failed
#   2  APM verification failed (only with --require-traces)
# ==============================================================================

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field

DEFAULT_URL = os.environ.get("APP_URL", "http://localhost:8001")
DEFAULT_TEMPO = os.environ.get("TEMPO_URL", "http://localhost:3200")
DEFAULT_GRAFANA = os.environ.get("GRAFANA_URL", "http://localhost:3000")

# Metrics/logs live under a 1-minute Prometheus scrape interval, so give the
# collector's batch processor (500ms) plus Tempo's ingester a moment to land.
FLUSH_WAIT_SECONDS = 3.0

RESET, BOLD = "\033[0m", "\033[1m"
RED, GREEN, YELLOW, BLUE, CYAN, MAGENTA = (
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[34m",
    "\033[36m",
    "\033[35m",
)


def _supports_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


if not _supports_colour():
    RESET = BOLD = RED = GREEN = YELLOW = BLUE = CYAN = MAGENTA = ""


@dataclass
class Sample:
    """One request's outcome."""

    op: str
    latency_ms: float
    http_status: int | None = None
    ok: bool = False
    status: str = ""
    transport_error: str = ""


@dataclass
class WorkerStats:
    samples: list[Sample] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, sample: Sample) -> None:
        with self.lock:
            self.samples.append(sample)


class RateLimiter:
    """Open-loop pacing: claim a slot, sleep until its scheduled time.

    Open-loop matters for a load test -- a closed loop (sleep a fixed amount
    after each request) silently reduces offered load as latency rises, which
    hides exactly the saturation you are trying to find.
    """

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate if rate > 0 else 0.0
        self._start = time.monotonic()
        self._next = 0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._interval == 0.0:
            return
        with self._lock:
            slot = self._next
            self._next += 1
        target = self._start + slot * self._interval
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def do_request(base_url: str, op: str, bucket: str, key: str, timeout: float) -> Sample:
    """Issue one S3 op. Reads `ok` from the body, not just the HTTP code."""
    query = urllib.parse.urlencode({"op": op, "bucket": bucket, "key": key})
    url = f"{base_url.rstrip('/')}/api/s3-storage/?{query}"
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            http_status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        http_status = exc.code
    except Exception as exc:  # URLError, socket.timeout, ConnectionReset...
        return Sample(
            op=op,
            latency_ms=(time.perf_counter() - started) * 1000,
            transport_error=f"{type(exc).__name__}: {exc}",
        )

    latency_ms = (time.perf_counter() - started) * 1000

    # A body we cannot parse is a failure, not a pass.
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return Sample(
            op=op,
            latency_ms=latency_ms,
            http_status=http_status,
            status=f"unparseable body: {body[:80]!r}",
        )

    return Sample(
        op=op,
        latency_ms=latency_ms,
        http_status=http_status,
        ok=bool(parsed.get("ok", False)),
        status=str(parsed.get("status", "")),
    )


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile: rank = ceil(pct/100 * N), 1-indexed.

    Nearest-rank reports a value that actually occurred rather than
    interpolating between two samples, so p99 of 100 requests is the 99th
    slowest request you really made.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(pct / 100.0 * len(ordered))
    return ordered[max(0, min(len(ordered) - 1, rank - 1))]


def run_worker(
    worker_id: int,
    args: argparse.Namespace,
    limiter: RateLimiter,
    deadline: float | None,
    budget: "Budget",
    stats: WorkerStats,
    stop: threading.Event,
) -> None:
    seq = 0
    ops = args.ops
    while not stop.is_set():
        if deadline is not None and time.monotonic() >= deadline:
            return
        if not budget.claim():
            return

        # Each worker owns its key namespace so a concurrent delete by one
        # worker cannot 404 another worker's get.
        seq += 1
        key = f"{args.key_prefix}/w{worker_id}-{seq}.json"

        for op in ops:
            if stop.is_set():
                return
            limiter.wait()
            stats.add(do_request(args.url, op, args.bucket, key, args.timeout))


class Budget:
    """Shared --count budget across workers. A total of 0 means unlimited."""

    def __init__(self, total: int) -> None:
        self._unlimited = total <= 0
        self._remaining = total
        self._lock = threading.Lock()

    def claim(self) -> bool:
        if self._unlimited:
            return True
        with self._lock:
            if self._remaining <= 0:
                return False
            self._remaining -= 1
            return True


def summarise(samples: list[Sample], elapsed: float) -> dict:
    """Aggregate by op, separating transport, S3, and simulated failures."""
    by_op: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by_op[s.op].append(s)

    transport_failures = [s for s in samples if s.transport_error]
    unparseable = [s for s in samples if s.status.startswith("unparseable")]
    simulated = [s for s in samples if s.status == "simulated"]
    s3_failures = [s for s in samples if not s.ok and not s.transport_error
                   and not s.status.startswith("unparseable") and s.status != "simulated"]

    return {
        "total": len(samples),
        "elapsed": elapsed,
        "by_op": by_op,
        "transport_failures": transport_failures,
        "unparseable": unparseable,
        "simulated": simulated,
        "s3_failures": s3_failures,
    }


def print_summary(summary: dict, args: argparse.Namespace) -> None:
    elapsed = summary["elapsed"] or 1e-9
    total = summary["total"]
    transport = summary["transport_failures"]
    unparseable = summary["unparseable"]
    simulated = summary["simulated"]
    s3_failures = summary["s3_failures"]

    good = total - len(transport) - len(unparseable) - len(simulated) - len(s3_failures)

    print(f"\n{BOLD}{CYAN}{'=' * 70}{RESET}")
    print(f"{BOLD}📊 S3 Load Test Summary{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 70}{RESET}")
    print(f"  Target          {args.url}/api/s3-storage/")
    print(f"  Operations      {', '.join(args.ops)}")
    print(f"  Concurrency     {args.concurrency} worker(s)")
    if args.rate > 0:
        print(f"  Rate cap        {args.rate} req/s")
    print(f"  Elapsed         {elapsed:.1f}s")
    print(f"  Throughput      {total / elapsed:.1f} req/s")
    print()

    print(f"{BOLD}  Per-operation latency{RESET}")
    header = f"    {'op':<8} {'n':>6} {'ok':>6} {'fail':>6} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}"
    print(f"{BOLD}{header}{RESET}")
    for op in args.ops:
        group = summary["by_op"].get(op, [])
        if not group:
            print(f"    {op:<8} {'0':>6}  {YELLOW}no samples{RESET}")
            continue
        latencies = [s.latency_ms for s in group]
        fails = [s for s in group if not s.ok and not s.transport_error
                 and not s.status.startswith("unparseable") and s.status != "simulated"]
        colour = GREEN if not fails else RED
        print(
            f"    {op:<8} {len(group):>6} {colour}{len(group) - len(fails):>6}{RESET} "
            f"{colour}{len(fails):>6}{RESET} "
            f"{percentile(latencies, 50):>7.1f}m {percentile(latencies, 95):>7.1f}m "
            f"{percentile(latencies, 99):>7.1f}m {max(latencies):>7.1f}m"
        )
    print()

    print(f"{BOLD}  Outcome breakdown{RESET}")
    print(f"    {GREEN}✔ S3 operations succeeded{RESET}  {good}")
    if s3_failures:
        print(f"    {RED}✖ S3 rejected the operation{RESET}     {len(s3_failures)}")
        seen: dict[str, int] = defaultdict(int)
        for s in s3_failures:
            seen[s.status] += 1
        for status, n in sorted(seen.items(), key=lambda kv: -kv[1])[:5]:
            print(f"        {RED}{n:>5}{RESET} × {status}")
    if transport:
        print(f"    {RED}✖ Transport / HTTP failures{RESET}     {len(transport)}")
        seen_t: dict[str, int] = defaultdict(int)
        for s in transport:
            seen_t[s.status or str(s.http_status)] += 1
        for status, n in sorted(seen_t.items(), key=lambda kv: -kv[1])[:5]:
            print(f"        {RED}{n:>5}{RESET} × {status[:70]}")
    if unparseable:
        print(f"    {RED}✖ Unparseable response bodies{RESET}  {len(unparseable)}")
    if simulated:
        print(f"    {YELLOW}⚠ 'simulated' responses{RESET}        {len(simulated)}")
        print(f"      {YELLOW}botocore was not importable in the app, so NO boto")
        print(f"      spans were emitted. These requests prove nothing.{RESET}")
    print()


# ------------------------------------------------------------------------------
# APM verification
# ------------------------------------------------------------------------------
def tempo_search(tempo_url: str, query: str, limit: int = 20) -> dict | None:
    """Query Tempo's TraceQL search API. Returns None if unreachable."""
    url = f"{tempo_url.rstrip('/')}/api/search?{urllib.parse.urlencode({'q': query, 'limit': limit})}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def search_with_retry(
    tempo_url: str, query: str, attempts: int = 4, delay: float = 2.5, limit: int = 20
) -> tuple[dict | None, bool]:
    """Poll Tempo until the query returns rows, or we give up.

    Returns (payload, ok). Without this, a span that is merely still being
    indexed reads as a missing span, and the run reports a false negative.
    """
    payload = None
    for attempt in range(attempts):
        payload = tempo_search(tempo_url, query, limit=limit)
        if payload and (payload.get("traces") or payload.get("metrics")):
            return payload, True
        if attempt < attempts - 1:
            time.sleep(delay)
    return payload, False


def tempo_trace(tempo_url: str, trace_id: str) -> dict | None:
    url = f"{tempo_url.rstrip('/')}/api/traces/{trace_id}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def collect_span_names(payload: dict) -> set[str]:
    """Pull every span name out of an OTLP/JSON trace payload.

    Tempo's /api/traces returns the OTLP JSON shape:
        {"batches": [{"scopeSpans": [{"spans": [{"name": ...}]}]}]}
    so the walk has to unwrap all three levels. Some deployments serve the
    older resourceSpans naming, which is handled too.
    """
    names: set[str] = set()

    def walk_spans(spans) -> None:
        for span in spans or []:
            if not isinstance(span, dict):
                continue
            name = span.get("name")
            if isinstance(name, str) and name:
                names.add(name)
            walk_spans(span.get("childSpans") or span.get("children"))

    def walk_scope_spans(groups) -> None:
        for group in groups or []:
            if not isinstance(group, dict):
                continue
            walk_spans(group.get("spans"))

    def walk_resource_spans(resources) -> None:
        for res in resources or []:
            if not isinstance(res, dict):
                continue
            walk_scope_spans(res.get("scopeSpans") or res.get("instrumentationLibrarySpans"))

    if not isinstance(payload, dict):
        return names

    for batch in payload.get("batches") or []:
        if isinstance(batch, dict):
            walk_scope_spans(batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans"))

    walk_resource_spans(payload.get("resourceSpans"))

    return names


# The botocore instrumentor names spans "{rpc.service}.{rpc.method}", i.e.
# "S3.<Operation>" (verified in extensions/types.py:73). Map our op names to the
# span names they must produce, so verification can ask Tempo about each one
# directly instead of sampling whichever traces happen to be newest.
SPAN_NAME_FOR_OP = {
    "put": "S3.PutObject",
    "get": "S3.GetObject",
    "delete": "S3.DeleteObject",
    "list": "S3.ListObjectsV2",
    "head": "S3.HeadObject",
}


def verify_apm(args: argparse.Namespace) -> bool:
    """Confirm boto spans reached Tempo. Returns True if all ops confirmed."""
    print(f"{BOLD}{CYAN}{'=' * 70}{RESET}")
    print(f"{BOLD}🔍 APM Verification (Tempo){RESET}")
    print(f"{BOLD}{CYAN}{'=' * 70}{RESET}")
    print(f"  Waiting {FLUSH_WAIT_SECONDS:.0f}s for the collector batch + Tempo ingest...")
    time.sleep(FLUSH_WAIT_SECONDS)

    if tempo_search(args.tempo, '{ name =~ ".*S3.*" }', limit=1) is None:
        print(f"  {RED}✖ Could not reach Tempo at {args.tempo}{RESET}")
        print(f"    The load numbers above are still valid, but APM delivery is")
        print(f"    UNVERIFIED -- do not read this as a pass.")
        return False
    print(f"  Tempo reachable at {args.tempo}")

    # Ask per operation rather than sampling recent traces: other generators
    # (e.g. `generate_traffic.sh -b`) may be writing S3 traces continuously,
    # so "the newest N traces" is not a reliable sample of what we just sent.
    print()
    print(f"{BOLD}  Span delivery per operation{RESET}")
    print(f"    {'op':<8} {'expected span':<20} {'traces in Tempo':>16}")
    confirmed: set[str] = set()
    missing: list[str] = []
    for op in args.ops:
        span_name = SPAN_NAME_FOR_OP.get(op)
        if not span_name:
            print(f"    {op:<8} {'(no span name known)':<20} {'-':>16}")
            missing.append(op)
            continue
        # Retry before declaring an op missing: Tempo indexing lags the
        # collector's batch export, so a single query right after the load can
        # miss spans that are about to land.
        data, _ = search_with_retry(
            args.tempo, '{ name =~ ".*' + span_name + '" }', attempts=4, delay=2.5
        )
        if data is None:
            print(f"    {RED}{op:<8} {span_name:<20} {'query failed':>16}{RESET}")
            missing.append(op)
            continue
        count = len(data.get("traces") or [])
        if count:
            confirmed.add(span_name)
            print(f"    {GREEN}{op:<8} {span_name:<20} {count:>16}{RESET}")
        else:
            print(f"    {RED}{op:<8} {span_name:<20} {0:>16}{RESET}  {YELLOW}(after retries){RESET}")
            missing.append(op)

    # Prove the boto spans are nested inside the Django request trace rather
    # than arriving as orphaned roots.
    print()
    data = tempo_search(args.tempo, '{ name =~ ".*S3.*" }', limit=20)
    traces = (data or {}).get("traces") or []
    nested = False
    for entry in traces[:5]:
        trace_id = entry.get("traceID") or entry.get("traceId")
        if not trace_id:
            continue
        payload = tempo_trace(args.tempo, trace_id)
        if not payload:
            continue
        names = collect_span_names(payload)
        if any(n.startswith("S3.") for n in names) and any("django.request" in n for n in names):
            nested = True

    if nested:
        print(f"  {GREEN}✔ boto spans are nested inside the 'django.request' trace{RESET}")
    else:
        print(f"  {YELLOW}⚠ Could not confirm S3 spans nest under a django.request span.{RESET}")

    if missing:
        print(f"  {RED}✖ No spans found for: {', '.join(missing)}{RESET}")
        print(f"    Those operations were not recorded in Tempo. If they returned")
        print(f"    ok=true, the boto integration may not be applied in the app.")

    print()
    print(f"  {CYAN}Inspect in Grafana:{RESET}")
    print(f"    🔥 Flamegraph (TraceQL: S3 spans)")
    print(f"       {args.grafana}/explore?schemaVersion=1&panes=%7B%22p%22:%7B%22datasource%22:%22tempo%22,"
          f"%22queries%22:%5B%7B%22query%22:%22%7Bname=~%5C%22.%2AS3.%2A%5C%22%7D%22,%22queryType%22:%22traceql%22%7D%5D%7D%7D")
    print(f"    📊 Service overview  (var-service = aws-s3):")
    print(f"       {args.grafana}/d/generic-service-overview?var-service=aws-s3")
    print(f"    📋 Operation details (shows S3.PutObject / S3.GetObject / ...):")
    print(f"       {args.grafana}/d/generic-operation-details?var-service=aws-s3")
    print()
    print(f"  {CYAN}Tempo raw search:{RESET} {args.tempo}/api/search?q=%7B+name=~%22.*S3.*%22%7D")
    print(f"{BOLD}{CYAN}{'=' * 70}{RESET}")

    return not missing and bool(confirmed)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load test the sample app's S3 endpoint and verify boto APM spans.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-u", "--url", default=DEFAULT_URL, help=f"App base URL (default: {DEFAULT_URL})")
    parser.add_argument("-d", "--duration", type=float, default=10.0, help="Seconds to run (default: 10)")
    parser.add_argument("-n", "--count", type=int, default=0, help="Stop after N requests (default: unlimited)")
    parser.add_argument("-c", "--concurrency", type=int, default=4, help="Parallel workers (default: 4)")
    parser.add_argument("-r", "--rate", type=float, default=0.0, help="Cap total req/s, 0 = uncapped (default: 0)")
    parser.add_argument("--ops", default="put,get,delete",
                        help="Comma-separated ops: put,get,delete,list,head (default: put,get,delete)")
    parser.add_argument("--bucket", default="sample-products-bucket", help="S3 bucket")
    parser.add_argument("--key-prefix", default="loadtest", help="Key namespace prefix")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout seconds")
    parser.add_argument("--tempo", default=DEFAULT_TEMPO, help=f"Tempo URL (default: {DEFAULT_TEMPO})")
    parser.add_argument("--grafana", default=DEFAULT_GRAFANA, help=f"Grafana URL (default: {DEFAULT_GRAFANA})")
    parser.add_argument("--no-verify", action="store_true", help="Skip the Tempo APM check")
    parser.add_argument("--require-traces", action="store_true",
                        help="Exit 2 if no S3 spans are confirmed in Tempo")
    args = parser.parse_args()

    args.ops = [op.strip().lower() for op in args.ops.split(",") if op.strip()]
    valid = {"put", "get", "delete", "list", "head"}
    unknown = [op for op in args.ops if op not in valid]
    if unknown or not args.ops:
        print(f"{RED}Invalid --ops: {', '.join(unknown) or '(empty)'}{RESET}")
        print(f"Valid operations: {', '.join(sorted(valid))}")
        print(f"Note: 'get' and 'delete' need an existing key -- include 'put' first.")
        return 1
    if args.concurrency < 1:
        print(f"{RED}--concurrency must be >= 1{RESET}")
        return 1

    # Fail fast and loudly if the app is not up, rather than reporting
    # thousands of connection errors as a load result.
    try:
        with urllib.request.urlopen(f"{args.url.rstrip('/')}/api/s3-storage/?op=list", timeout=10) as r:
            r.read(1)
    except Exception as exc:
        print(f"{RED}✖ Sample app unreachable at {args.url}{RESET}")
        print(f"  {type(exc).__name__}: {exc}")
        print(f"  Start the stack first, e.g.: docker compose up -d")
        return 1

    print(f"{CYAN}{BOLD}")
    print("=" * 70)
    print("  🪣  S3 / Boto3 Load Test  →  APM Verification")
    print("=" * 70)
    print(f"  Target       : {args.url}/api/s3-storage/")
    print(f"  Operations   : {', '.join(args.ops)}")
    print(f"  Concurrency  : {args.concurrency} worker(s)")
    print(f"  Rate cap     : {'uncapped' if args.rate <= 0 else f'{args.rate} req/s'}")
    print(f"  Duration     : {'until ' + str(args.count) + ' requests' if args.count else f'{args.duration:.0f}s'}")
    print(f"  Tempo        : {args.tempo}")
    print("=" * 70)
    print(f"{RESET}")

    deadline = time.monotonic() + args.duration if not args.count else None
    limiter = RateLimiter(args.rate)
    budget = Budget(args.count)
    stop = threading.Event()
    started = time.perf_counter()

    # Keep the per-worker stats objects so their samples can be aggregated
    # after the pool drains.
    all_stats = [WorkerStats() for _ in range(args.concurrency)]
    workers = [
        threading.Thread(
            target=run_worker,
            args=(i, args, limiter, deadline, budget, all_stats[i - 1], stop),
            daemon=True,
        )
        for i in range(1, args.concurrency + 1)
    ]

    def shutdown(*_):
        stop.set()

    try:
        for w in workers:
            w.start()
        for w in workers:
            while w.is_alive():
                w.join(timeout=0.2)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted -- finishing in-flight requests...{RESET}")
        shutdown()
        for w in workers:
            w.join(timeout=5)
    finally:
        shutdown()
        for w in workers:
            w.join(timeout=5)

    elapsed = time.perf_counter() - started
    samples = [s for stats in all_stats for s in stats.samples]
    summary = summarise(samples, elapsed)
    print_summary(summary, args)

    verified = True
    if not args.no_verify:
        verified = verify_apm(args)

    # Exit code reflects the worst outcome, so this is usable in CI.
    if summary["transport_failures"] or summary["unparseable"] or summary["s3_failures"]:
        if args.require_traces and not verified:
            return 2
        return 1
    if args.require_traces and not verified:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
