#!/usr/bin/env bash
# ==============================================================================
# generate_all_traffic.sh — Universal Multi-App Traffic Generator for tp_trace APM
# ==============================================================================
# Generates realistic, concurrent traffic across apps:
#   1. Django Full App (Port 8001)   — Django + Postgres (Primary & Replicas) + Redis + External API
#   2. Django Lite App (Port 8003)   — Pure In-Memory Django (Zero Postgres, Zero Redis)
# ==============================================================================

DJANGO_FULL_URL="${DJANGO_FULL_URL:-http://localhost:8001}"
DJANGO_LITE_URL="${DJANGO_LITE_URL:-http://localhost:8003}"

MODE="loop"
DURATION=0
SLEEP_DELAY=0.2
TARGET_APPS="all"

COUNT_DJANGO_FULL=0
COUNT_DJANGO_LITE=0
COUNT_TOTAL=0
COUNT_2XX=0
COUNT_4XX=0
COUNT_5XX=0
START_TIME=$(date +%s)

# Terminal colors
C_RESET="\033[0m"
C_BOLD="\033[1m"
C_GREEN="\033[32m"
C_YELLOW="\033[33m"
C_RED="\033[31m"
C_BLUE="\033[34m"
C_CYAN="\033[36m"
C_MAGENTA="\033[35m"

# Parse args
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --once) MODE="once" ;;
        --loop) MODE="loop" ;;
        -d|--duration) DURATION="$2"; MODE="duration"; shift ;;
        -s|--sleep) SLEEP_DELAY="$2"; shift ;;
        --apps) TARGET_APPS="$2"; shift ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --loop            Run continuously until Ctrl+C (default)"
            echo "  --once            Run a single pass of all endpoints"
            echo "  -d, --duration N  Run continuously for N seconds"
            echo "  -s, --sleep SEC   Delay between cycles in seconds (default: 0.2)"
            echo "  --apps LIST       Comma-separated apps: all, django, django-lite"
            echo "  -h, --help        Show this help message"
            exit 0
            ;;
        *) echo "Unknown parameter: $1"; exit 1 ;;
    esac
    shift
done

cleanup() {
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - START_TIME))
    if [ "$ELAPSED" -le 0 ]; then ELAPSED=1; fi
    RPS=$(echo "scale=2; $COUNT_TOTAL / $ELAPSED" | bc 2>/dev/null || echo "N/A")

    echo ""
    echo -e "${C_BOLD}================================================================${C_RESET}"
    echo -e "${C_BOLD}                   TRAFFIC GENERATION SUMMARY                   ${C_RESET}"
    echo -e "${C_BOLD}================================================================${C_RESET}"
    echo -e "  Total Duration     : ${ELAPSED}s"
    echo -e "  Total Requests     : ${C_CYAN}${COUNT_TOTAL}${C_RESET}"
    echo -e "  Overall RPS        : ${C_BOLD}${RPS} req/s${C_RESET}"
    echo -e "  2xx Successful     : ${C_GREEN}${COUNT_2XX}${C_RESET}"
    echo -e "  4xx Client Errors  : ${C_YELLOW}${COUNT_4XX}${C_RESET}"
    echo -e "  5xx Server Errors  : ${C_RED}${COUNT_5XX}${C_RESET}"
    echo -e "----------------------------------------------------------------"
    echo -e "  1. Django (Full)   : ${C_BLUE}${COUNT_DJANGO_FULL}${C_RESET} requests (Postgres + Redis)"
    echo -e "  2. Django (Lite)   : ${C_GREEN}${COUNT_DJANGO_LITE}${C_RESET} requests (Pure Django, Zero DB)"
    echo -e "${C_BOLD}================================================================${C_RESET}"
    exit 0
}

trap cleanup INT TERM

