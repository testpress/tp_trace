#!/usr/bin/env bash
# ==============================================================================
# generate_traffic.sh - Comprehensive Observability Traffic Generator
# ==============================================================================
# Sends realistic, diverse traffic across all sample app endpoints:
#   - Database primary and read replicas (Postgres default, slave1 via PgBouncer; slave2, slave3 direct)
#   - Individual DB targeting via CLI flags (--primary, --slave1, --slave2, --slave3, --pgbouncer, --db <name>)
#   - PgBouncer connection pooler targeting (--pgbouncer)
#   - Redis caching and key-value operations (--redis)
#   - Outgoing HTTP calls to external services (--http)
#   - Fast & slow requests (p50/p95 latency variations)
#   - 429 Rate Throttling bursts (--throttle)
#   - 500 Server Errors & Template waterfall crash spans (--error)
#   - Multi-DB sync & cross-database transfer workflows (--all-dbs)
# ==============================================================================

set -e

BASE_URL="${BASE_URL:-http://localhost:8002}"
MODE="loop"
DURATION=0
SLEEP_DELAY=0.3
REQUEST_GAP=0
VERBOSE=0
CONCURRENCY=1
REQUEST_COUNT=0
WORKER_DIR=""
WORKER_ID=0

# Service selection flags
BURST_MODE=0
BURST_SIZE=50
SERVICE_PRODUCT=0
SERVICE_REDIS=0
SERVICE_HTTP=0
SERVICE_TEMPLATE=0
SERVICE_SQL=0
SERVICE_THROTTLE=0
SERVICE_ERROR=0
SERVICE_BOTO=0
SERVICE_CELERY=0
SERVICE_N_PLUS_ONE=0
RUN_ALL=0

# Intentional Slowdown Flags
SLOW_DJANGO=0
SLOW_POSTGRES=0
SLOW_REDIS=0
SLOW_HTTP=0
SLOW_ALL=0
GOAL_UI=0

# Database-specific selection flags
TARGET_DB_PRIMARY=0
TARGET_DB_SLAVE1=0
TARGET_DB_SLAVE2=0
TARGET_DB_SLAVE3=0
TARGET_DB_PGBOUNCER=0
TARGET_DB_ALL=0

# Color codes
C_RESET="\033[0m"
C_BOLD="\033[1m"
C_GREEN="\033[32m"
C_YELLOW="\033[33m"
C_RED="\033[31m"
C_BLUE="\033[34m"
C_CYAN="\033[36m"
C_MAGENTA="\033[35m"

# Counters
COUNT_TOTAL=0
COUNT_2XX=0
COUNT_4XX=0
COUNT_5XX=0
START_TIME=$(date +%s)

