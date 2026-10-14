#!/usr/bin/env bash
# ==============================================================================
# query_slave2.sh - Execute Direct Query on PostgreSQL Slave 2 (Bypassing PgBouncer)
# ==============================================================================
# This script sends queries directly to the postgres-slave2 database (slave2db:5432),
# completely bypassing PgBouncer (which only proxies primary & slave1 on port 6432).
#
# It automatically fetches the generated OpenTelemetry Trace ID from Tempo and
# provides direct links to inspect the trace in Grafana and Tempo.
#
# Usage:
#   ./query_slave2.sh [select|insert|read]
#   ./query_slave2.sh select   # (Default) Raw SELECT on slave2 database
#   ./query_slave2.sh insert   # Raw INSERT on slave2 database
#   ./query_slave2.sh read     # ORM Products Read directly from slave2
# ==============================================================================

set -eo pipefail

APP_URL="${APP_URL:-http://localhost:8001}"
TEMPO_URL="${TEMPO_URL:-http://localhost:3200}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"

ACTION="${1:-select}"

# Colors
C_RESET="\033[0m"
C_CYAN="\033[1;36m"
C_GREEN="\033[1;32m"
C_YELLOW="\033[1;33m"
C_BLUE="\033[1;34m"
C_MAGENTA="\033[1;35m"

echo -e "${C_CYAN}======================================================================${C_RESET}"
echo -e "${C_CYAN}  PostgreSQL Slave 2 Direct Query & Trace Inspection${C_RESET}"
echo -e "${C_CYAN}======================================================================${C_RESET}"
echo -e "Database Target:  ${C_GREEN}slave2db:5432${C_RESET} (Direct PostgreSQL - NO PgBouncer)"
echo -e "Connection Port:  ${C_GREEN}5432${C_RESET} (PgBouncer runs on 6432 - Bypassed)"
echo -e "Action:           ${C_YELLOW}${ACTION}${C_RESET}"
echo ""

TIMESTAMP_BEFORE=$(date +%s%N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1e9))')

case "${ACTION}" in
  insert)
    echo -e "${C_BLUE}Sending Direct INSERT query to Slave 2...${C_RESET}"
    RESPONSE=$(curl -s -X POST "${APP_URL}/api/multi-db/?db=slave2&action=insert")
    echo -e "Response: ${RESPONSE}"
    OPERATION="INSERT"
    ;;
  read)
    echo -e "${C_BLUE}Sending ORM Read query to Slave 2 (/api/products/read-slave2/)...${C_RESET}"
    RESPONSE=$(curl -s "${APP_URL}/api/products/read-slave2/")
    echo -e "Response: $(echo "${RESPONSE}" | cut -c 1-120)..."
    OPERATION="GET /api/products/read-slave2/"
    ;;
  select|*)
    echo -e "${C_BLUE}Sending Direct SELECT query to Slave 2 (/api/multi-db/?db=slave2)...${C_RESET}"
    RESPONSE=$(curl -s "${APP_URL}/api/multi-db/?db=slave2")
    echo -e "Response: $(echo "${RESPONSE}" | cut -c 1-120)..."
    OPERATION="SELECT"
    ;;
esac

echo ""
echo -e "${C_YELLOW}Waiting 1.5s for OpenTelemetry trace to flush and index in Tempo...${C_RESET}"
sleep 1.5

# Query Tempo for the most recent trace targeting django_otel_slave2
TRACE_DATA=$(curl -s "${TEMPO_URL}/api/search?q=%7B+span.db.name+%3D+%22django_otel_slave2%22+%7D&limit=1")
TRACE_ID=$(echo "${TRACE_DATA}" | jq -r '.traces[0].traceID // empty')

if [ -n "${TRACE_ID}" ]; then
  echo -e "${C_GREEN}✔ Trace successfully captured!${C_RESET}"
  echo -e "  Trace ID:    ${C_MAGENTA}${TRACE_ID}${C_RESET}"
  echo ""
  echo -e "${C_CYAN}Inspect your trace here:${C_RESET}"
  echo -e "  🔥 ${C_YELLOW}Grafana Flamegraph:${C_RESET}"
  echo -e "     ${GRAFANA_URL}/explore?schemaVersion=1&panes=%7B%22p%22:%7B%22datasource%22:%22tempo%22,%22queries%22:%5B%7B%22query%22:%22${TRACE_ID}%22,%22queryType%22:%22traceql%22%7D%5D%7D%7D"
  echo ""
  echo -e "  📊 ${C_YELLOW}Generic Operation Details Dashboard:${C_RESET}"
  echo -e "     ${GRAFANA_URL}/d/generic-operation-details?var-service=postgres"
  echo ""
  echo -e "  ℹ️  ${C_BLUE}Tempo Raw Trace API:${C_RESET}"
  echo -e "     ${TEMPO_URL}/api/traces/${TRACE_ID}"
else
  echo -e "${C_YELLOW}No recent trace found in Tempo yet. Check tempo container logs if delay persists.${C_RESET}"
fi

echo -e "${C_CYAN}======================================================================${C_RESET}"
