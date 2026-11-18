#!/usr/bin/env bash
# ==============================================================================
# repro_collector_swap.sh - Reproduce Monday-night Collector swap-thrashing
# ==============================================================================
# Starts an isolated local Collector (otel-collector-repro) with exact
# production limits from before/after the Nov 16 incident and drives bounded
# OTLP/HTTP trace load with pinned telemetrygen v0.162.0.
#
# Keeping container limits identical to production and scaling traffic upward
# (via --workers / --rate) lets faster Apple Silicon Macs saturate the same
# 0.5 CPU / 512m limits without artificially shrinking container quotas.
#
# Modes:
#   swap   (default) 0.5 CPU, 512m RAM, 1024m swap (Docker 2x default),
#                    GOMEMLIMIT=380MiB, memory_limiter=400/80 MiB.
#                    Reproduces swap-thrashing + self-scrape timeout (up == 0).
#   oom              0.5 CPU, 512m RAM, 512m swap (memswap == mem).
#                    Demonstrates fail-fast OOM-kill + automatic restart.
#   fixed            1.0 CPU, 2048m RAM, 2048m swap, GOMEMLIMIT=1500MiB,
#                    memory_limiter=1600/350 MiB (post-#68 limits).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/dev/docker-compose.yml"

TELEMETRYGEN_IMAGE="ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:v0.162.0"
ALPINE_IMAGE="alpine:3.20"
COLLECTOR_CONTAINER="tp-otel-collector-repro"
TELEMETRYGEN_CONTAINER="tp-telemetrygen-repro"
COLLECTOR_ALIAS="repro-cluster2-app1"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3001}"
HEALTH_URL="${REPRO_HEALTH_URL:-http://localhost:13134/}"

MODE="swap"
DURATION="5m"
WORKERS="12"
RATE="1500"
CHILD_SPANS="24"
BATCH_SIZE="256"
PAYLOAD_MB="0"
SPAN_BYTES="5000"
POLL_INTERVAL="15"
START_STACK=1
TEARDOWN_AFTER=0

usage() {
  cat <<'EOF'
Usage: tests/e2e/repro_collector_swap.sh [options]

Options:
  --mode <swap|oom|fixed>  Resource profile (default: swap)
                           swap  = 0.5 CPU, 512m RAM, 1024m swap (Monday incident)
                           oom   = 0.5 CPU, 512m RAM, 512m swap (fail-fast OOM)
                           fixed = 1.0 CPU, 2048m RAM, 2048m swap (post-#68 fix)
  --duration <dur>         Finite Go duration for load, e.g. 2m, 5m (default: 5m)
  --workers <n>            Telemetrygen worker goroutines (default: 12)
                           Increase on Apple Silicon (e.g. 16-24) to saturate
                           0.5 CPU / 512m without lowering container limits.
  --rate <n>               Approximate configured spans/s per worker (default: 1500)
                           Configured target = workers * rate; delivered throughput
                           may be lower under backpressure or export failures.
  --child-spans <n>        Child spans per trace, >= 1 (default: 24)
  --batch-size <n>         Spans per OTLP batch (default: 256)
  --span-bytes <bytes>     Target size in bytes for span attribute payload (default: 5000)
  --size <mb>              Extra MB per parent span, >= 0 (default: 0)
  --poll-interval <sec>    Monitoring output interval in seconds (default: 15)
  --no-start               Reuse an already-running otel-collector-repro container
  --down                   Stop and remove otel-collector-repro when finished
  -h, --help               Show this help

Notes for macOS (Mac mini):
  - Docker containers use the Docker Linux VM's swap, not macOS host swap.
    In Docker Desktop, verify Settings -> Resources -> Swap is >= 1 GiB.
  - Explicit telemetrygen CLI flags control endpoint, path, TLS, and timeout;
    running via `docker run --rm` without `-e` prevents inheriting host
    OTEL_EXPORTER_OTLP_* header/compression environment variables.
EOF
}

require_pos_int() {
  local flag="$1"
  local val="$2"
  if [[ ! "$val" =~ ^[0-9]+$ ]] || [ "$((10#$val))" -eq 0 ]; then
    echo "Error: ${flag} must be a positive integer (got '${val}')" >&2
    exit 1
  fi
}

require_nonneg_int() {
  local flag="$1"
  local val="$2"
  if [[ ! "$val" =~ ^[0-9]+$ ]]; then
    echo "Error: ${flag} must be a non-negative integer (got '${val}')" >&2
    exit 1
  fi
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --duration)
      DURATION="${2:-}"
      shift 2
      ;;
    --workers)
      WORKERS="${2:-}"
      shift 2
      ;;
    --rate)
      RATE="${2:-}"
      shift 2
      ;;
    --child-spans)
      CHILD_SPANS="${2:-}"
      shift 2
      ;;
    --batch-size)
      BATCH_SIZE="${2:-}"
      shift 2
      ;;
    --span-bytes)
      SPAN_BYTES="${2:-}"
      shift 2
      ;;
    --size)
      PAYLOAD_MB="${2:-}"
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL="${2:-}"
      shift 2
      ;;
    --no-start)
      START_STACK=0
      shift
      ;;
    --down)
      TEARDOWN_AFTER=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# Validate finite load bounds (refuse --rate 0 and --duration inf)