# Parse CLI arguments
while [[ "$#" -gt 0 ]]; do
  case $1 in
  --once) MODE="once" ;;
  --loop) MODE="loop" ;;
  -d | --duration)
    DURATION="$2"
    MODE="duration"
    shift
    ;;
  -s | --sleep)
    SLEEP_DELAY="$2"
    shift
    ;;
  -g | --gap)
    REQUEST_GAP="${2:-}"
    if [[ ! "$REQUEST_GAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
      echo "--gap requires a non-negative number of seconds" >&2
      exit 1
    fi
    shift
    ;;
  -c | --concurrency)
    CONCURRENCY="${2:-}"
    if [[ ! "$CONCURRENCY" =~ ^[0-9]{1,9}$ ]] || [ "$((10#$CONCURRENCY))" -eq 0 ]; then
      echo "--concurrency requires a positive integer (maximum 999999999)" >&2
      exit 1
    fi
    CONCURRENCY=$((10#$CONCURRENCY))
    shift
    ;;
  -n | --count)
    REQUEST_COUNT="${2:-}"
    if [[ ! "$REQUEST_COUNT" =~ ^[0-9]{1,9}$ ]] || [ "$((10#$REQUEST_COUNT))" -eq 0 ]; then
      echo "--count requires a positive integer (maximum 999999999)" >&2
      exit 1
    fi
    REQUEST_COUNT=$((10#$REQUEST_COUNT))
    shift
    ;;
  -u | --url)
    BASE_URL="$2"
    shift
    ;;
  -v | --verbose) VERBOSE=1 ;;

  # Burst Cardinality Mode
  --burst | --cardinality-burst) BURST_MODE=1 ;;
  --burst-size)
    BURST_SIZE="${2:-}"
    if [[ ! "$BURST_SIZE" =~ ^[0-9]{1,9}$ ]] || [ "$((10#$BURST_SIZE))" -eq 0 ]; then
      echo "--burst-size requires a positive integer (maximum 999999999)" >&2
      exit 1
    fi
    BURST_SIZE=$((10#$BURST_SIZE))
    BURST_MODE=1
    shift
    ;;

  # Comprehensive mode (exercises all instrumentations)
  --all) RUN_ALL=1 ;;

  # Goal UI / Datadog Pattern Flag
  --goal-ui | --datadog) GOAL_UI=1 ;;

  # Intentional Slowdown / Latency Flags
  --slow-django) SLOW_DJANGO=1 ;;
  --slow-postgres | --slow-db) SLOW_POSTGRES=1 ;;
  --slow-redis) SLOW_REDIS=1 ;;
  --slow-http) SLOW_HTTP=1 ;;
  --slow-all | --slow)
    SLOW_DJANGO=1
    SLOW_POSTGRES=1
    SLOW_REDIS=1
    SLOW_HTTP=1
    SLOW_ALL=1
    ;;

  # Database specific spam flags
  --primary | --default) TARGET_DB_PRIMARY=1 ;;
  --slave1) TARGET_DB_SLAVE1=1 ;;
  --slave2) TARGET_DB_SLAVE2=1 ;;
  --slave3) TARGET_DB_SLAVE3=1 ;;
  --pgbouncer) TARGET_DB_PGBOUNCER=1 ;;
  --all-dbs | --sync) TARGET_DB_ALL=1 ;;
  --db | --database)
    case "$2" in
    default | primary) TARGET_DB_PRIMARY=1 ;;
    slave1) TARGET_DB_SLAVE1=1 ;;
    slave2) TARGET_DB_SLAVE2=1 ;;
    slave3) TARGET_DB_SLAVE3=1 ;;
    pgbouncer) TARGET_DB_PGBOUNCER=1 ;;
    all) TARGET_DB_ALL=1 ;;
    *)
      echo -e "${C_RED}Unknown database: $2 (Available: default, slave1, slave2, slave3, pgbouncer, all)${C_RESET}"
      exit 1
      ;;
    esac
    shift
    ;;

  # General service flags
  -p | --product) SERVICE_PRODUCT=1 ;;
  -r | --redis) SERVICE_REDIS=1 ;;
  -x | --http) SERVICE_HTTP=1 ;;
  -m | --template) SERVICE_TEMPLATE=1 ;;
  -a | --sql) SERVICE_SQL=1 ;;
  -k | --throttle) SERVICE_THROTTLE=1 ;;
  -y | --error) SERVICE_ERROR=1 ;;
  -b | --boto | --s3) SERVICE_BOTO=1 ;;
  -q | --celery) SERVICE_CELERY=1 ;;
  --n-plus-one | --repeated-queries) SERVICE_N_PLUS_ONE=1 ;;

  -h | --help)
    echo -e "${C_BOLD}Usage: $0 [OPTIONS]${C_RESET}"
    echo ""
    echo "Options:"
    echo "  --all             Exercise ALL instrumentations (Postgres, Slaves 1-3, Redis, Requests, S3, Celery, Django)"
    echo "  --loop            Run continuously until Ctrl+C (default)"
    echo "  --once            Run a single sweep through all endpoint scenarios"
    echo "  -d, --duration N  Run continuously for N seconds"
    echo "  -s, --sleep SEC   Delay between request cycles in seconds (default: 0.3)"
    echo "  -g, --gap SEC     Delay between individual requests in seconds (default: 0)"
    echo "  -c, --concurrency N  Run N parallel workers (default: 1)"
    echo "  -n, --count N        Send exactly N total request attempts across all workers"
    echo "                       Overrides --once, --loop and --duration (default: unlimited)"
    echo "  -u, --url URL     Base URL (default: http://localhost:8002)"
    echo "  -v, --verbose     Print response body snippets"
    echo "  -h, --help        Show this help message"
    echo ""
    echo "Intentional Component Slowdown Flags (Demonstrates Flamegraph Time-Spent Shifts):"
    echo "  --slow-django     Trigger artificial Django framework handler delays (2.0s sleep + thread delays)"
    echo "  --slow-postgres   Trigger artificial PostgreSQL query delays (pg_sleep 1.5s across DB instances)"
    echo "  --slow-redis      Trigger heavy Redis cache iteration & key scan operations"
    echo "  --slow-http       Trigger slow downstream HTTP client calls to external APIs"
    echo "  --slow-all, --slow Trigger intentional slowness across ALL components simultaneously"
    echo ""
    echo "Database-Specific Flags (Spam one DB at a time):"
    echo "  --primary         Spam PostgreSQL Primary (default via PgBouncer:6432) [Reads, Writes, Tx]"
    echo "  --slave1          Spam PostgreSQL Replica 1 (slave1 via PgBouncer:6432) [Reads, Writes, Updates]"
    echo "  --slave2          Spam PostgreSQL Replica 2 (slave2 direct) [Reads, Writes, Updates]"
    echo "  --slave3          Spam PostgreSQL Replica 3 (slave3 direct) [Reads, Writes, Updates]"
    echo "  --pgbouncer       Spam PgBouncer Pooler targets (default & slave1 databases)"
    echo "  --all-dbs         Spam cross-database sync & transfer across all 4 DBs"
    echo "  --db <name>       Target a specific DB (default, slave1, slave2, slave3, pgbouncer, all)"
    echo ""
    echo "Other Service Flags:"
    echo "  -p, --product     Spam product service endpoints"
    echo "  -r, --redis       Spam Redis cache endpoints"
    echo "  -x, --http        Spam HTTP downstream call endpoints"
    echo "  -b, --boto, --s3  Spam Boto / S3 object storage endpoints"
    echo "  -q, --celery      Spam Celery async task dispatch endpoints"
    echo "  -m, --template    Spam HTML template rendering endpoints"
    echo "  -a, --sql         Spam SQL/threaded operation endpoints"
    echo "  -k, --throttle    Spam rate throttling burst endpoints"
    echo "  -y, --error       Spam 500 error endpoint endpoints"
    echo "  --n-plus-one      Spam repeated database queries endpoint (triggers N+1 detection)"
    echo ""
    echo "Burst Cardinality Flags (Simulates High-Cardinality Production Traffic):"
    echo "  --burst, --cardinality-burst  Generate high-cardinality bursts with randomized routes,"
    echo "                                dynamic SQL queries, multi-DB targets, tenants, and status codes"
    echo "  --burst-size N                Number of unique randomized requests per burst cycle (default: 50)"
    echo ""
    exit 0
    ;;
  *)
    echo "Unknown parameter: $1"
    exit 1
    ;;
  esac
  shift
done

if [ "$REQUEST_COUNT" -gt 0 ]; then
  MODE="count"
fi

# Check if any specific database target or slow filter was selected
HAS_DB_FILTER=0
if [ "$TARGET_DB_PRIMARY" -eq 1 ] || [ "$TARGET_DB_SLAVE1" -eq 1 ] || [ "$TARGET_DB_SLAVE2" -eq 1 ] || [ "$TARGET_DB_SLAVE3" -eq 1 ] || [ "$TARGET_DB_PGBOUNCER" -eq 1 ] || [ "$TARGET_DB_ALL" -eq 1 ]; then
  HAS_DB_FILTER=1
fi

