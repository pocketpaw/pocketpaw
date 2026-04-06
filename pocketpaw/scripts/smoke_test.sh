#!/usr/bin/env bash
# Smoke test for PocketPaw server.
# Starts the server, polls until ready, hits key endpoints, and reports results.
# Exits non-zero if any check fails. Cleans up the server process on exit.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL="http://localhost:8888"
STARTUP_TIMEOUT=30      # seconds to wait for health endpoint
CURL_TIMEOUT=5          # per-request timeout
SERVER_PID=""
PASS_COUNT=0
FAIL_COUNT=0

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log_pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    echo -e "  ${GREEN}PASS${NC}  $1"
}

log_fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo -e "  ${RED}FAIL${NC}  $1"
}

log_info() {
    echo -e "  ${CYAN}INFO${NC}  $1"
}

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        log_info "Stopping PocketPaw server (PID $SERVER_PID)..."
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Start server
# ---------------------------------------------------------------------------
echo ""
echo -e "${CYAN}=== PocketPaw Smoke Test ===${NC}"
echo ""

log_info "Starting PocketPaw server..."

# Change to project root so uv can find pyproject.toml
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

uv run pocketpaw > /tmp/pocketpaw_smoke.log 2>&1 &
SERVER_PID=$!

log_info "Server PID: $SERVER_PID"

# ---------------------------------------------------------------------------
# Wait for server to be ready
# ---------------------------------------------------------------------------
log_info "Waiting for server to be ready (timeout: ${STARTUP_TIMEOUT}s)..."

ELAPSED=0
while [ $ELAPSED -lt $STARTUP_TIMEOUT ]; do
    if curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$BASE_URL/api/v1/health" 2>/dev/null | grep -q "200"; then
        break
    fi

    # Check if server process died
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo ""
        log_fail "Server process died during startup. Last 20 lines of log:"
        tail -20 /tmp/pocketpaw_smoke.log 2>/dev/null || true
        exit 1
    fi

    sleep 1
    ELAPSED=$((ELAPSED + 1))
done

if [ $ELAPSED -ge $STARTUP_TIMEOUT ]; then
    log_fail "Server did not become ready within ${STARTUP_TIMEOUT}s"
    echo "  Last 20 lines of server log:"
    tail -20 /tmp/pocketpaw_smoke.log 2>/dev/null || true
    exit 1
fi

log_info "Server ready after ${ELAPSED}s"
echo ""

# ---------------------------------------------------------------------------
# Test 1: Health endpoint returns 200
# ---------------------------------------------------------------------------
HTTP_CODE=$(curl -s -o /tmp/pocketpaw_health.json -w "%{http_code}" \
    --max-time $CURL_TIMEOUT "$BASE_URL/api/v1/health")

if [ "$HTTP_CODE" = "200" ]; then
    log_pass "Health endpoint returned 200"
else
    log_fail "Health endpoint returned $HTTP_CODE (expected 200)"
fi

# ---------------------------------------------------------------------------
# Test 2: Version endpoint returns 200 with version field
# ---------------------------------------------------------------------------
HTTP_CODE=$(curl -s -o /tmp/pocketpaw_version.json -w "%{http_code}" \
    --max-time $CURL_TIMEOUT "$BASE_URL/api/v1/version")

if [ "$HTTP_CODE" = "200" ]; then
    # Check that response contains a "version" key
    if grep -q '"version"' /tmp/pocketpaw_version.json 2>/dev/null; then
        VERSION=$(python3 -c "import json; print(json.load(open('/tmp/pocketpaw_version.json'))['version'])" 2>/dev/null || echo "unknown")
        log_pass "Version endpoint returned 200 (v$VERSION)"
    else
        log_fail "Version endpoint returned 200 but missing 'version' field"
    fi
else
    log_fail "Version endpoint returned $HTTP_CODE (expected 200)"
fi

