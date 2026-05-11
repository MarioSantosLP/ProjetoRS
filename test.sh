#!/usr/bin/env bash
# =============================================================================
#  stress_test.sh — Reverse Proxy Stress Tests
#  Run from project root: bash stress_test.sh
# =============================================================================

PROXY="http://localhost:8080"
PASS=0
FAIL=0

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ── Helpers ───────────────────────────────────────────────────────────────────
section() {
    echo ""
    echo -e "${CYAN}${BOLD}━━━  $1  ━━━${RESET}"
}

pass() {
    echo -e "  ${GREEN}✔ PASS${RESET}  $1"
    ((PASS++))
}

fail() {
    echo -e "  ${RED}✘ FAIL${RESET}  $1"
    ((FAIL++))
}

info() {
    echo -e "  ${YELLOW}›${RESET} $1"
}

# Run ab and extract a field from its output
ab_field() {
    local output="$1"
    local field="$2"
    echo "$output" | grep "$field" | awk '{print $NF}' | tr -d 'ms%[]'
}

# Return the number of non-2xx responses from ab output (0 if line missing)
ab_non2xx() {
    local output="$1"
    echo "$output" | grep "Non-2xx responses:" | awk '{print $NF}' || echo "0"
}

ab_failed() {
    local output="$1"
    echo "$output" | grep "Failed requests:" | awk '{print $NF}' || echo "0"
}

# ── Pre-flight ────────────────────────────────────────────────────────────────
section "PRE-FLIGHT"

if ! curl -sf "$PROXY/ping" > /dev/null; then
    echo -e "${RED}Proxy is not reachable at $PROXY — aborting.${RESET}"
    exit 1
fi
pass "Proxy is up at $PROXY"

# Make sure both backends are up before we start
docker compose start web1 web2 2>/dev/null
sleep 2

STATUS=$(curl -sf "$PROXY/status")
W1=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print([c for c in d['containers'] if 'web1' in c['container']][0]['reachable'])")
W2=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print([c for c in d['containers'] if 'web2' in c['container']][0]['reachable'])")

if [ "$W1" = "True" ] && [ "$W2" = "True" ]; then
    pass "Both backends reachable (web1=$W1, web2=$W2)"
else
    fail "Not all backends reachable (web1=$W1, web2=$W2) — some tests may give false results"
fi


# ── Test 1 — Baseline ─────────────────────────────────────────────────────────
section "TEST 1 — BASELINE (500 req, c=10)"

OUT=$(ab -n 500 -c 10 "$PROXY/ping" 2>/dev/null)
FAILED=$(ab_failed "$OUT")
RPS=$(echo "$OUT" | grep "Requests per second" | awk '{print $4}')
P99=$(echo "$OUT" | grep "99%" | awk '{print $2}')

info "req/s=$RPS  P99=${P99}ms  failed=$FAILED"

[ "$FAILED" -eq 0 ] && pass "0 failed requests" || fail "Got $FAILED failed requests (expected 0)"
[ "${P99%.*}" -lt 200 ] && pass "P99 ${P99}ms is under 200ms" || fail "P99 ${P99}ms exceeds 200ms"


# ── Test 2 — Moderate load ────────────────────────────────────────────────────
section "TEST 2 — MODERATE LOAD (2000 req, c=50)"

OUT=$(ab -n 2000 -c 50 "$PROXY/ping" 2>/dev/null)
FAILED=$(ab_failed "$OUT")
RPS=$(echo "$OUT" | grep "Requests per second" | awk '{print $4}')
P99=$(echo "$OUT" | grep "99%" | awk '{print $2}')

info "req/s=$RPS  P99=${P99}ms  failed=$FAILED"

[ "$FAILED" -eq 0 ] && pass "0 failed requests under moderate load" || fail "Got $FAILED failed requests"
[ "${P99%.*}" -lt 500 ] && pass "P99 ${P99}ms is under 500ms" || fail "P99 ${P99}ms exceeds 500ms"


# ── Test 3 — Queue saturation + 429s ─────────────────────────────────────────
section "TEST 3 — QUEUE SATURATION (5000 req, c=200)"

OUT=$(ab -n 5000 -c 200 "$PROXY/ping" 2>/dev/null)
NON2XX=$(ab_non2xx "$OUT")
RPS=$(echo "$OUT" | grep "Requests per second" | awk '{print $4}')

info "req/s=$RPS  non-2xx=$NON2XX"

[ "$NON2XX" -gt 0 ] && pass "Queue correctly returned $NON2XX 429s under saturation" || fail "Expected 429s but got none — queue may not be limiting correctly"

# Proxy must still be alive after saturation
if curl -sf "$PROXY/ping" > /dev/null; then
    pass "Proxy still alive after saturation"
else
    fail "Proxy crashed or hung after saturation"
fi


# ── Test 4 — Role-based routing ───────────────────────────────────────────────
section "TEST 4 — ROLE-BASED ROUTING"

# cpu → should always hit web1
CPU_RESP=$(curl -sf -H "X-Workload-Type: cpu" "$PROXY/ping")
CPU_SVC=$(echo "$CPU_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('service','?'))" 2>/dev/null)
[ "$CPU_SVC" = "web1" ] && pass "X-Workload-Type: cpu routed to web1" || fail "cpu routed to $CPU_SVC (expected web1)"

# memory → should always hit web2
MEM_RESP=$(curl -sf -H "X-Workload-Type: memory" "$PROXY/ping")
MEM_SVC=$(echo "$MEM_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('service','?'))" 2>/dev/null)
[ "$MEM_SVC" = "web2" ] && pass "X-Workload-Type: memory routed to web2" || fail "memory routed to $MEM_SVC (expected web2)"