HAS_SLOW_FILTER=0
if [ "$SLOW_DJANGO" -eq 1 ] || [ "$SLOW_POSTGRES" -eq 1 ] || [ "$SLOW_REDIS" -eq 1 ] || [ "$SLOW_HTTP" -eq 1 ]; then
  HAS_SLOW_FILTER=1
fi

# Print banner
echo -e "${C_CYAN}${C_BOLD}"
echo "================================================================="
echo "   🚀  APM Observability Traffic Generator"
echo "   Target Base URL: ${BASE_URL}"
echo "   Mode: ${MODE} $([ $DURATION -gt 0 ] && echo "(for ${DURATION}s)")"
echo "   Concurrency: ${CONCURRENCY} worker(s)"
if [ -n "$REQUEST_GAP" ] && ! [[ "$REQUEST_GAP" =~ ^0*(\.0*)?$ ]]; then
  echo "   Request Gap: ${REQUEST_GAP}s"
fi
if [ "$RUN_ALL" -eq 1 ]; then
  echo "   Scope: ALL Instrumentations (Postgres Primary & Slaves 1-3, Redis, Requests HTTP, S3, Celery, Django)"
fi
if [ "$BURST_MODE" -eq 1 ]; then
  echo "   Burst Mode: HIGH CARDINALITY (${BURST_SIZE} diverse requests/cycle across routes, SQL, multi-DB, tenants)"
fi
if [ "$HAS_DB_FILTER" -eq 1 ]; then
  echo -n "   Target Database(s): "
  [ "$TARGET_DB_PRIMARY" -eq 1 ] && echo -n "[Primary (PgBouncer)] "
  [ "$TARGET_DB_SLAVE1" -eq 1 ] && echo -n "[slave1 (PgBouncer)] "
  [ "$TARGET_DB_SLAVE2" -eq 1 ] && echo -n "[slave2 (Direct)] "
  [ "$TARGET_DB_SLAVE3" -eq 1 ] && echo -n "[slave3 (Direct)] "
  [ "$TARGET_DB_PGBOUNCER" -eq 1 ] && echo -n "[PgBouncer (default + slave1)] "
  [ "$TARGET_DB_ALL" -eq 1 ] && echo -n "[All Databases Synced] "
  echo ""
fi
if [ "$HAS_SLOW_FILTER" -eq 1 ]; then
  echo -n "   Intentional Slowdown: "
  [ "$SLOW_DJANGO" -eq 1 ] && echo -n "[Django (2.0s Handler Sleep)] "
  [ "$SLOW_POSTGRES" -eq 1 ] && echo -n "[PostgreSQL (pg_sleep 1.5s)] "
  [ "$SLOW_REDIS" -eq 1 ] && echo -n "[Redis (Heavy Keys Iteration)] "
  [ "$SLOW_HTTP" -eq 1 ] && echo -n "[HTTP Client (Slow Downstream)] "
  echo ""
fi
echo "=================================================================${C_RESET}"

# Cleanup on exit
trap cleanup INT TERM

cleanup() {
  END_TIME=$(date +%s)
  ELAPSED=$((END_TIME - START_TIME))
  [ $ELAPSED -eq 0 ] && ELAPSED=1
  RPS=$(awk "BEGIN {printf \"%.2f\", $COUNT_TOTAL / $ELAPSED}")

  echo -e "\n${C_BOLD}${C_CYAN}=================================================================${C_RESET}"
  echo -e "${C_BOLD}📊 Traffic Generation Summary:${C_RESET}"
  echo -e "   Total Requests: ${C_BOLD}${COUNT_TOTAL}${C_RESET} (${RPS} req/s over ${ELAPSED}s)"
  echo -e "   ${C_GREEN}✔ 2xx Successes:${C_RESET}  ${COUNT_2XX}"
  echo -e "   ${C_BLUE}ℹ 4xx Throttled:${C_RESET}  ${COUNT_4XX}"
  echo -e "   ${C_RED}✖ 5xx Errors:${C_RESET}     ${COUNT_5XX}"
  echo -e "${C_BOLD}${C_CYAN}=================================================================${C_RESET}"
  [ -n "$WORKER_DIR" ] && rm -rf "$WORKER_DIR"
  exit 0
}

# Helper function to send an HTTP request
send_req() {
  # Enforce each worker's budget before curl, including mid-scenario sweeps.
  if [ "$REQUEST_COUNT" -gt 0 ] && [ "$COUNT_TOTAL" -ge "$REQUEST_COUNT" ]; then
    return 0
  fi

  local method="$1"
  local path="$2"
  local data="$3"
  local desc="$4"
  local extra_header="${5:-}"
  local url="${BASE_URL}${path}"

  COUNT_TOTAL=$((COUNT_TOTAL + 1))

  local response
  local http_code
  local body

  local -a curl_cmd=(curl -s -w "\n%{http_code}" -X "$method" "$url" --max-time 10)
  if [ -n "$extra_header" ]; then
    curl_cmd+=(-H "$extra_header")
  fi
  if [ "$method" == "POST" ] || [ "$method" == "PUT" ] || [ "$method" == "PATCH" ]; then
    curl_cmd+=(-H "Content-Type: application/json" -d "$data")
  fi

  response=$("${curl_cmd[@]}" 2>/dev/null || echo "000")

  http_code=$(echo "$response" | tail -n1 | tr -d '\r')
  if [ -z "$http_code" ] || [ "${#http_code}" -ne 3 ]; then
    http_code="000"
  fi
  body=$(echo "$response" | sed '$d')

  local code_color="${C_GREEN}"
  if [[ "$http_code" =~ ^2 ]]; then
    COUNT_2XX=$((COUNT_2XX + 1))
    code_color="${C_GREEN}"
  elif [[ "$http_code" =~ ^4 ]]; then
    COUNT_4XX=$((COUNT_4XX + 1))
    code_color="${C_BLUE}"
  else
    COUNT_5XX=$((COUNT_5XX + 1))
    code_color="${C_RED}"
  fi

  printf " [%s%04d] %-6s %-36s -> ${code_color}%-3s${C_RESET} (%s)\n" \
    "$([ "$WORKER_ID" -gt 0 ] && echo "W${WORKER_ID}:" || echo "")" "$COUNT_TOTAL" "$method" "$path" "$http_code" "$desc"

  if [ "$VERBOSE" -eq 1 ] && [ -n "$body" ]; then
    echo -e "        ${C_YELLOW}Response:${C_RESET} $(echo "$body" | head -c 120)..."
  fi

  if [ -n "$REQUEST_GAP" ] && ! [[ "$REQUEST_GAP" =~ ^0*(\.0*)?$ ]]; then
    if [ "$REQUEST_COUNT" -eq 0 ] || [ "$COUNT_TOTAL" -lt "$REQUEST_COUNT" ]; then
      sleep "$REQUEST_GAP"
    fi
  fi
}

