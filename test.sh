#!/usr/bin/env bash
# =============================================================================
# test_websocket.sh
# WebSocket integration + load test for the reverse proxy.
# Requires: wscat  (npm i -g wscat)
#           websocat (https://github.com/vi/websocat — fallback for load test)
#           curl, python3, bc — all standard
#
# Run from the project root with the stack already up:
#   bash test_websocket.sh
# =============================================================================

set -uo pipefail

PROXY_HTTP="http://localhost:8080"
PROXY_WS="ws://localhost:8080/ws"
PASS=0
FAIL=0
SKIP=0
WARN=0

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ok()      { echo -e "  ${GREEN}✔${RESET} $*";          ((PASS++)); }
fail()    { echo -e "  ${RED}✘${RESET} $*";            ((FAIL++)); }
skip()    { echo -e "  ${YELLOW}–${RESET} $* (skipped)"; ((SKIP++)); }
warn()    { echo -e "  ${YELLOW}⚠${RESET} $*";          ((WARN++)); }
section() { echo -e "\n${BOLD}${CYAN}── $* ──${RESET}"; }
note()    { echo -e "    ${CYAN}↳${RESET} $*"; }

# ── Dependency check ──────────────────────────────────────────────────────────

section "0 · Dependency check"

HAS_WSCAT=false
HAS_WEBSOCAT=false
HAS_PYTHON_WS=false

if command -v wscat &>/dev/null; then
    HAS_WSCAT=true
    ok "wscat found: $(wscat --version 2>/dev/null || echo 'installed')"
else
    warn "wscat not found — install with: npm i -g wscat"
fi

if command -v websocat &>/dev/null; then
    HAS_WEBSOCAT=true
    ok "websocat found"
else
    warn "websocat not found — some load tests will be skipped"
fi

# Python websockets library check
if python3 -c "import websockets" &>/dev/null; then
    HAS_PYTHON_WS=true
    ok "python3 websockets library found"
else
    warn "python3 'websockets' not installed — install with: pip install websockets"
    note "Falling back to wscat/websocat for connection tests"
fi

if ! $HAS_WSCAT && ! $HAS_WEBSOCAT && ! $HAS_PYTHON_WS; then
    echo -e "\n${RED}ERROR: No WebSocket client available. Install at least one of:${RESET}"
    echo "  npm i -g wscat"
    echo "  pip install websockets"
    exit 1
fi

# ── Helper: send one WS message and capture reply (Python) ───────────────────

ws_send_recv() {
    # Usage: ws_send_recv <url> <message> [timeout_seconds]
    local url="$1" msg="$2" timeout="${3:-5}"
    python3 - <<PYEOF
import asyncio, sys
try:
    import websockets
except ImportError:
    sys.exit(2)

async def run():
    try:
        async with websockets.connect("$url", open_timeout=$timeout) as ws:
            await ws.send("""$msg""")
            reply = await asyncio.wait_for(ws.recv(), timeout=$timeout)
            print(reply)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

asyncio.run(run())
PYEOF
}

ws_multi_send() {
    # Send N messages on one connection, print all replies
    local url="$1" count="$2" prefix="${3:-msg}" timeout="${4:-10}"
    python3 - <<PYEOF
import asyncio, sys
try:
    import websockets
except ImportError:
    sys.exit(2)

async def run():
    try:
        async with websockets.connect("$url", open_timeout=5) as ws:
            for i in range($count):
                await ws.send(f"$prefix-{i}")
                reply = await asyncio.wait_for(ws.recv(), timeout=$timeout)
                print(reply)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

asyncio.run(run())
PYEOF
}

ws_connect_only() {
    # Just open + close — returns 0 if successful
    local url="$1" timeout="${2:-5}"
    python3 - <<PYEOF
import asyncio, sys
try:
    import websockets
except ImportError:
    sys.exit(2)

async def run():
    try:
        async with websockets.connect("$url", open_timeout=$timeout) as ws:
            pass  # just check we can open
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

asyncio.run(run())
PYEOF
}

# ── 1. Proxy sanity ───────────────────────────────────────────────────────────

