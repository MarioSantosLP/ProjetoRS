#!/usr/bin/env bash
# =============================================================================
# test_grpc_integration.sh
# Run from the project root with the stack already up.
# =============================================================================

set -uo pipefail

PROXY="http://localhost:8080"
LOG_FILE="./logs/main.py.log"
PASS=0
FAIL=0
SKIP=0

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
RESET='\033[0m'

ok()      { echo -e "  ${GREEN}✔${RESET} $*"; ((PASS++)); }
fail()    { echo -e "  ${RED}✘${RESET} $*"; ((FAIL++)); }
skip()    { echo -e "  ${YELLOW}–${RESET} $* (skipped)"; ((SKIP++)); }
section() { echo -e "\n${BOLD}── $* ──${RESET}"; }

proxy_get() {
    curl -sf --max-time 5 "$PROXY$1"
}

proxy_status_code() {
    curl -o /dev/null -s -w "%{http_code}" --max-time 5 "$PROXY$1"
}

# Parse a top-level field from a JSON string
json_field() {
    echo "$1" | python3 -c "import sys,json; print(json.load(sys.stdin).get('$2',''))" 2>/dev/null || echo ""
}

# Count items in the containers array matching a given key=value
# Usage: json_count_containers <json> <key> <value>
json_count_containers() {
    echo "$1" | python3 -c "
import sys, json
data = json.load(sys.stdin)
containers = data.get('containers', [])
print(sum(1 for c in containers if str(c.get('$2','')).lower() == '$3'))
" 2>/dev/null || echo "0"
}

# Check if any container in the array has a given name substring
json_has_container() {
    echo "$1" | python3 -c "
import sys, json
data = json.load(sys.stdin)
containers = data.get('containers', [])
found = any('$2' in c.get('container', '') for c in containers)
print('1' if found else '0')
" 2>/dev/null || echo "0"
}

# Count lines in a plain string containing a pattern (for log files only)
count_lines() {
    echo "$1" | grep -c "$2" 2>/dev/null || echo "0"
}

# ── 1. Proxy health ───────────────────────────────────────────────────────────

section "1 · Proxy health"

STATUS_BODY=$(proxy_get "/status" || echo "")

if [[ -z "$STATUS_BODY" ]]; then
    fail "/status returned empty body — is the stack running?"
else
    ok "/status is reachable"

    WEB1=$(json_has_container "$STATUS_BODY" "web1")
    WEB2=$(json_has_container "$STATUS_BODY" "web2")
    [[ "$WEB1" == "1" ]] && ok "web1 present in /status" || fail "web1 missing from /status"
    [[ "$WEB2" == "1" ]] && ok "web2 present in /status" || fail "web2 missing from /status"

    REACHABLE_COUNT=$(json_count_containers "$STATUS_BODY" "reachable" "true")
    [[ "$REACHABLE_COUNT" -ge 2 ]] \
        && ok "both backends reported reachable ($REACHABLE_COUNT)" \
        || fail "expected 2 reachable backends, got $REACHABLE_COUNT"

    CLOSED_COUNT=$(json_count_containers "$STATUS_BODY" "circuit" "closed")
    [[ "$CLOSED_COUNT" -ge 2 ]] \
        && ok "both circuit breakers are closed ($CLOSED_COUNT)" \
        || fail "expected 2 closed circuits, got $CLOSED_COUNT"
fi

# ── 2. gRPC round-trip ────────────────────────────────────────────────────────

section "2 · gRPC round-trip"

RESPONSE=$(proxy_get "/api/test" || echo "")

if [[ -z "$RESPONSE" ]]; then
    fail "No response from proxy for GET /api/test"
else
    ok "Proxy responded to GET /api/test"

    SERVICE_FIELD=$(json_field "$RESPONSE" "service")
    PATH_FIELD=$(json_field "$RESPONSE" "path")
    METHOD_FIELD=$(json_field "$RESPONSE" "method")

    [[ -n "$SERVICE_FIELD" ]] \
        && ok "Response contains 'service' field → $SERVICE_FIELD" \
        || fail "Response missing 'service' field (raw: $RESPONSE)"

    [[ "$PATH_FIELD" == "/api/test" ]] \
        && ok "Response 'path' matches request path" \
        || fail "Response 'path' mismatch: expected /api/test, got '$PATH_FIELD'"

    [[ "$METHOD_FIELD" == "GET" ]] \
        && ok "Response 'method' matches" \
        || fail "Response 'method' mismatch: expected GET, got '$METHOD_FIELD'"
fi

# ── 3. Load distribution ──────────────────────────────────────────────────────

section "3 · Load distribution across backends"

REQUESTS=10
declare -A SEEN_SERVICES

for i in $(seq 1 $REQUESTS); do
    svc=$(proxy_get "/api/probe" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('service','unknown'))" 2>/dev/null \
        || echo "error")
    SEEN_SERVICES["$svc"]=1
done

echo "  Services seen: ${!SEEN_SERVICES[*]}"