if [[ "$DURATION" == "inf" ]] || [[ ! "$DURATION" =~ ^[0-9]+(ms|s|m|h)$ ]]; then
  echo "Error: --duration must be a finite Go duration like 30s, 5m, 1h (got '${DURATION}')" >&2
  exit 1
fi
require_pos_int "--workers" "$WORKERS"
require_pos_int "--rate" "$RATE"
require_pos_int "--child-spans" "$CHILD_SPANS"
require_pos_int "--batch-size" "$BATCH_SIZE"
require_nonneg_int "--span-bytes" "$SPAN_BYTES"
require_nonneg_int "--size" "$PAYLOAD_MB"
require_pos_int "--poll-interval" "$POLL_INTERVAL"

case "$MODE" in
  swap)
    export REPRO_COLLECTOR_CPUS="0.5"
    export REPRO_COLLECTOR_MEMORY_LIMIT="512m"
    export REPRO_COLLECTOR_MEMSWAP_LIMIT="1024m"
    export REPRO_COLLECTOR_GOMEMLIMIT="380MiB"
    export REPRO_COLLECTOR_GOGC="80"
    export REPRO_COLLECTOR_MEMORY_LIMIT_MIB="400"
    export REPRO_COLLECTOR_MEMORY_SPIKE_LIMIT_MIB="80"
    ;;
  oom)
    export REPRO_COLLECTOR_CPUS="0.5"
    export REPRO_COLLECTOR_MEMORY_LIMIT="512m"
    export REPRO_COLLECTOR_MEMSWAP_LIMIT="512m"
    export REPRO_COLLECTOR_GOMEMLIMIT="380MiB"
    export REPRO_COLLECTOR_GOGC="80"
    export REPRO_COLLECTOR_MEMORY_LIMIT_MIB="400"
    export REPRO_COLLECTOR_MEMORY_SPIKE_LIMIT_MIB="80"
    ;;
  fixed)
    export REPRO_COLLECTOR_CPUS="1.0"
    export REPRO_COLLECTOR_MEMORY_LIMIT="2048m"
    export REPRO_COLLECTOR_MEMSWAP_LIMIT="2048m"
    export REPRO_COLLECTOR_GOMEMLIMIT="1500MiB"
    export REPRO_COLLECTOR_GOGC="80"
    export REPRO_COLLECTOR_MEMORY_LIMIT_MIB="1600"
    export REPRO_COLLECTOR_MEMORY_SPIKE_LIMIT_MIB="350"
    ;;
  *)
    echo "Error: --mode must be one of: swap, oom, fixed (got '${MODE}')" >&2
    exit 1
    ;;
esac

export REPRO_COLLECTOR_ALIAS="${COLLECTOR_ALIAS}"

COMPOSE_CMD=(docker compose)
if [[ -f "${REPO_ROOT}/.env" ]]; then
  COMPOSE_CMD+=(--env-file "${REPO_ROOT}/.env")
fi
COMPOSE_CMD+=(-f "${COMPOSE_FILE}" --profile repro)