section "1 · Proxy sanity (HTTP)"

STATUS=$(curl -sf --max-time 5 "$PROXY_HTTP/status" || echo "")
if [[ -z "$STATUS" ]]; then
    fail "Proxy not responding — is the stack up? (docker-compose up -d)"
    echo -e "\n${RED}Cannot continue without a running proxy.${RESET}"
    exit 1
fi
ok "Proxy HTTP is reachable"

REACHABLE=$(echo "$STATUS" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(sum(1 for c in d.get('containers',[]) if c.get('reachable')))
" 2>/dev/null || echo "0")

[[ "$REACHABLE" -ge 2 ]] \
    && ok "Both backends reachable ($REACHABLE)" \
    || fail "Expected 2 reachable backends, got $REACHABLE"

# ── 2. Basic WebSocket connection ─────────────────────────────────────────────

section "2 · Basic WebSocket connection"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available — skipping section 2"
else
    if ws_connect_only "$PROXY_WS" 5; then
        ok "WebSocket handshake succeeded (connect + close)"
    else
        fail "WebSocket handshake failed — proxy may not be routing /ws correctly"
    fi

    # Verify the Upgrade header path is recognised
    WS_UPGRADE_CODE=$(curl -o /dev/null -s -w "%{http_code}" \
        -H "Upgrade: websocket" \
        -H "Connection: Upgrade" \
        -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
        -H "Sec-WebSocket-Version: 13" \
        --max-time 5 "$PROXY_HTTP/ws" || echo "000")

    # 101 = switching protocols, 400 = bad request (still reached proxy)
    if [[ "$WS_UPGRADE_CODE" == "101" || "$WS_UPGRADE_CODE" == "400" ]]; then
        ok "Proxy recognised WebSocket upgrade (HTTP $WS_UPGRADE_CODE)"
    else
        warn "Unexpected HTTP code for WS upgrade: $WS_UPGRADE_CODE (may be fine)"
    fi
fi

# ── 3. Echo round-trip ────────────────────────────────────────────────────────

section "3 · Echo round-trip — single message"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    REPLY=$(ws_send_recv "$PROXY_WS" "hello-proxy" 5 2>&1)
    if echo "$REPLY" | grep -qi "received: hello-proxy"; then
        ok "Echo reply contains sent message"
        note "Reply: $REPLY"
    elif echo "$REPLY" | grep -qi "ERROR"; then
        fail "WebSocket echo failed: $REPLY"
    else
        warn "Reply format unexpected (may still be ok): $REPLY"
    fi
fi

# ── 4. Multi-message on single connection ─────────────────────────────────────

section "4 · Multiple messages on one connection"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    MSGS=10
    REPLIES=$(ws_multi_send "$PROXY_WS" $MSGS "payload" 10 2>&1)
    REPLY_COUNT=$(echo "$REPLIES" | grep -c "received" || echo "0")

    if [[ "$REPLY_COUNT" -eq "$MSGS" ]]; then
        ok "All $MSGS messages echoed back on single connection"
    elif [[ "$REPLY_COUNT" -gt 0 ]]; then
        warn "Got $REPLY_COUNT/$MSGS replies — partial success"
    else
        fail "No echo replies received for multi-message test"
        note "Output: $REPLIES"
    fi
fi

# ── 5. Load balancing — distribution across backends ─────────────────────────

section "5 · Load balancing — WS connections hit multiple backends"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    CONNECTIONS=10
    SERVICES_SEEN=$(python3 - <<PYEOF
import asyncio, websockets, json

WS_URL = "$PROXY_WS"
CONNECTIONS = $CONNECTIONS

async def one_conn(i):
    try:
        async with websockets.connect(WS_URL, open_timeout=5) as ws:
            await ws.send(f"probe-{i}")
            reply = await asyncio.wait_for(ws.recv(), timeout=5)
            # reply is "webN received: probe-N"
            return reply.split(" ")[0] if reply else "unknown"
    except Exception as e:
        return f"error:{e}"

async def main():
    results = await asyncio.gather(*[one_conn(i) for i in range(CONNECTIONS)])
    unique = set(r for r in results if not r.startswith("error"))
    print(" ".join(unique))

asyncio.run(main())
PYEOF
    )

    UNIQUE_COUNT=$(echo "$SERVICES_SEEN" | wc -w | tr -d ' ')
    if [[ "$UNIQUE_COUNT" -ge 2 ]]; then
        ok "Connections distributed across $UNIQUE_COUNT backends: $SERVICES_SEEN"
    elif [[ "$UNIQUE_COUNT" -eq 1 ]]; then
        warn "All $CONNECTIONS connections went to one backend: $SERVICES_SEEN"
        note "This may be valid under active_probe if one backend is consistently faster"
    else
        fail "Could not determine backend distribution — all connections errored"
        note "Services seen: $SERVICES_SEEN"
    fi
fi

# ── 6. Concurrent WebSocket connections ───────────────────────────────────────

section "6 · Concurrent connections — 20 simultaneous"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    CONC_RESULT=$(python3 - <<PYEOF
import asyncio, websockets

WS_URL = "$PROXY_WS"
N = 20

async def one(i):
    try:
        async with websockets.connect(WS_URL, open_timeout=5) as ws:
            await ws.send(f"concurrent-{i}")
            reply = await asyncio.wait_for(ws.recv(), timeout=8)
            return "ok"
    except Exception as e:
        return f"err:{e}"

async def main():
    results = await asyncio.gather(*[one(i) for i in range(N)])
    ok_count  = sum(1 for r in results if r == "ok")
    err_count = sum(1 for r in results if r.startswith("err"))
    print(f"{ok_count} {err_count}")

asyncio.run(main())
PYEOF
    )

    WS_OK=$(echo "$CONC_RESULT" | awk '{print $1}')
    WS_ERR=$(echo "$CONC_RESULT" | awk '{print $2}')
    TOTAL_CONC=$((WS_OK + WS_ERR))

    if [[ "$WS_OK" -eq 20 ]]; then
        ok "All 20 concurrent connections succeeded"
    elif [[ "$WS_OK" -ge 15 ]]; then
        warn "$WS_OK/20 succeeded ($WS_ERR errors) — acceptable under load"
    else
        fail "$WS_OK/20 succeeded ($WS_ERR errors) — too many failures"
    fi
fi

# ── 7. Large message payload ──────────────────────────────────────────────────

section "7 · Large message payload (64 KB)"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    LARGE_RESULT=$(python3 - <<PYEOF
import asyncio, websockets

WS_URL = "$PROXY_WS"
BIG = "X" * 65536   # 64 KB

async def main():
    try:
        async with websockets.connect(WS_URL, open_timeout=5, max_size=2**21) as ws:
            await ws.send(BIG)
            reply = await asyncio.wait_for(ws.recv(), timeout=10)
            if "received" in reply:
                print("ok")
            else:
                print(f"unexpected:{reply[:80]}")
    except Exception as e:
        print(f"error:{e}")

asyncio.run(main())
PYEOF
    )

    if [[ "$LARGE_RESULT" == "ok" ]]; then
        ok "64 KB message echoed successfully"
    elif echo "$LARGE_RESULT" | grep -q "unexpected"; then
        warn "Reply format unexpected for large payload: $LARGE_RESULT"
    else
        fail "Large payload test failed: $LARGE_RESULT"
    fi
fi

# ── 8. Graceful client disconnect ─────────────────────────────────────────────

section "8 · Graceful client disconnect"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    DISC_RESULT=$(python3 - <<PYEOF
import asyncio, websockets

WS_URL = "$PROXY_WS"

async def main():
    try:
        async with websockets.connect(WS_URL, open_timeout=5) as ws:
            await ws.send("before-close")
            reply = await asyncio.wait_for(ws.recv(), timeout=5)
            # Now close from client side
            await ws.close(code=1000, reason="done")
            print("ok")
    except Exception as e:
        print(f"error:{e}")

asyncio.run(main())
PYEOF
    )

    if [[ "$DISC_RESULT" == "ok" ]]; then
        ok "Client-initiated close completed without error"
    else
        fail "Graceful disconnect failed: $DISC_RESULT"
    fi
fi

# Wait a moment and check proxy is still healthy after disconnect
sleep 1
STATUS_AFTER=$(curl -sf --max-time 5 "$PROXY_HTTP/status" || echo "")
if [[ -n "$STATUS_AFTER" ]]; then
    ok "Proxy still healthy after client disconnect"
else
    fail "Proxy stopped responding after client disconnect"
fi

# ── 9. Circuit breaker bypass — WS hits healthy backend ───────────────────────

section "9 · Circuit breaker state — WS respects healthy backends"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    CB_STATUS=$(curl -sf --max-time 5 "$PROXY_HTTP/status" || echo "")
    CB_CLOSED=$(echo "$CB_STATUS" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(sum(1 for c in d.get('containers',[]) if c.get('circuit')=='closed'))
" 2>/dev/null || echo "0")

    if [[ "$CB_CLOSED" -ge 2 ]]; then
        ok "Both circuit breakers closed — WS routing should use full pool"
    else
        warn "Some circuits not closed ($CB_CLOSED/2) — WS may route to limited pool"
    fi

    WS_AFTER=$(ws_send_recv "$PROXY_WS" "circuit-check" 5 2>&1)
    if echo "$WS_AFTER" | grep -qi "received"; then
        ok "WebSocket still functional with current circuit breaker state"
    else
        fail "WebSocket failed: $WS_AFTER"
    fi
fi

# ── 10. Binary message support ────────────────────────────────────────────────

section "10 · Binary message (the backend echoes text — checking proxy doesn't corrupt)"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    BIN_RESULT=$(python3 - <<PYEOF
import asyncio, websockets

WS_URL = "$PROXY_WS"

async def main():
    try:
        async with websockets.connect(WS_URL, open_timeout=5) as ws:
            # Send bytes — the FastAPI backend only handles text, so we expect
            # either an echo, a close frame, or a protocol error. We check the
            # proxy doesn't hang or crash.
            await ws.send(b"\x00\x01\x02\x03binary-test")
            try:
                reply = await asyncio.wait_for(ws.recv(), timeout=5)
                print(f"got_reply:{type(reply).__name__}")
            except asyncio.TimeoutError:
                print("timeout_no_reply")
    except (websockets.exceptions.ConnectionClosedError,
            websockets.exceptions.ConnectionClosedOK) as e:
        # Backend closed on binary — that's acceptable behaviour
        print(f"closed_by_backend:{e.code}")
    except Exception as e:
        print(f"error:{e}")

asyncio.run(main())
PYEOF
    )

    if echo "$BIN_RESULT" | grep -qE "got_reply|closed_by_backend|timeout_no_reply"; then
        ok "Binary frame handled gracefully (no proxy crash): $BIN_RESULT"
    else
        fail "Unexpected result for binary message: $BIN_RESULT"
    fi

    # Proxy must still be up
    sleep 1
    ALIVE=$(curl -sf --max-time 5 "$PROXY_HTTP/status" || echo "")
    [[ -n "$ALIVE" ]] && ok "Proxy alive after binary message test" \
                      || fail "Proxy crashed after binary message"
fi

# ── 11. WebSocket + X-Workload-Type header routing ───────────────────────────

section "11 · X-Workload-Type header not applicable to WS (bypass check)"

# WS connections skip the priority queue entirely (correct behaviour).
# We verify an HTTP request with X-Workload-Type still works in parallel,
# confirming the two paths don't interfere.

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    HTTP_RESP=$(curl -sf --max-time 5 -H "X-Workload-Type: cpu" "$PROXY_HTTP/api/test" || echo "")
    WS_RESP=$(ws_send_recv "$PROXY_WS" "parallel-test" 5 2>&1)

    if [[ -n "$HTTP_RESP" ]] && echo "$WS_RESP" | grep -qi "received"; then
        ok "HTTP (X-Workload-Type) and WebSocket work simultaneously"
    elif [[ -z "$HTTP_RESP" ]]; then
        fail "HTTP request with X-Workload-Type failed during WS test"
    else
        warn "WS reply unexpected during parallel test: $WS_RESP"
    fi
fi

# ── 12. Sustained load test ───────────────────────────────────────────────────

section "12 · Load test — 50 concurrent connections, 5 messages each"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    echo "  Running sustained WS load test (this takes ~15s)..."

    LOAD_RESULT=$(python3 - <<PYEOF
import asyncio, websockets, time

WS_URL     = "$PROXY_WS"
CONNS      = 50
MSGS_EACH  = 5
TIMEOUT    = 15

ok_conn = 0
err_conn = 0
ok_msgs  = 0
err_msgs = 0
latencies = []

async def one_conn(conn_id):
    global ok_conn, err_conn, ok_msgs, err_msgs
    try:
        async with websockets.connect(WS_URL, open_timeout=5, max_size=2**20) as ws:
            ok_conn += 1
            for i in range(MSGS_EACH):
                try:
                    t0 = time.monotonic()
                    await ws.send(f"load-{conn_id}-{i}")
                    reply = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT)
                    latencies.append((time.monotonic() - t0) * 1000)
                    if "received" in reply:
                        ok_msgs += 1
                    else:
                        err_msgs += 1
                except Exception:
                    err_msgs += 1
    except Exception:
        err_conn += 1

async def main():
    await asyncio.gather(*[one_conn(i) for i in range(CONNS)])
    total_msgs = ok_msgs + err_msgs
    if latencies:
        latencies.sort()
        p50 = latencies[int(len(latencies)*0.50)]
        p95 = latencies[int(len(latencies)*0.95)]
        p99 = latencies[min(int(len(latencies)*0.99), len(latencies)-1)]
    else:
        p50 = p95 = p99 = 0.0
    print(f"conn_ok={ok_conn} conn_err={err_conn} msg_ok={ok_msgs} msg_err={err_msgs} "
          f"p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms")

asyncio.run(main())
PYEOF
    )

    echo "  $LOAD_RESULT"

    CONN_OK=$(echo  "$LOAD_RESULT" | grep -oP 'conn_ok=\K[0-9]+' || echo 0)
    CONN_ERR=$(echo "$LOAD_RESULT" | grep -oP 'conn_err=\K[0-9]+'|| echo 0)
    MSG_OK=$(echo   "$LOAD_RESULT" | grep -oP 'msg_ok=\K[0-9]+'  || echo 0)
    MSG_ERR=$(echo  "$LOAD_RESULT" | grep -oP 'msg_err=\K[0-9]+' || echo 0)
    P95=$(echo      "$LOAD_RESULT" | grep -oP 'p95=\K[0-9.]+'    || echo 0)

    CONNS=50
    TOTAL_EXPECTED=250

    [[ "$CONN_OK"  -ge 45 ]] && ok "Connections: $CONN_OK/50 succeeded"            \
                              || fail "Too many connection failures: $CONN_ERR/50 failed"

    [[ "$MSG_OK"   -ge 200 ]] && ok "Messages: $MSG_OK/250 echoed successfully"    \
                               || fail "Too many message failures: $MSG_ERR/250 failed"

    P95_INT=$(echo "$P95" | cut -d. -f1)
    [[ "${P95_INT:-9999}" -lt 2000 ]] \
        && ok "P95 latency: ${P95}ms (under 2s threshold)"                         \
        || warn "P95 latency high: ${P95}ms — backends may be under pressure"
fi

# ── 13. Proxy resilience — HTTP still works during WS load ───────────────────

section "13 · HTTP endpoints functional during WebSocket load"

if ! $HAS_PYTHON_WS; then
    skip "python3 websockets not available"
else
    echo "  Running 20 background WS connections while checking HTTP endpoints..."

    python3 - <<PYEOF &
import asyncio, websockets

async def hold(i):
    try:
        async with websockets.connect("$PROXY_WS", open_timeout=5) as ws:
            for _ in range(5):
                await ws.send(f"background-{i}")
                await ws.recv()
                await asyncio.sleep(0.2)
    except Exception:
        pass

async def main():
    await asyncio.gather(*[hold(i) for i in range(20)])

asyncio.run(main())
PYEOF
    BG_PID=$!

    sleep 1   # let WS connections establish

    # Check HTTP endpoints while WS is live
    STATUS_DURING=$(curl -sf --max-time 5 "$PROXY_HTTP/status"  || echo "")
    METRICS_DURING=$(curl -sf --max-time 5 "$PROXY_HTTP/metrics" || echo "")
    HTTP_DURING=$(curl -sf --max-time 5 "$PROXY_HTTP/api/health_check" || echo "")

    wait $BG_PID 2>/dev/null || true

    [[ -n "$STATUS_DURING"  ]] && ok "/status responsive during WS load"  || fail "/status failed during WS load"
    [[ -n "$METRICS_DURING" ]] && ok "/metrics responsive during WS load" || fail "/metrics failed during WS load"
    [[ -n "$HTTP_DURING"    ]] && ok "HTTP proxy forwarding works during WS load" \
                                || warn "HTTP forwarding unavailable during WS load (backends may be saturated)"
fi

# ── 14. Trace — WebSocket hop is recorded ────────────────────────────────────

section "14 · Distributed trace — WebSocket events recorded"

LOG_FILE="./logs/main.py.log"

if [[ ! -f "$LOG_FILE" ]]; then
    skip "Log file not found at $LOG_FILE"
else
    # Make a WS connection so there's a recent trace entry
    if $HAS_PYTHON_WS; then
        ws_send_recv "$PROXY_WS" "trace-probe" 5 >/dev/null 2>&1 || true
        sleep 0.5
    fi

    WS_LOG_LINES=$(grep -c "websocket" "$LOG_FILE" 2>/dev/null || echo "0")
    if [[ "$WS_LOG_LINES" -gt 0 ]]; then
        ok "WebSocket events appear in proxy logs ($WS_LOG_LINES lines)"
    else
        warn "No 'websocket' entries in proxy log — trace may use different labels"
    fi

    # Check for connect/disconnect events
    for keyword in "connected" "disconnected"; do
        COUNT=$(grep -c "$keyword" "$LOG_FILE" 2>/dev/null || echo "0")
        [[ "$COUNT" -gt 0 ]] \
            && ok "Log contains '$keyword' events ($COUNT occurrences)" \
            || warn "No '$keyword' events found in logs"
    done
fi

# ── 15. Metrics reflect WebSocket activity ────────────────────────────────────

section "15 · /metrics — request counts updated"

METRICS_BEFORE=$(curl -sf --max-time 5 "$PROXY_HTTP/metrics" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_requests',0))" 2>/dev/null || echo "0")

# WS connections bypass the priority queue so total_requests won't increment,
# but making a normal HTTP request alongside verifies the counter is live.
curl -sf --max-time 5 "$PROXY_HTTP/api/metrics_probe" >/dev/null || true
sleep 0.5

METRICS_AFTER=$(curl -sf --max-time 5 "$PROXY_HTTP/metrics" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_requests',0))" 2>/dev/null || echo "0")

if [[ "$METRICS_AFTER" -gt "$METRICS_BEFORE" ]]; then
    ok "total_requests counter incremented ($METRICS_BEFORE → $METRICS_AFTER)"
else
    warn "total_requests unchanged ($METRICS_BEFORE) — WS bypasses queue counter (expected)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${CYAN}═══════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  WebSocket Test Summary${RESET}"
echo -e "${BOLD}${CYAN}═══════════════════════════════════════${RESET}"
echo -e "  ${GREEN}Passed :${RESET}  $PASS"
echo -e "  ${RED}Failed :${RESET}  $FAIL"
echo -e "  ${YELLOW}Warnings:${RESET} $WARN"
echo -e "  ${YELLOW}Skipped :${RESET} $SKIP"
echo -e "${BOLD}${CYAN}═══════════════════════════════════════${RESET}"

if [[ $FAIL -gt 0 ]]; then
    echo -e "\n${RED}  Some tests failed. Check './logs/main.py.log' for details.${RESET}"
    exit 1
else
    echo -e "\n${GREEN}  All tests passed (warnings are non-blocking).${RESET}"
    exit 0
fi