# ---------------------------------------------------------------------------
# Test 3: Dashboard serves HTML at /
# ---------------------------------------------------------------------------
HTTP_CODE=$(curl -s -o /tmp/pocketpaw_dashboard.html -w "%{http_code}" \
    --max-time $CURL_TIMEOUT "$BASE_URL/")

if [ "$HTTP_CODE" = "200" ]; then
    if grep -qi '<html' /tmp/pocketpaw_dashboard.html 2>/dev/null; then
        log_pass "Dashboard returned 200 with HTML content"
    else
        log_fail "Dashboard returned 200 but response is not HTML"
    fi
else
    log_fail "Dashboard returned $HTTP_CODE (expected 200)"
fi

# ---------------------------------------------------------------------------
# Test 4: OpenAPI spec is accessible
# ---------------------------------------------------------------------------
HTTP_CODE=$(curl -s -o /tmp/pocketpaw_openapi.json -w "%{http_code}" \
    --max-time $CURL_TIMEOUT "$BASE_URL/api/v1/openapi.json")

if [ "$HTTP_CODE" = "200" ]; then
    if grep -q '"openapi"' /tmp/pocketpaw_openapi.json 2>/dev/null; then
        log_pass "OpenAPI spec returned 200 with valid schema"
    else
        log_fail "OpenAPI spec returned 200 but missing 'openapi' field"
    fi
else
    log_fail "OpenAPI spec returned $HTTP_CODE (expected 200)"
fi

# ---------------------------------------------------------------------------
# Test 5: Sessions endpoint responds (create session)
# ---------------------------------------------------------------------------
HTTP_CODE=$(curl -s -o /tmp/pocketpaw_session.json -w "%{http_code}" \
    --max-time $CURL_TIMEOUT \
    -X POST "$BASE_URL/api/v1/sessions")

if [ "$HTTP_CODE" = "200" ]; then
    if grep -q '"id"' /tmp/pocketpaw_session.json 2>/dev/null; then
        log_pass "Create session returned 200 with session ID"
    else
        log_fail "Create session returned 200 but missing 'id' field"
    fi
else
    log_fail "Create session returned $HTTP_CODE (expected 200)"
fi

# ---------------------------------------------------------------------------
# Test 6: Sessions list endpoint responds
# ---------------------------------------------------------------------------
HTTP_CODE=$(curl -s -o /tmp/pocketpaw_sessions_list.json -w "%{http_code}" \
    --max-time $CURL_TIMEOUT "$BASE_URL/api/v1/sessions")

if [ "$HTTP_CODE" = "200" ]; then
    log_pass "List sessions returned 200"
else
    log_fail "List sessions returned $HTTP_CODE (expected 200)"
fi

# ---------------------------------------------------------------------------
# Test 7: Settings endpoint responds
# ---------------------------------------------------------------------------
HTTP_CODE=$(curl -s -o /tmp/pocketpaw_settings.json -w "%{http_code}" \
    --max-time $CURL_TIMEOUT "$BASE_URL/api/v1/settings")

if [ "$HTTP_CODE" = "200" ]; then
    log_pass "Settings endpoint returned 200"
else
    # Settings may require auth even on localhost in some configs — warn, don't fail hard
    if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
        log_info "Settings endpoint returned $HTTP_CODE (auth required — skipping)"
    else
        log_fail "Settings endpoint returned $HTTP_CODE (expected 200)"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
TOTAL=$((PASS_COUNT + FAIL_COUNT))
echo -e "${CYAN}=== Results: ${GREEN}${PASS_COUNT} passed${NC}, ${RED}${FAIL_COUNT} failed${NC} out of ${TOTAL} checks ===${NC}"
echo ""

if [ $FAIL_COUNT -gt 0 ]; then
    echo -e "${RED}Smoke test FAILED${NC}"
    exit 1
else
    echo -e "${GREEN}Smoke test PASSED${NC}"
    exit 0
fi
