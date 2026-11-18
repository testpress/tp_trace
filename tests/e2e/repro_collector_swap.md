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
./tests/e2e/repro_collector_swap.sh --mode swap --duration 5m
```

- Simulates the production fleet topology: `repro-cluster2-app1` (target), `repro-cluster1-app1`, and `repro-cluster2-app2` pushing concurrent traces to shared Tempo.
- Tempo runs with production defaults: 15 MB/s distributor rate limit (`15000000 bytes/s`) and 2 GB memory ceiling.
- With 0ms artificial latency, aggregate fleet trace bandwidth breaches Tempo's 15 MB/s ceiling, naturally triggering gRPC backpressure, export queue accumulation, swap spilling, and scrape timeouts (`UP = 0.00`).
- Watch for: `CPU%` pinned near `50%`, `VM_SWAP` climbing, `SCRAPE_S > 10s`, `UP(fail) = 0.00`, and `RSS_MiB = missing` while `RSTR = 0` (`OOM = false`).
- Open local Grafana (`http://localhost:3001`) -> **OpenTelemetry Collector** dashboard and inspect collectors side-by-side or filter alias `repro-cluster2-app1`.

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