# Run a high-cardinality burst cycle stressing routes, queries, DBs, and status codes
run_burst_cycle() {
  local cycle_num="$1"
  echo -e "\n${C_BOLD}${C_MAGENTA}--- Burst Cycle #${cycle_num} (${BURST_SIZE} diverse requests) ---${C_RESET}"

  local partials=("_header" "_footer" "_product_card" "_stock_badge" "_sidebar")
  local dbs=("default" "slave1" "slave2" "slave3")
  local s3_ops=("list" "put" "get" "head")

  for ((i = 1; i <= BURST_SIZE; i++)); do
    if [ "$REQUEST_COUNT" -gt 0 ] && [ "$COUNT_TOTAL" -ge "$REQUEST_COUNT" ]; then
      break
    fi

    local rnd=$((RANDOM % 9000 + 1000))
    local tenant="X-Tenant-ID: tenant-$((RANDOM % 50 + 1))"
    local sel=$(( (i + RANDOM) % 12 ))

    case $sel in
    0)
      # Dynamic Product Detail (200 / 404 variation)
      local pid=$((RANDOM % 300 + 1))
      send_req "GET" "/api/products/${pid}/" "" "Burst: Product Detail #${pid}" "$tenant"
      ;;
    1)
      # Redis Cached Product Lookup
      local pid=$((RANDOM % 150 + 1))
      send_req "GET" "/api/products/${pid}/cached/" "" "Burst: Redis Cache Product #${pid}" "$tenant"
      ;;
    2)
      # Atomic Stock Adjustment (DB Write + Lock)
      local pid=$((RANDOM % 100 + 1))
      local delta=$((RANDOM % 7 - 3))
      send_req "POST" "/api/products/${pid}/adjust_stock/" "{\"delta\": ${delta}}" "Burst: Adjust Stock #${pid} (${delta})" "$tenant"
      ;;
    3)
      # HTML Template Waterfall or Dynamic Partials
      if [ $((RANDOM % 2)) -eq 0 ]; then
        local pid=$((RANDOM % 100 + 1))
        send_req "GET" "/api/products-tmpl/${pid}/" "" "Burst: HTML Template #${pid}" "$tenant"
      else
        local pidx=$((RANDOM % ${#partials[@]}))
        send_req "GET" "/api/partial/${partials[$pidx]}/" "" "Burst: HTML Partial ${partials[$pidx]}" "$tenant"
      fi
      ;;
    4)
      # Multi-DB Read Queries (Distributed Across All 4 Databases)
      local db_idx=$((RANDOM % ${#dbs[@]}))
      local target_db="${dbs[$db_idx]}"
      send_req "GET" "/api/multi-db/?db=${target_db}&burst_seed=${rnd}" "" "Burst: Multi-DB Read [${target_db}]" "$tenant"
      ;;
    5)
      # Multi-DB Writes & Updates (Distributed Across Replicas & Primary)
      local db_idx=$((RANDOM % ${#dbs[@]}))
      local target_db="${dbs[$db_idx]}"
      if [ $((RANDOM % 2)) -eq 0 ]; then
        send_req "POST" "/api/multi-db/?db=${target_db}&action=insert" "" "Burst: Multi-DB Insert [${target_db}]" "$tenant"
      else
        send_req "POST" "/api/multi-db/?db=${target_db}&action=update" "" "Burst: Multi-DB Update [${target_db}]" "$tenant"
      fi
      ;;
    6)
      # Cross-Database Synchronization & Stock Transfer
      if [ $((RANDOM % 2)) -eq 0 ]; then
        send_req "POST" "/api/products/sync-all-dbs/" "{\"name\": \"BurstOmni-${rnd}\", \"price\": \"79.99\", \"stock\": 100}" "Burst: 4-DB Sync Omni-Write" "$tenant"
      else
        local transfer_amt=$((RANDOM % 5 + 1))
        send_req "POST" "/api/products/multi-db-transfer/" "{\"amount\": ${transfer_amt}}" "Burst: Cross-DB Transfer (${transfer_amt} units)" "$tenant"
      fi
      ;;
    7)
      # Randomized Unique Raw SQL Queries (Produces unique SQL statements / entropy)
      local sql_choice=$((RANDOM % 4))
      case $sql_choice in
      0) send_req "GET" "/api/raw-sql/?query=SELECT%20${rnd}%20AS%20burst_entropy_${rnd}" "" "Burst: Raw SQL [Entropy #${rnd}]" "$tenant" ;;
      1) send_req "GET" "/api/raw-sql/?query=SELECT%20id,%20name,%20stock%20FROM%20api_product%20WHERE%20id%3E$((RANDOM % 50))%20LIMIT%205" "" "Burst: Raw SQL [Filtered Scan]" "$tenant" ;;
      2) send_req "GET" "/api/raw-sql/?query=SELECT%20COUNT(*)%20FROM%20api_product%20WHERE%20stock%3E$((RANDOM % 20))" "" "Burst: Raw SQL [Aggregate Query]" "$tenant" ;;
      3) send_req "GET" "/api/raw-sql/?query=SELECT%20pg_backend_pid(),%20NOW(),%20${rnd}" "" "Burst: Raw SQL [Backend Info #${rnd}]" "$tenant" ;;
      esac
      ;;
    8)
      # Repeated Queries (Simulated N+1 Pattern with Dynamic Count)
      local n_count=$((RANDOM % 25 + 5))
      send_req "GET" "/api/repeated-queries/?count=${n_count}" "" "Burst: N+1 Repeated Queries (${n_count} DB loops)" "$tenant"
      ;;
    9)
      # Asynchronous Celery Dispatch & S3 Storage Operations
      if [ $((RANDOM % 2)) -eq 0 ]; then
        send_req "POST" "/api/celery/publish/" "{\"value\": ${rnd}, \"fail\": false}" "Burst: Celery Async Task [value=${rnd}]" "$tenant"
      else
        local op_idx=$((RANDOM % ${#s3_ops[@]}))
        send_req "GET" "/api/s3-storage/?op=${s3_ops[$op_idx]}" "" "Burst: MinIO S3 [${s3_ops[$op_idx]}]" "$tenant"
      fi
      ;;
    10)
      # Product Entity Lifecycle & Multi-Statement Transactions
      if [ $((RANDOM % 2)) -eq 0 ]; then
        local p_price="$((RANDOM % 80 + 10)).$((RANDOM % 90 + 10))"
        send_req "POST" "/api/products/" "{\"name\": \"BurstProd-${rnd}\", \"price\": \"${p_price}\", \"stock\": $((RANDOM % 50 + 1))}" "Burst: Create Product [${p_price}]" "$tenant"
      else
        local tx_ops=$((RANDOM % 4 + 2))
        send_req "POST" "/api/db-tx/" "{\"operations\": ${tx_ops}}" "Burst: DB Tx (${tx_ops} statements)" "$tenant"
      fi
      ;;
    11)
      # Edge Cases & Status Code Diversity (4xx, 5xx, 429, 404, 405)
      local edge_choice=$((RANDOM % 5))
      case $edge_choice in
      0) send_req "GET" "/api/products/throttled/" "" "Burst Probe: 429 Throttle Endpoint" "$tenant" ;;
      1) send_req "GET" "/api/products/error/" "" "Burst Probe: 500 Deliberate Server Error" "$tenant" ;;
      2) send_req "GET" "/api/products/$((90000 + RANDOM % 10000))/" "" "Burst Probe: 404 Unmatched Product" "$tenant" ;;
      3) send_req "GET" "/api/webhook/" "" "Burst Probe: 405 Method Not Allowed" "$tenant" ;;
      4) send_req "POST" "/api/multi-db/?db=nonexistent_db_${rnd}" "" "Burst Probe: 400 Invalid DB Parameter" "$tenant" ;;
      esac
      ;;
    esac
  done
}