cleanup() {
  if docker ps -a -q -f "name=^${TELEMETRYGEN_CONTAINER}$" | grep -q .; then
    docker rm -f "${TELEMETRYGEN_CONTAINER}" >/dev/null 2>&1 || true
  fi
  if [[ "${TEARDOWN_AFTER}" -eq 1 ]]; then
    echo ""
    echo "Stopping ${COLLECTOR_CONTAINER} (--down)..."
    "${COMPOSE_CMD[@]}" rm -sf otel-collector-repro >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

prom_query() {
  local expr="$1"
  python3 - "$PROMETHEUS_URL" "$expr" <<'PY'
import json
import sys
import urllib.parse
import urllib.request

base_url, expr = sys.argv[1], sys.argv[2]
url = f"{base_url}/api/v1/query?query={urllib.parse.quote(expr, safe='')}"
try:
    with urllib.request.urlopen(url, timeout=3) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    results = payload.get("data", {}).get("result", [])
    if not results:
        print("missing")
    else:
        val = float(results[0]["value"][1])
        print(f"{val:.2f}")
except Exception:
    print("err")
PY
}

check_vm_swap() {
  local swap_line swap_total
  swap_line="$(docker run --rm "${ALPINE_IMAGE}" free -m 2>/dev/null | awk '/^Swap:/ {print $2 " " $3}' || true)"
  swap_total="$(printf '%s' "$swap_line" | awk '{print $1}')"
  if [[ -z "$swap_total" ]]; then
    echo "Warning: could not inspect Docker VM swap via ${ALPINE_IMAGE}."
    return
  fi
  echo "Docker Linux VM swap: total=${swap_total} MiB, used=$(printf '%s' "$swap_line" | awk '{print $2}') MiB"
  if [[ "$MODE" == "swap" ]] && [[ "$swap_total" -lt 512 ]]; then
    echo "WARNING: Docker VM swap is < 512 MiB (${swap_total} MiB)."
    echo "         Without VM swap, --mode swap will OOM-kill immediately instead of"
    echo "         reproducing the Monday swap-thrashing freeze. In Docker Desktop,"
    echo "         set Settings -> Resources -> Swap to >= 1 GiB."
  fi
}

echo "=== Collector Swap/OOM Incident Repro ==="
echo "Mode:             ${MODE}"
echo "Limits:           cpus=${REPRO_COLLECTOR_CPUS}, mem=${REPRO_COLLECTOR_MEMORY_LIMIT}, memswap=${REPRO_COLLECTOR_MEMSWAP_LIMIT}"
echo "Go / Limiter:     GOMEMLIMIT=${REPRO_COLLECTOR_GOMEMLIMIT}, limit_mib=${REPRO_COLLECTOR_MEMORY_LIMIT_MIB}, spike_mib=${REPRO_COLLECTOR_MEMORY_SPIKE_LIMIT_MIB}"
TOTAL_TARGET_SPANS=$((WORKERS * RATE))
TRACES_PER_SEC=$((TOTAL_TARGET_SPANS / (CHILD_SPANS + 1)))
echo "Load target:      duration=${DURATION}, workers=${WORKERS}, rate=${RATE}/worker (~${TOTAL_TARGET_SPANS} spans/s, ~${TRACES_PER_SEC} traces/s, ~${SPAN_BYTES} bytes/span)"
echo ""

check_vm_swap

if [[ "${START_STACK}" -eq 1 ]]; then
  echo "Starting core observability services (tempo, prometheus, grafana) and ${COLLECTOR_CONTAINER}..."
  "${COMPOSE_CMD[@]}" up --no-build -d --wait --wait-timeout 180 tempo prometheus grafana
  "${COMPOSE_CMD[@]}" up --no-build -d --force-recreate otel-collector-repro

  echo -n "Waiting for ${COLLECTOR_CONTAINER} health endpoint (${HEALTH_URL})..."
  for _ in $(seq 1 30); do
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
      echo " ready."
      break
    fi
    sleep 1
  done
  if ! curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    echo " timed out!" >&2
    exit 1
  fi
fi

echo "Active container cgroup limits:"
docker inspect "${COLLECTOR_CONTAINER}" \
  --format '  NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}} MemorySwap={{.HostConfig.MemorySwap}} RestartCount={{.RestartCount}}'

echo ""
echo "Launching telemetrygen (${TELEMETRYGEN_IMAGE}) against otel-collector-repro:4318 on tp-dev-net..."

# Remove any leftover run container first
docker rm -f "${TELEMETRYGEN_CONTAINER}" >/dev/null 2>&1 || true

# Realistic SQL payload roughly mirroring production Django DB span attributes
# Sized to ~SPAN_BYTES (default: 5000 bytes) to match heavy production SQL/ORM queries.
if [[ "${SPAN_BYTES}" -gt 200 ]]; then
  REPEAT_COUNT=$(( (SPAN_BYTES - 150) / 16 ))
  if command -v python3 >/dev/null 2>&1; then
    SQL_PAYLOAD="$(python3 -c "print('0123456789abcdef' * ${REPEAT_COUNT})")"
  else
    SQL_PAYLOAD="$(head -c "$((REPEAT_COUNT * 16))" < /dev/zero | tr '\0' 'a')"
  fi
  SQL_SAMPLE="SELECT id, institute_id, title, payload FROM courses_course WHERE query_hash = '${SQL_PAYLOAD}' AND is_active = true"
else
  SQL_SAMPLE="SELECT id, institute_id, title, slug, is_active, modified FROM courses_course WHERE institute_id = 123 AND is_active = true ORDER BY modified DESC LIMIT 50"
fi

# --allow-export-failures is used strictly against this local disposable repro
# collector so traffic continues past memory_limiter refusals and OOM restarts
# for the finite --duration; export errors remain visible in container logs.
TG_ARGS=(
  traces
  --otlp-http
  --otlp-insecure
  --otlp-endpoint otel-collector-repro:4318
  --allow-export-failures
  --duration "${DURATION}"
  --workers "${WORKERS}"
  --rate "${RATE}"
  --child-spans "${CHILD_SPANS}"
  --batch
  --batch-size "${BATCH_SIZE}"
  --service "testpress"
  --otlp-attributes 'deployment.environment.name="production"'
  --telemetry-attributes 'http.method="GET"'
  --telemetry-attributes 'http.route="/api/v2/courses/"'
  --telemetry-attributes 'db.system="postgresql"'
  --telemetry-attributes 'db.operation="SELECT"'
  --telemetry-attributes "db.statement=\"${SQL_SAMPLE}\""
  --telemetry-attributes 'tp_trace.sampled=true'
)
if [[ "${PAYLOAD_MB}" -gt 0 ]]; then
  TG_ARGS+=(--size "${PAYLOAD_MB}")
fi

docker run -d --name "${TELEMETRYGEN_CONTAINER}" \
  --network "tp-dev-net" \
  "${TELEMETRYGEN_IMAGE}" \
  "${TG_ARGS[@]}" >/dev/null

echo ""
printf '%-8s | %-7s | %-20s | %-4s | %-3s | %-8s | %-8s | %-9s | %-8s | %-8s | %-6s\n' \
  "TIME" "CPU%" "MEM USAGE / LIMIT" "RSTR" "OOM" "VM_SWAP" "UP(fail)" "SCRAPE_S" "RSS_MiB" "REFUSED" "QUEUE"
printf '%s\n' "---------+---------+----------------------+------+-----+----------+----------+-----------+----------+----------+-------"

while docker ps -q -f "name=^${TELEMETRYGEN_CONTAINER}$" | grep -q .; do
  NOW_TS="$(date +%H:%M:%S)"
  STATS="$(docker stats --no-stream --format '{{.CPUPerc}}|{{.MemUsage}}' "${COLLECTOR_CONTAINER}" 2>/dev/null || echo 'n/a|n/a')"
  CPU_PCT="$(printf '%s' "$STATS" | cut -d'|' -f1)"
  MEM_USE="$(printf '%s' "$STATS" | cut -d'|' -f2)"

  INSP="$(docker inspect "${COLLECTOR_CONTAINER}" --format '{{.RestartCount}}|{{.State.OOMKilled}}' 2>/dev/null || echo '?|?')"
  RESTARTS="$(printf '%s' "$INSP" | cut -d'|' -f1)"
  OOM_KILLED="$(printf '%s' "$INSP" | cut -d'|' -f2)"

  VM_SWAP="$(docker run --rm "${ALPINE_IMAGE}" free -m 2>/dev/null | awk '/^Swap:/ {print $3 "MiB"}' || echo 'n/a')"

  PROM_UP="$(prom_query "up{source=\"otelcol-telemetry\",collector_alias=\"${COLLECTOR_ALIAS}\"}")"
  PROM_SCRAPE="$(prom_query "scrape_duration_seconds{collector_alias=\"${COLLECTOR_ALIAS}\"}")"
  PROM_RSS="$(prom_query "otelcol_process_memory_rss_bytes{collector_alias=\"${COLLECTOR_ALIAS}\"} / 1048576")"
  PROM_REFUSED="$(prom_query "rate(otelcol_processor_refused_spans_total{collector_alias=\"${COLLECTOR_ALIAS}\"}[1m])")"
  PROM_RWQ="$(prom_query "max(otelcol_exporter_queue_size{collector_alias=\"${COLLECTOR_ALIAS}\",exporter=\"otlp/tempo\"})")"

  printf '%-8s | %-7s | %-20s | %-4s | %-3s | %-8s | %-8s | %-9s | %-8s | %-8s | %-6s\n' \
    "$NOW_TS" "$CPU_PCT" "$MEM_USE" "$RESTARTS" "$OOM_KILLED" "$VM_SWAP" "$PROM_UP" "$PROM_SCRAPE" "$PROM_RSS" "$PROM_REFUSED" "$PROM_RWQ"

  sleep "${POLL_INTERVAL}"
done

TG_EXIT="$(docker inspect "${TELEMETRYGEN_CONTAINER}" --format '{{.State.ExitCode}}' 2>/dev/null || echo "unknown")"
echo ""
echo "=== Telemetrygen finished (exit code: ${TG_EXIT}) ==="
docker logs --tail 10 "${TELEMETRYGEN_CONTAINER}" 2>&1 || true
docker rm -f "${TELEMETRYGEN_CONTAINER}" >/dev/null 2>&1 || true

FINAL_INSP="$(docker inspect "${COLLECTOR_CONTAINER}" --format 'RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} Status={{.State.Status}}' 2>/dev/null || echo 'unknown')"
echo ""
echo "Final ${COLLECTOR_CONTAINER} state: ${FINAL_INSP}"
echo "Inspect metrics in local Grafana: ${GRAFANA_URL} (filter Collector alias: ${COLLECTOR_ALIAS})"
echo "Inspect PromQL directly:          ${PROMETHEUS_URL}"
