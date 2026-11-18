# Collector Swap-Thrashing Local Repro

Reproduce the Nov 16 `cluster2_app1` Collector incident locally using an isolated `otel-collector-repro` container (`profiles: ["repro"]` in `dev/docker-compose.yml`) and `tests/e2e/repro_collector_swap.sh`.

## Prerequisites (Mac mini)

- Docker Desktop with **Swap >= 1 GiB** (**Settings -> Resources -> Swap**).
  Check VM swap with:
  ```bash
  docker run --rm alpine:3.20 free -h
  ```
  *(If `Swap:` is `0B`, `--mode swap` will OOM-kill immediately instead of swap-thrashing.)*

## Run the 3 Modes

### 1. Reproduce Monday's freeze (`0.5 CPU`, `512m` RAM, `1024m` swap)

```bash
./tests/e2e/repro_collector_swap.sh --mode swap
```

- **Production Topology & Load**:
  - Simulates the dedicated bare-metal server with **128 workers** dumping concurrent traces (~12,000 spans/sec) into the 0.5 CPU container.
  - Multi-collector fleet simulation enabled by default (`repro-cluster2-app1`, `repro-cluster1-app1`, `repro-cluster2-app2`) pushing concurrent traces to shared Tempo.
  - Simulates the inter-datacenter network RTT (**30ms default latency**) between the dedicated application server and the central Tempo instance (`65.21.236.56`), preventing unrealistic instantaneous 0ms localhost drains.
  - Tempo runs with production defaults: 15 MB/s distributor rate limit (`15000000 bytes/s`).
  - Defaults to **1 hour (`1h`)** duration to match sustained production workloads.
- **Expected Failure Signature**:
  - `CPU%` pinned near `50%` (100% of the 0.5 CPU quota).
  - Exporter queue (`QUEUE`) fills up due to inter-server latency and distributor rate limits.
  - Container memory breaches `512m` and spills into swap (`VM_SWAP` climbs).
  - Page-ins stall the Go runtime; Prometheus self-scrape exceeds timeout (`SCRAPE_S > 10s`).
  - `UP(fail) = 0.00` while `RSTR = 0` and `OOM = false`.
- Open local Grafana (`http://localhost:3001`) -> **OpenTelemetry Collector** dashboard and filter alias `repro-cluster2-app1`.

### 2. Verify fail-fast OOM (`0.5 CPU`, `512m` RAM, `512m` swap)

```bash
./tests/e2e/repro_collector_swap.sh --mode oom --duration 3m --workers 12 --rate 1500
```

- Same CPU/RAM limits with `memswap_limit == mem_limit`: container OOM-kills (`OOM=true`, `RSTR >= 1`) and restarts cleanly instead of wedging in swap.

### 3. Verify post-`#68` limits (`1.0 CPU`, `2048m` RAM, `2048m` swap) and clean up

```bash
./tests/e2e/repro_collector_swap.sh --mode fixed --duration 3m --workers 12 --rate 1500 --down
```

- `--down` stops and removes `tp-otel-collector-repro` when the run finishes.