# cpu routing holds under load — all 200 requests must go to web1
info "Running 200 cpu-workload requests to verify affinity holds under load..."
OUT=$(ab -n 200 -c 20 -H "X-Workload-Type: cpu" "$PROXY/ping" 2>/dev/null)
FAILED=$(ab_failed "$OUT")
# Check logs: no web2 hit for cpu workload
WEB2_HITS=$(docker compose exec proxy cat logs/main.py.log 2>/dev/null | grep "workload=cpu" | grep "web2" | wc -l)
[ "$WEB2_HITS" -eq 0 ] && pass "cpu workload never leaked to web2 under load" || fail "cpu workload hit web2 $WEB2_HITS times"


# ── Test 5 — Circuit breaker ──────────────────────────────────────────────────
section "TEST 5 — CIRCUIT BREAKER"

info "Killing web1..."
docker compose stop web1 2>/dev/null
sleep 2

# Send enough requests to trigger the circuit
ab -n 50 -c 5 "$PROXY/ping" > /dev/null 2>&1
sleep 1

STATUS=$(curl -sf "$PROXY/status")
CIRCUIT=$(echo "$STATUS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = [x for x in d['containers'] if 'web1' in x['container']][0]
print(c['circuit'])
")

[ "$CIRCUIT" = "open" ] && pass "Circuit opened for web1 after failures (state=$CIRCUIT)" || fail "Circuit did not open (state=$CIRCUIT)"

# All traffic should now go to web2 — check status code and service name
RESP_CODE=$(curl -s -o /tmp/ping_resp.json -w "%{http_code}" "$PROXY/ping")
SVC=$(python3 -c "import json; print(json.load(open('/tmp/ping_resp.json')).get('service','?'))" 2>/dev/null)
if [ "$RESP_CODE" = "200" ] && [ "$SVC" = "web2" ]; then
    pass "Traffic correctly shifted to web2 while web1 circuit is open"
elif [ "$RESP_CODE" = "200" ] && [ "$SVC" = "web1" ]; then
    fail "Traffic still hitting web1 despite circuit open"
else
    fail "Unexpected response code=$RESP_CODE svc=$SVC"
fi

info "Restarting web1..."
docker compose start web1 2>/dev/null
info "Waiting for circuit recovery (30s)..."
sleep 32

# Trigger half_open → closed by sending a request
curl -sf "$PROXY/ping" > /dev/null
curl -sf "$PROXY/ping" > /dev/null
curl -sf "$PROXY/ping" > /dev/null
sleep 1

STATUS=$(curl -sf "$PROXY/status")
CIRCUIT=$(echo "$STATUS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = [x for x in d['containers'] if 'web1' in x['container']][0]
print(c['circuit'])
")

[ "$CIRCUIT" = "closed" ] && pass "Circuit recovered to closed after web1 restart" || fail "Circuit did not recover (state=$CIRCUIT)"


# ── Test 6 — All backends down ────────────────────────────────────────────────
section "TEST 6 — ALL BACKENDS DOWN"

info "Stopping all backends..."
docker compose stop web1 web2 2>/dev/null
sleep 2

RESP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$PROXY/ping" 2>/dev/null || echo "000")

if [ "$RESP_CODE" = "503" ] || [ "$RESP_CODE" = "502" ]; then
    pass "Got $RESP_CODE (not a crash) when all backends are down"
else
    fail "Expected 503/502 but got $RESP_CODE"
fi

info "Restarting all backends..."
docker compose start web1 web2 2>/dev/null
sleep 3

RESP=$(curl -sf "$PROXY/ping")
if echo "$RESP" | grep -q "service"; then
    pass "Proxy recovered after backends came back"
else
    fail "Proxy did not recover after backends restarted"
fi


# ── Test 7 — Trace endpoint ───────────────────────────────────────────────────
section "TEST 7 — TRACE ENDPOINT"

# Make a request and grab its req_id from the log
curl -sf "$PROXY/ping" > /dev/null
sleep 0.5
REQ_ID=$(docker compose exec proxy cat logs/main.py.log 2>/dev/null | grep "gateway received" | tail -1 | grep -oE '\[[a-f0-9]+\]' | tr -d '[]')

if [ -z "$REQ_ID" ]; then
    fail "Could not extract req_id from proxy logs"
else
    info "Checking trace for req_id=$REQ_ID"
    TRACE=$(curl -sf "$PROXY/trace/$REQ_ID")
    HAS_GATEWAY=$(echo "$TRACE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(any(e['component']=='gateway' for e in d.get('events',[])))" 2>/dev/null)
    HAS_BALANCER=$(echo "$TRACE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(any(e['component']=='balancer' for e in d.get('events',[])))" 2>/dev/null)
    HAS_PROXY=$(echo "$TRACE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(any(e['component']=='proxy' for e in d.get('events',[])))" 2>/dev/null)

    [ "$HAS_GATEWAY" = "True" ] && pass "Trace has gateway event" || fail "Trace missing gateway event"
    [ "$HAS_BALANCER" = "True" ] && pass "Trace has balancer event" || fail "Trace missing balancer event"
    [ "$HAS_PROXY" = "True" ]   && pass "Trace has proxy event"   || fail "Trace missing proxy event"
fi

# 404 for unknown id
CODE=$(curl -o /dev/null -sw "%{http_code}" "$PROXY/trace/doesnotexist")
[ "$CODE" = "404" ] && pass "Unknown trace id returns 404" || fail "Expected 404 for unknown id, got $CODE"


# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Results: ${GREEN}$PASS passed${RESET}  ${RED}$FAIL failed${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

[ "$FAIL" -eq 0 ] && exit 0 || exit 1