# Run one full traffic cycle with diverse scenarios
run_traffic_cycle() {
  local cycle_num="$1"

  # =========================================================================
  # High-Cardinality Burst Mode (Dynamic routes, SQL queries, multi-DB, tenants)
  # =========================================================================
  if [ "$BURST_MODE" -eq 1 ]; then
    run_burst_cycle "$cycle_num"
    return
  fi

  echo -e "\n${C_BOLD}${C_MAGENTA}--- Cycle #${cycle_num} ---${C_RESET}"

  local rnd=$((RANDOM % 900 + 100))

  # =========================================================================
  # Goal UI / Datadog Pattern Mode (49% Postgres, 30% Django, 21% Requests/Redis)
  # =========================================================================
  if [ "$GOAL_UI" -eq 1 ]; then
    echo -e "   ${C_CYAN}🎯 Goal UI Pattern: PostgreSQL (~49%), Django (~30%), Downstream (~21%)...${C_RESET}"
    send_req "GET" "/api/products/postgres-slow/?delay=0.06" "" "Postgres Primary: pg_sleep(0.06s) Query (49% Time)"
    send_req "GET" "/api/products/" "" "Django: Products List Handler (30% Time)"
    # send_req "GET" "/api/products/external/" "" "HTTP Client: External Downstream Call (21% Time)"
    return
  fi

  # =========================================================================
  # If intentional slow filters are passed, trigger targeted component slowness
  # =========================================================================
  if [ "$HAS_SLOW_FILTER" -eq 1 ]; then
    if [ "$SLOW_DJANGO" -eq 1 ]; then
      echo -e "   ${C_YELLOW}🐌 Intentional Slowdown: Django Web Handler Latency (2.0s sleep)...${C_RESET}"
      send_req "GET" "/api/products/slow/" "" "Django Slow Handler (2.0s artificial sleep)"
      send_req "GET" "/api/threaded/?threads=5&delay=0.3" "" "Django Threaded Work (5 threads x 0.3s)"
      send_req "GET" "/api/products-tmpl/" "" "Django HTML Template Rendering Waterfall"
    fi

    if [ "$SLOW_POSTGRES" -eq 1 ]; then
      echo -e "   ${C_YELLOW}🐢 Intentional Slowdown: PostgreSQL Database Query Delay (pg_sleep 1.5s)...${C_RESET}"
      send_req "GET" "/api/products/postgres-slow/?delay=1.5" "" "Postgres Primary: pg_sleep(1.5s) SQL Query"
      send_req "POST" "/api/multi-db/?db=slave1&action=insert" "" "Postgres Slave1: Insert Record"
      send_req "GET" "/api/products/postgres-slow/?db=slave1&delay=1.0" "" "Postgres Slave1: pg_sleep(1.0s) DB Info"
      send_req "POST" "/api/db-tx/" "{\"operations\": 10}" "Postgres Primary: Heavy 10-Statement DB Transaction"
    fi

    if [ "$SLOW_REDIS" -eq 1 ]; then
      echo -e "   ${C_YELLOW}⚡ Intentional Slowdown: Redis Cache Latency (5.0s delay)...${C_RESET}"
      send_req "GET" "/api/products/redis-slow/?delay=5.0" "" "Redis Slow Operation (5.0s artificial sleep)"
      send_req "GET" "/api/cache-stats/" "" "Redis: Key Scan & Cache Backend Stats"
    fi

    if [ "$SLOW_HTTP" -eq 1 ]; then
      echo -e "   ${C_YELLOW}🌐 Intentional Slowdown: Downstream HTTP Client Latency (1.5s timeout)...${C_RESET}"
      # send_req "GET" "/api/products/external/" "" "HTTP Client: Outgoing Request to External API"
      send_req "GET" "/api/s3-storage/" "" "Cloud Storage: S3/Boto Object Storage Call"
    fi

    return
  fi

  # =========================================================================
  # If specific DB filters are passed, only spam the targeted database(s)
  # =========================================================================
  if [ "$HAS_DB_FILTER" -eq 1 ]; then
    if [ "$TARGET_DB_PRIMARY" -eq 1 ]; then
      echo -e "   ${C_BLUE}🔹 Spamming PostgreSQL Primary (default via PgBouncer)...${C_RESET}"
      send_req "GET" "/api/products/" "" "Postgres Primary (PgBouncer): List Products"
      send_req "GET" "/api/products/health/" "" "HEALTH"
      send_req "GET" "/api/products/1/" "" "Postgres Primary (PgBouncer): Product Detail #1"
      send_req "POST" "/api/products/" "{\"name\": \"Primary-Widget-$rnd\", \"price\": \"29.99\", \"stock\": 10}" "Postgres Primary (PgBouncer): Create Product"
      send_req "POST" "/api/products/bulk_create/" "[{\"name\":\"PrimaryA-$rnd\",\"price\":\"15.00\",\"stock\":5}]" "Postgres Primary (PgBouncer): Bulk Create"
      send_req "POST" "/api/products/1/adjust_stock/" "{\"delta\": -1}" "Postgres Primary (PgBouncer): Stock Adjustment"
      send_req "POST" "/api/db-tx/" "{\"operations\": 3}" "Postgres Primary (PgBouncer): Multi-Statement Tx"
      send_req "POST" "/api/multi-db/?db=default&action=update" "" "Postgres Primary (PgBouncer): Raw UPDATE"
      send_req "GET" "/api/multi-db/?db=default" "" "Postgres Primary (PgBouncer): Raw SELECT"
    fi

    if [ "$TARGET_DB_SLAVE1" -eq 1 ]; then
      echo -e "   ${C_BLUE}🔹 Spamming PostgreSQL Replica 1 (slave1 via PgBouncer)...${C_RESET}"
      send_req "GET" "/api/products/read-slave1/" "" "Postgres slave1 (PgBouncer): Read Products"
      send_req "POST" "/api/products/write-slave1/" "{\"name\": \"Item-Slave1-$rnd\", \"price\": \"19.99\", \"stock\": 15}" "Postgres slave1 (PgBouncer): Direct Write"
      send_req "POST" "/api/multi-db/?db=slave1&action=insert" "" "Postgres slave1 (PgBouncer): Raw INSERT"
      send_req "POST" "/api/multi-db/?db=slave1&action=update" "" "Postgres slave1 (PgBouncer): Raw UPDATE"
      send_req "GET" "/api/multi-db/?db=slave1" "" "Postgres slave1 (PgBouncer): Raw SELECT"
    fi

    if [ "$TARGET_DB_PGBOUNCER" -eq 1 ]; then
      echo -e "   ${C_YELLOW}⚡ Spamming PgBouncer Pooler Targets (default & slave1 DBs)...${C_RESET}"
      send_req "GET" "/api/products/" "" "PgBouncer [default]: List Products"
      send_req "GET" "/api/products/read-slave1/" "" "PgBouncer [slave1]: Read Products"
      send_req "POST" "/api/products/write-slave1/" "{\"name\": \"Item-Slave1-$rnd\", \"price\": \"19.99\", \"stock\": 15}" "PgBouncer [slave1]: Direct Write"
      send_req "POST" "/api/products/1/adjust_stock/" "{\"delta\": -1}" "PgBouncer [default]: Stock Adjustment"
      send_req "POST" "/api/multi-db/?db=slave1&action=update" "" "PgBouncer [slave1]: Raw UPDATE"
      send_req "GET" "/api/multi-db/?db=default" "" "PgBouncer [default]: Raw SELECT"
    fi

    if [ "$TARGET_DB_SLAVE2" -eq 1 ]; then
      echo -e "   ${C_BLUE}🔹 Spamming PostgreSQL Replica 2 (slave2 direct)...${C_RESET}"
      send_req "GET" "/api/products/read-slave2/" "" "Postgres slave2 (Direct): Read Products"
      send_req "POST" "/api/products/write-slave2/" "{\"name\": \"Item-Slave2-$rnd\", \"price\": \"39.99\", \"stock\": 30}" "Postgres slave2 (Direct): Direct Write"
      send_req "POST" "/api/multi-db/?db=slave2&action=insert" "" "Postgres slave2 (Direct): Raw INSERT"
      send_req "POST" "/api/multi-db/?db=slave2&action=update" "" "Postgres slave2 (Direct): Raw UPDATE"
      send_req "GET" "/api/multi-db/?db=slave2" "" "Postgres slave2 (Direct): Raw SELECT"
    fi

    if [ "$TARGET_DB_SLAVE3" -eq 1 ]; then
      echo -e "   ${C_BLUE}🔹 Spamming PostgreSQL Replica 3 (slave3 direct)...${C_RESET}"
      send_req "GET" "/api/products/read-slave3/" "" "Postgres slave3 (Direct): Read Products"
      send_req "POST" "/api/products/write-slave3/" "{\"name\": \"Item-Slave3-$rnd\", \"price\": \"89.99\", \"stock\": 45}" "Postgres slave3 (Direct): Direct Write"
      send_req "POST" "/api/multi-db/?db=slave3&action=insert" "" "Postgres slave3 (Direct): Raw INSERT"
      send_req "POST" "/api/multi-db/?db=slave3&action=update" "" "Postgres slave3 (Direct): Raw UPDATE"
      send_req "GET" "/api/multi-db/?db=slave3" "" "Postgres slave3 (Direct): Raw SELECT"
    fi

    if [ "$TARGET_DB_ALL" -eq 1 ]; then
      echo -e "   ${C_MAGENTA}🌀 Spamming Cross-Database Synchronization across All 4 DBs...${C_RESET}"
      send_req "POST" "/api/products/sync-all-dbs/" "{\"name\": \"Synced-$rnd\", \"price\": \"99.00\", \"stock\": 100}" "Multi-DB: Sync Across All 4 Databases"
      send_req "POST" "/api/products/multi-db-transfer/" "{\"amount\": 3}" "Multi-DB: Cross-DB Stock Transfer & Audit"
    fi

    return
  fi

  # =========================================================================
  # Standard Full Traffic Cycle (Balanced across all services and tiers)
  # =========================================================================
  local no_service_selected=0
  if [ "$SERVICE_REDIS" -eq 0 ] && [ "$SERVICE_HTTP" -eq 0 ] && [ "$SERVICE_TEMPLATE" -eq 0 ] && \
     [ "$SERVICE_SQL" -eq 0 ] && [ "$SERVICE_THROTTLE" -eq 0 ] && [ "$SERVICE_ERROR" -eq 0 ] && \
     [ "$SERVICE_BOTO" -eq 0 ] && [ "$SERVICE_CELERY" -eq 0 ] && [ "$SERVICE_N_PLUS_ONE" -eq 0 ]; then
    no_service_selected=1
  fi

  local full_cycle_ran=0
  if [ "$RUN_ALL" -eq 1 ] || [ "$SERVICE_PRODUCT" -eq 1 ] || [ "$no_service_selected" -eq 1 ]; then
    full_cycle_ran=1
    # 1. Django Web & Core Framework Tier
    send_req "GET" "/api/products/" "" "Django Web: List Products (Core Handler)"
    send_req "GET" "/api/products/1/" "" "Django Web: Product Detail #1"
    send_req "GET" "/api/products-tmpl/" "" "Django Web: Template Rendering Waterfall"
    send_req "GET" "/api/threaded/?threads=2&delay=0.02" "" "Django Web: Multi-Threaded Task"

    # 2. PostgreSQL Primary (default) & PgBouncer Connection Pooler
    send_req "GET" "/api/multi-db/?db=default" "" "Postgres Primary: Direct SELECT Query"
    send_req "POST" "/api/products/1/adjust_stock/" "{\"delta\": -1}" "PgBouncer: Stock Adjustment on Primary"
    send_req "POST" "/api/db-tx/" "{\"operations\": 2}" "PgBouncer: 2-Statement Transaction"

    # 3. PostgreSQL Read Replicas (Evenly Distributed Across Slaves 1, 2, 3)
    send_req "GET" "/api/products/read-slave1/" "" "Postgres Replica 1 (slave1 via PgBouncer)"
    send_req "GET" "/api/products/read-slave2/" "" "Postgres Replica 2 (slave2 Direct - No PgBouncer)"
    send_req "GET" "/api/products/read-slave3/" "" "Postgres Replica 3 (slave3 Direct - No PgBouncer)"

    # 4. PostgreSQL Replica Writes (Evenly Distributed)
    send_req "POST" "/api/products/write-slave1/" "{\"name\": \"Item-Slave1-$rnd\", \"price\": \"19.99\", \"stock\": 15}" "Postgres Slave 1: Write Record"
    send_req "POST" "/api/products/write-slave2/" "{\"name\": \"Item-Slave2-$rnd\", \"price\": \"39.99\", \"stock\": 30}" "Postgres Slave 2: Direct Write (No PgBouncer)"
    send_req "POST" "/api/products/write-slave3/" "{\"name\": \"Item-Slave3-$rnd\", \"price\": \"89.99\", \"stock\": 45}" "Postgres Slave 3: Direct Write (No PgBouncer)"

    # 5. Redis Cache Tier (Fast, Clean Operations - No Blocking Lua Scripts)
    send_req "GET" "/api/products/cache/" "" "Redis Cache: Get/Set Products Cache"
    send_req "GET" "/api/cache-stats/" "" "Redis Cache: Metadata & Key Statistics"
    send_req "GET" "/api/products/health/" "" "Infrastructure Health: All 4 Postgres DBs + Redis"

    # 6. Cloud Storage & Downstream
    send_req "GET" "/api/s3-storage/" "" "Cloud Storage (Boto/S3): List Bucket Objects"

    # 7. Celery Asynchronous Tasks (Producer -> Broker -> Worker Consumer)
    send_req "POST" "/api/celery/publish/" "{\"value\": $rnd, \"fail\": false}" "Celery Async: Dispatch demo_task (Producer -> Consumer)"

    # 8. Repeated Database Queries (Potential N+1 Pattern)
    if [ $((cycle_num % 3)) -eq 0 ] || [ "$SERVICE_N_PLUS_ONE" -eq 1 ]; then
      send_req "GET" "/api/repeated-queries/?count=20" "" "Repeated Queries: Simulated N+1 ORM Loop (20 DB queries)"
    fi

    # 9. Balanced Error & Throttling (Realistic 2-5% rate instead of constant spam)
    if [ $((cycle_num % 5)) -eq 0 ]; then
      send_req "GET" "/api/products/throttled/" "" "Rate Throttling: Occasional 429 Test Probe"
    fi
    if [ $((cycle_num % 10)) -eq 0 ]; then
      send_req "GET" "/api/products/error/" "" "Server Error: Occasional 500 Test Probe"
      send_req "POST" "/api/celery/publish/" "{\"value\": $rnd, \"fail\": true}" "Celery Error: Occasional Failing Task Probe"
    fi
  fi

  # Template endpoints
  if [ "$SERVICE_TEMPLATE" -eq 1 ]; then
    send_req "GET" "/api/products-tmpl/" "" "HTML: Product List Template"
    send_req "GET" "/api/partial/_header/" "" "HTML Partial: Header"
  fi

  # SQL/Threaded endpoints
  if [ "$SERVICE_SQL" -eq 1 ]; then
    send_req "GET" "/api/threaded/?threads=3&delay=0.05" "" "Threads: 3 Parallel Tasks"
    send_req "GET" "/api/raw-sql/?query=SELECT%20COUNT(*)%20FROM%20api_product" "" "Raw SQL: Direct Query"
    send_req "GET" "/api/manual-span/" "" "OTel: Custom Manual Span"
  fi

  # HTTP downstream endpoints
  if [ "$SERVICE_HTTP" -eq 1 ]; then
    send_req "GET" "/api/products/external/" "" "HTTP Client: Downstream Call"
  fi

  # Redis endpoints
  if [ "$SERVICE_REDIS" -eq 1 ]; then
    send_req "GET" "/api/products/cache/" "" "Redis: Cache Get/Set"
    send_req "GET" "/api/cache-stats/" "" "Redis: Cache Backend Stats"
  fi

  # Boto / S3 Cloud Storage endpoints
  if [ "$SERVICE_BOTO" -eq 1 ]; then
    send_req "GET" "/api/s3-storage/" "" "Cloud Storage (Boto/S3): List Objects"
  fi

  # Celery async task endpoints
  if [ "$SERVICE_CELERY" -eq 1 ]; then
    send_req "POST" "/api/celery/publish/" "{\"value\": $rnd, \"fail\": false}" "Celery: Dispatch demo_task (Producer -> Consumer)"
    if [ $((cycle_num % 4)) -eq 0 ]; then
      send_req "POST" "/api/celery/publish/" "{\"value\": $rnd, \"fail\": true}" "Celery: Dispatch failing demo_task (ValueError)"
    fi
  fi

  # Repeated Database Queries (N+1) standalone
  if [ "$SERVICE_N_PLUS_ONE" -eq 1 ] && [ "$full_cycle_ran" -eq 0 ]; then
    send_req "GET" "/api/repeated-queries/?count=20" "" "Repeated Queries: Simulated N+1 ORM Loop (20 DB queries)"
  fi
}

