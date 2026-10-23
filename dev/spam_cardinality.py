#!/usr/bin/env python3
"""High-cardinality endpoint traffic generator for local Collector comparison.

Spams unique operations across 4 distinct workloads against sample-app,
demonstrating unbound cardinality accumulation in the old collector vs
bounded series retention in the fixed collector.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import sys
import time
import urllib.request

WORKLOADS = ["", "catalog/", "checkout/", "search/"]


def send_request(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cardinality-spammer/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Generate high-cardinality traces")
    parser.add_argument("--base-url", default="http://localhost:8002", help="Sample app base URL")
    parser.add_argument("--count", type=int, default=1000, help="Number of distinct operations per workload")
    parser.add_argument("--concurrency", type=int, default=10, help="Parallel worker threads")
    parser.add_argument("--start", type=int, default=0, help="Starting operation ID offset")
    args = parser.parse_args()

    urls = []
    for op in range(args.start, args.start + args.count):
        for w in WORKLOADS:
            urls.append(f"{args.base_url}/api/collector-demo/{w}{op}/")

    total = len(urls)
    print(f"Generating {total} high-cardinality requests across {len(WORKLOADS)} workloads with concurrency {args.concurrency}...")
    start_time = time.monotonic()
    successes = 0

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        for ok in executor.map(send_request, urls):
            if ok:
                successes += 1
            if (successes % 200) == 0:
                sys.stdout.write(f"\rProgress: {successes}/{total} requests completed...")
                sys.stdout.flush()

    elapsed = time.monotonic() - start_time
    print(f"\nDone! Sent {successes}/{total} requests in {elapsed:.1f}s ({successes / elapsed:.1f} req/s).")


if __name__ == "__main__":
    main()