send_req() {
    local method="$1"
    local url="$2"
    local app_name="$3"
    local data="$4"

    local status
    if [ "$method" = "POST" ] && [ -n "$data" ]; then
        status=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -d "$data" "$url" 2>/dev/null || echo "000")
    else
        status=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    fi

    COUNT_TOTAL=$((COUNT_TOTAL + 1))
    case "$app_name" in
        "django_full") COUNT_DJANGO_FULL=$((COUNT_DJANGO_FULL + 1)) ;;
        "django_lite") COUNT_DJANGO_LITE=$((COUNT_DJANGO_LITE + 1)) ;;
    esac

    if [[ "$status" =~ ^2 ]]; then
        COUNT_2XX=$((COUNT_2XX + 1))
        echo -e " [${C_GREEN}${status}${C_RESET}] ${app_name} ${method} ${url}"
    elif [[ "$status" =~ ^4 ]]; then
        COUNT_4XX=$((COUNT_4XX + 1))
        echo -e " [${C_YELLOW}${status}${C_RESET}] ${app_name} ${method} ${url}"
    elif [[ "$status" =~ ^5 ]]; then
        COUNT_5XX=$((COUNT_5XX + 1))
        echo -e " [${C_RED}${status}${C_RESET}] ${app_name} ${method} ${url}"
    else
        echo -e " [${C_RED}${status}${C_RESET}] ${app_name} ${method} ${url} (Connection refused/unreachable)"
    fi
}

echo -e "${C_BOLD}================================================================${C_RESET}"
echo -e "${C_BOLD}         TP_TRACE APM — UNIVERSAL MULTI-APP TRAFFIC          ${C_RESET}"
echo -e "${C_BOLD}================================================================${C_RESET}"
echo -e "  Target 1: Django Full (Postgres + Redis) : ${DJANGO_FULL_URL}"
echo -e "  Target 2: Django Lite (Zero DB)          : ${DJANGO_LITE_URL}"
echo -e "  Mode: ${C_CYAN}${MODE}${C_RESET} | Apps: ${C_YELLOW}${TARGET_APPS}${C_RESET}"
echo -e "================================================================"

iteration=0

run_cycle() {
    iteration=$((iteration + 1))

    # --- 1. DJANGO FULL APP (Postgres + Redis + External) ---
    if [[ "$TARGET_APPS" == "all" || "$TARGET_APPS" =~ "django" && ! "$TARGET_APPS" =~ "django-lite" ]]; then
        send_req "GET"  "${DJANGO_FULL_URL}/api/products/" "django_full"
        send_req "GET"  "${DJANGO_FULL_URL}/api/products/1/" "django_full"
        send_req "GET"  "${DJANGO_FULL_URL}/api/products/cache/" "django_full"
        send_req "GET"  "${DJANGO_FULL_URL}/api/products/health/" "django_full"
        send_req "GET"  "${DJANGO_FULL_URL}/api/products/external/" "django_full"
        send_req "GET"  "${DJANGO_FULL_URL}/api/products/replicas/" "django_full"
        
        if [ $((iteration % 3)) -eq 0 ]; then
            send_req "POST" "${DJANGO_FULL_URL}/api/products/" "django_full" '{"name":"Gadget","price":29.99,"stock":50}'
        fi
        if [ $((iteration % 5)) -eq 0 ]; then
            send_req "GET"  "${DJANGO_FULL_URL}/api/products/error/" "django_full"
        fi
    fi

    # --- 2. DJANGO LITE APP (Pure Django, Zero Postgres, Zero Redis) ---
    if [[ "$TARGET_APPS" == "all" || "$TARGET_APPS" =~ "django-lite" ]]; then
        send_req "GET"  "${DJANGO_LITE_URL}/" "django_lite"
        send_req "GET"  "${DJANGO_LITE_URL}/api/users/" "django_lite"
        send_req "GET"  "${DJANGO_LITE_URL}/api/orders/" "django_lite"
        send_req "GET"  "${DJANGO_LITE_URL}/api/slow/" "django_lite"
        
        if [ $((iteration % 6)) -eq 0 ]; then
            send_req "GET"  "${DJANGO_LITE_URL}/api/error/" "django_lite"
        fi
    fi

    sleep "$SLEEP_DELAY"
}

if [ "$MODE" = "once" ]; then
    echo "Running single test cycle across target apps..."
    run_cycle
    cleanup
elif [ "$MODE" = "duration" ]; then
    echo "Running traffic generator for ${DURATION} seconds..."
    END_AT=$((START_TIME + DURATION))
    while [ "$(date +%s)" -lt "$END_AT" ]; do
        run_cycle
    done
    cleanup
else
    echo "Streaming live traffic continuously... (Press Ctrl+C to stop & view summary)"
    while true; do
        run_cycle
    done
fi