# Worker loop function (used by concurrent workers)
run_worker_loop() {
  local wid="$1"
  local worker_counts_file="${WORKER_DIR}/counts_${wid}"

  worker_save_counts() {
    echo "${COUNT_TOTAL} ${COUNT_2XX} ${COUNT_4XX} ${COUNT_5XX}" >"$worker_counts_file"
  }

  trap 'worker_save_counts; exit 0' INT TERM
  trap 'worker_save_counts' EXIT

  local cycle=1
  while true; do
    run_traffic_cycle "$cycle" 2>/dev/null

    [ "$MODE" == "once" ] && break

    if [ "$REQUEST_COUNT" -gt 0 ] && [ "$COUNT_TOTAL" -ge "$REQUEST_COUNT" ]; then
      break
    fi

    if [ "$MODE" == "duration" ]; then
      local now
      now=$(date +%s)
      [ $((now - START_TIME)) -ge "$DURATION" ] && break
    fi

    cycle=$((cycle + 1))
    sleep "$SLEEP_DELAY"
  done

  worker_save_counts
}

# Main Execution Loop
if [ "$CONCURRENCY" -gt 1 ]; then
  # Concurrent mode: launch N parallel workers
  WORKER_DIR=$(mktemp -d)
  trap 'kill 0; wait; cleanup' INT TERM

  echo -e "   ${C_YELLOW}⚡ Launching ${CONCURRENCY} concurrent workers...${C_RESET}"

  for i in $(seq 1 "$CONCURRENCY"); do
    if [ "$REQUEST_COUNT" -gt 0 ]; then
      worker_budget=$((REQUEST_COUNT / CONCURRENCY))
      if [ "$i" -le "$((REQUEST_COUNT % CONCURRENCY))" ]; then
        worker_budget=$((worker_budget + 1))
      fi
      [ "$worker_budget" -eq 0 ] && continue
      WORKER_ID=$i REQUEST_COUNT=$worker_budget REQUEST_GAP=$REQUEST_GAP run_worker_loop "$i" &
    else
      WORKER_ID=$i REQUEST_GAP=$REQUEST_GAP run_worker_loop "$i" &
    fi
  done

  wait

  # Aggregate counts from all workers
  COUNT_TOTAL=0
  COUNT_2XX=0
  COUNT_4XX=0
  COUNT_5XX=0
  for f in "${WORKER_DIR}"/counts_*; do
    [ -f "$f" ] || continue
    read -r wt w2 w4 w5 <"$f"
    COUNT_TOTAL=$((COUNT_TOTAL + wt))
    COUNT_2XX=$((COUNT_2XX + w2))
    COUNT_4XX=$((COUNT_4XX + w4))
    COUNT_5XX=$((COUNT_5XX + w5))
  done

  cleanup
else
  # Single worker mode (original behavior)
  cycle=1
  while true; do
    run_traffic_cycle "$cycle"

    if [ "$MODE" == "once" ]; then
      break
    fi

    if [ "$REQUEST_COUNT" -gt 0 ] && [ "$COUNT_TOTAL" -ge "$REQUEST_COUNT" ]; then
      break
    fi

    if [ "$MODE" == "duration" ]; then
      CURRENT_TIME=$(date +%s)
      if [ $((CURRENT_TIME - START_TIME)) -ge "$DURATION" ]; then
        break
      fi
    fi

    cycle=$((cycle + 1))
    sleep "$SLEEP_DELAY"
  done

  cleanup
fi