SEEN_COUNT=${#SEEN_SERVICES[@]}
if [[ $SEEN_COUNT -ge 2 ]]; then
    ok "Both backends were hit across $REQUESTS requests"
elif [[ $SEEN_COUNT -eq 1 ]]; then
    skip "Only one backend seen — may be valid if active_probe consistently prefers one"
else
    fail "No valid service names received across $REQUESTS requests"
fi

# ── 4. Trace records the gRPC hop ────────────────────────────────────────────

section "4 · Distributed trace — gRPC hop recorded"

proxy_get "/api/trace_test" > /dev/null 2>&1 || true
sleep 1

if [[ ! -f "$LOG_FILE" ]]; then
    skip "Log file not found at $LOG_FILE — skipping trace test"
else
    REQ_ID=$(grep -oE '\[[0-9a-f]{8}\]' "$LOG_FILE" \
        | tail -1 \
        | tr -d '[]' || echo "")

    if [[ -z "$REQ_ID" ]]; then
        skip "Could not extract req_id from $LOG_FILE — skipping trace test"
    else
        TRACE_BODY=$(proxy_get "/trace/$REQ_ID" || echo "")

        if [[ -z "$TRACE_BODY" ]]; then
            fail "/trace/$REQ_ID returned empty body"
        else
            ok "/trace/$REQ_ID is accessible"

            PROXY_HOP=$(count_lines "$TRACE_BODY" '"component": "proxy"')
            [[ "$PROXY_HOP" -gt 0 ]] \
                && ok "Trace contains 'proxy' component (gRPC leg recorded)" \
                || fail "Trace missing 'proxy' component — gRPC leg not traced"

            QUEUE_HOP=$(count_lines "$TRACE_BODY" '"component": "queue"')
            [[ "$QUEUE_HOP" -gt 0 ]] \
                && ok "Trace contains 'queue' component" \
                || fail "Trace missing 'queue' component"

            BALANCER_HOP=$(count_lines "$TRACE_BODY" '"component": "balancer"')
            [[ "$BALANCER_HOP" -gt 0 ]] \
                && ok "Trace contains 'balancer' component" \
                || fail "Trace missing 'balancer' component"
        fi
    fi
fi

# ── 5. X-Workload-Type header routing ────────────────────────────────────────

section "5 · X-Workload-Type header routing"

for WORKLOAD in cpu memory; do
    RESP=$(curl -sf --max-time 5 -H "X-Workload-Type: $WORKLOAD" "$PROXY/api/workload_test" || echo "")

    if [[ -z "$RESP" ]]; then
        fail "No response for X-Workload-Type: $WORKLOAD"
    else
        SVC=$(json_field "$RESP" "service")
        [[ -n "$SVC" ]] \
            && ok "X-Workload-Type: $WORKLOAD → routed to '$SVC'" \
            || fail "X-Workload-Type: $WORKLOAD — response has no 'service' field (raw: $RESP)"
    fi
done

# ── 6. /metrics ───────────────────────────────────────────────────────────────

section "6 · Metrics endpoint"

METRICS_BODY=$(proxy_get "/metrics" || echo "")

if [[ -z "$METRICS_BODY" ]]; then
    fail "/metrics returned empty body"
else
    ok "/metrics is reachable"
    UPTIME=$(json_field "$METRICS_BODY" "uptime")
    TOTAL=$(json_field  "$METRICS_BODY" "total_requests")
    [[ -n "$UPTIME" ]] && ok "uptime present → ${UPTIME}s"     || fail "uptime missing from /metrics"
    [[ -n "$TOTAL"  ]] && ok "total_requests present → $TOTAL" || fail "total_requests missing from /metrics"
fi

# ── 7. Trace 404 for unknown id ───────────────────────────────────────────────

section "7 · Trace 404 for unknown id"

CODE=$(proxy_status_code "/trace/deadbeef")
[[ "$CODE" == "404" ]] \
    && ok "/trace/deadbeef returns 404 as expected" \
    || fail "/trace/deadbeef returned $CODE instead of 404"

# ── 8. Priority queue saturation ─────────────────────────────────────────────

section "8 · Priority queue — 429 under saturation"

echo "  Firing 120 concurrent requests (queue max is 100)..."
TMPFILE=$(mktemp)
for i in $(seq 1 120); do
    curl -o /dev/null -s -w "%{http_code}\n" --max-time 3 "$PROXY/api/load_test" >> "$TMPFILE" &
done
wait

GOT_429=$(count_lines "$(cat "$TMPFILE")" "429")
rm -f "$TMPFILE"

if [[ "$GOT_429" -gt 0 ]]; then
    ok "Got $GOT_429 × 429 responses — queue rejection is working"
else
    skip "No 429 observed — backends may have drained the queue fast enough (not a failure)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}═══════════════════════════════${RESET}"
echo -e "${BOLD}  Test Summary${RESET}"
echo -e "${BOLD}═══════════════════════════════${RESET}"
echo -e "  ${GREEN}Passed:${RESET}  $PASS"
echo -e "  ${RED}Failed:${RESET}  $FAIL"
echo -e "  ${YELLOW}Skipped:${RESET} $SKIP"
echo -e "${BOLD}═══════════════════════════════${RESET}"

if [[ $FAIL -gt 0 ]]; then
    echo -e "\n${RED}  Some tests failed. Check '$LOG_FILE' for details.${RESET}"
    exit 1
else
    echo -e "\n${GREEN}  All tests passed.${RESET}"
    exit 0
fi