#!/usr/bin/env python3
"""Live smoke test for the A2A protocol endpoints.

Usage:
    # 1. Start PocketPaw with A2A enabled:
    #    POCKETPAW_A2A_ENABLED=true uv run pocketpaw
    #
    # 2. Run this script:
    #    uv run python scripts/test_a2a_live.py
    #
    # Optional: pass a custom base URL:
    #    uv run python scripts/test_a2a_live.py http://localhost:9000

Runs through all major A2A features and prints PASS/FAIL for each check.
"""

from __future__ import annotations

import json
import sys
import time

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8888"
TIMEOUT = 130.0  # slightly above the default 120s task timeout

passed = 0
failed = 0
task_id: str | None = None


def ok(label: str, detail: str = "") -> None:
    global passed
    passed += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  PASS  {label}{suffix}")


def fail(label: str, detail: str = "") -> None:
    global failed
    failed += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  FAIL  {label}{suffix}")


def jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    resp = httpx.post(f"{BASE_URL}/a2a", json=payload, timeout=TIMEOUT)
    return resp.json()


# -------------------------------------------------------------------------
# 1. Agent Card
# -------------------------------------------------------------------------
def test_agent_card() -> None:
    print("\n[1] Agent Card")
    resp = httpx.get(f"{BASE_URL}/.well-known/agent.json", timeout=10)
    if resp.status_code != 200:
        fail("GET /.well-known/agent.json", f"status {resp.status_code}")
        return
    ok("GET /.well-known/agent.json", f"status {resp.status_code}")

    card = resp.json()

    for field in ("name", "description", "url", "version", "capabilities", "skills"):
        if field in card:
            val = card[field]
            detail = f"{val!r:.60s}" if not isinstance(val, dict | list) else ""
            ok(f"has '{field}'", detail)
        else:
            fail(f"has '{field}'")

    if card.get("protocol_version") == "0.2.5":
        ok("protocol_version = 0.2.5")
    else:
        fail("protocol_version", f"got {card.get('protocol_version')!r}")

    caps = card.get("capabilities", {})
    if caps.get("streaming") is True:
        ok("capabilities.streaming = true")
    else:
        fail("capabilities.streaming", f"got {caps.get('streaming')!r}")

    # Alias path
    resp2 = httpx.get(f"{BASE_URL}/.well-known/agent-card.json", timeout=10)
    if resp2.status_code == 200:
        ok("alias /.well-known/agent-card.json")
    else:
        fail("alias /.well-known/agent-card.json", f"status {resp2.status_code}")


# -------------------------------------------------------------------------
# 2. Blocking message/send
# -------------------------------------------------------------------------
def test_message_send() -> None:
    global task_id
    print("\n[2] message/send (blocking)")

    body = jsonrpc(
        "message/send",
        {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "What is 2+2? Reply with just the number."}],
            },
        },
    )

    if "error" in body:
        fail("message/send", f"error: {body['error']}")
        return

    result = body.get("result", {})
    task_id = result.get("id")

    if task_id:
        ok("returned task id", task_id[:16])
    else:
        fail("returned task id")

    state = result.get("status", {}).get("state")
    if state == "completed":
        ok("state = completed")
    elif state == "failed":
        parts = result.get("status", {}).get("message", {}).get("parts", [{}])
        fail("state = failed", parts[0].get("text", "")[:80])
        return
    else:
        fail("state", f"got {state!r}")

    msg = result.get("status", {}).get("message", {})
    parts = msg.get("parts", [])
    reply_text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
    if reply_text:
        ok("got reply", reply_text[:80])
    else:
        fail("got reply", "empty")

    history = result.get("history", [])
    if len(history) >= 2:
        ok(f"history has {len(history)} messages")
    else:
        fail("history length", f"got {len(history)}")

    artifacts = result.get("artifacts", [])
    if artifacts:
        ok(f"has {len(artifacts)} artifact(s)")
    else:
        fail("has artifacts", "none returned")


# -------------------------------------------------------------------------
# 3. tasks/get + historyLength
# -------------------------------------------------------------------------
def test_tasks_get() -> None:
    print("\n[3] tasks/get + historyLength")
    if not task_id:
        fail("skipped (no task_id from step 2)")
        return

    body = jsonrpc("tasks/get", {"id": task_id})
    if "error" in body:
        fail("tasks/get", f"error: {body['error']}")
        return
    ok("tasks/get", f"state={body['result'].get('status', {}).get('state')}")

    # With history_length=0
    body0 = jsonrpc("tasks/get", {"id": task_id, "history_length": 0})
    if "error" not in body0 and body0.get("result", {}).get("history") == []:
        ok("history_length=0 returns empty")
    else:
        fail("history_length=0", f"got {body0}")

    # With history_length=1
    body1 = jsonrpc("tasks/get", {"id": task_id, "history_length": 1})
    hist = body1.get("result", {}).get("history", [])
    if len(hist) == 1:
        ok("history_length=1 returns 1 message")
    else:
        fail("history_length=1", f"got {len(hist)} messages")

    # Not found
    body404 = jsonrpc("tasks/get", {"id": "nonexistent-task-id-xyz"})
    if "error" in body404 and body404["error"].get("code") == -32001:
        ok("not found returns -32001")
    else:
        fail("not found error code", f"got {body404}")


# -------------------------------------------------------------------------
# 4. Terminal state guard
# -------------------------------------------------------------------------
def test_terminal_guard() -> None:
    print("\n[4] Terminal state guard")
    if not task_id:
        fail("skipped (no task_id)")
        return

    body = jsonrpc(
        "message/send",
        {
            "id": task_id,
            "message": {"role": "user", "parts": [{"type": "text", "text": "More?"}]},
        },
    )
    if "error" in body and body["error"].get("code") == -32003:
        ok("rejected with TASK_NOT_MODIFIABLE (-32003)")
    else:
        fail("terminal guard", f"got {body}")


# -------------------------------------------------------------------------
# 5. Output mode rejection
# -------------------------------------------------------------------------
def test_output_mode_rejection() -> None:
    print("\n[5] Output mode validation")

    body = jsonrpc(
        "message/send",
        {
            "message": {"role": "user", "parts": [{"type": "text", "text": "Hi"}]},
            "configuration": {"accepted_output_modes": ["video/mp4"]},
        },
    )
    if "error" in body and body["error"].get("code") == -32005:
        ok("incompatible modes rejected (-32005)")
    else:
        fail("output mode rejection", f"got {body}")

    # Compatible mode should not trigger -32005
    body2 = jsonrpc(
        "message/send",
        {
            "message": {"role": "user", "parts": [{"type": "text", "text": "Hi"}]},
            "configuration": {"accepted_output_modes": ["text/plain", "application/json"]},
        },
    )
    if "error" not in body2 or body2.get("error", {}).get("code") != -32005:
        ok("compatible modes accepted")
    else:
        fail("compatible modes", f"got {body2}")


# -------------------------------------------------------------------------
# 6. Task cancellation
# -------------------------------------------------------------------------
def test_cancel() -> None:
    print("\n[6] Task cancellation")
    if not task_id:
        fail("skipped (no task_id)")
        return

    # Cancel a terminal task should fail
    body = jsonrpc("tasks/cancel", {"id": task_id})
    if "error" in body and body["error"].get("code") == -32002:
        ok("cancel terminal task rejected (-32002)")
    else:
        fail("cancel terminal", f"got {body}")

    # Cancel nonexistent
    body2 = jsonrpc("tasks/cancel", {"id": "nonexistent-xyz"})
    if "error" in body2 and body2["error"].get("code") == -32001:
        ok("cancel not found returns -32001")
    else:
        fail("cancel not found", f"got {body2}")


# -------------------------------------------------------------------------
# 7. Streaming (SSE)
# -------------------------------------------------------------------------
def test_streaming() -> None:
    print("\n[7] message/stream (SSE)")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/stream",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "Say hello in one word."}],
            },
        },
    }
    events: list[dict] = []
    try:
        with httpx.stream(
            "POST",
            f"{BASE_URL}/a2a",
            json=payload,
            timeout=TIMEOUT,
            headers={"Accept": "text/event-stream"},
        ) as resp:
            if resp.status_code != 200:
                fail("SSE response", f"status {resp.status_code}")
                return
            ok("SSE response status 200")

            for line in resp.iter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        events.append(json.loads(data_str))
                    except json.JSONDecodeError:
                        pass
    except httpx.ReadTimeout:
        fail("SSE stream timed out")
        return

    if not events:
        fail("no SSE events received")
        return

    ok(f"received {len(events)} SSE event(s)")

    # Check for status updates
    states_seen = []
    for evt in events:
        result = evt.get("result", {})
        status = result.get("status", {})
        if "state" in status:
            states_seen.append(status["state"])

    if states_seen:
        ok(f"status states: {' -> '.join(states_seen)}")
    else:
        fail("no status states in events")

    # Check final event
    last_event = events[-1]
    last_result = last_event.get("result", {})
    is_final = last_result.get("final", False)
    last_state = last_result.get("status", {}).get("state", "")
    if is_final and last_state in ("completed", "failed"):
        ok(f"final event: state={last_state}, final=true")
    else:
        fail("final event", f"final={is_final}, state={last_state!r}")

    # Check for artifact events
    artifact_events = [e for e in events if "artifact" in e.get("result", {})]
    if artifact_events:
        ok(f"{len(artifact_events)} artifact event(s)")
        # Check stable artifact_id
        artifact_ids = {e["result"]["artifact"].get("artifact_id") for e in artifact_events}
        if len(artifact_ids) == 1:
            ok("stable artifact_id across chunks")
        else:
            fail("artifact_id stability", f"saw {len(artifact_ids)} different IDs")
    else:
        fail("no artifact events")


# -------------------------------------------------------------------------
# 8. JSON-RPC error handling
# -------------------------------------------------------------------------
def test_jsonrpc_errors() -> None:
    print("\n[8] JSON-RPC error handling")

    # Parse error
    resp = httpx.post(f"{BASE_URL}/a2a", content=b"not json", timeout=10)
    body = resp.json()
    if body.get("error", {}).get("code") == -32700:
        ok("parse error (-32700)")
    else:
        fail("parse error", f"got {body}")

    # Method not found
    body2 = jsonrpc("nonexistent/method")
    if body2.get("error", {}).get("code") == -32601:
        ok("method not found (-32601)")
    else:
        fail("method not found", f"got {body2}")

    # Push notifications unsupported
    body3 = jsonrpc("tasks/pushNotificationConfig/set", {"id": "x"})
    if body3.get("error", {}).get("code") == -32004:
        ok("push notifications unsupported (-32004)")
    else:
        fail("push notifications", f"got {body3}")

    # tasks/resubscribe (streaming method, returns SSE)
    payload4 = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tasks/resubscribe",
        "params": {"id": "nonexistent-xyz"},
    }
    try:
        resp4 = httpx.post(f"{BASE_URL}/a2a", json=payload4, timeout=10)
        # Server returns SSE stream; parse first data line for the error
        text = resp4.text
        if "error" in text or "-32001" in text:
            ok("tasks/resubscribe not-found returns error in SSE")
        else:
            fail("tasks/resubscribe", f"unexpected: {text[:100]}")
    except Exception as e:
        fail("tasks/resubscribe", str(e))


# -------------------------------------------------------------------------
# 9. REST endpoints
# -------------------------------------------------------------------------
def test_rest_endpoints() -> None:
    print("\n[9] REST endpoints")

    # POST /a2a/tasks/send
    resp = httpx.post(
        f"{BASE_URL}/a2a/tasks/send",
        json={
            "message": {"role": "user", "parts": [{"type": "text", "text": "Say ok."}]},
        },
        timeout=TIMEOUT,
    )
    if resp.status_code == 200:
        body = resp.json()
        rest_task_id = body.get("id")
        state = body.get("status", {}).get("state")
        ok("POST /a2a/tasks/send", f"state={state}")

        # GET /a2a/tasks/{id}
        if rest_task_id:
            resp2 = httpx.get(f"{BASE_URL}/a2a/tasks/{rest_task_id}", timeout=10)
            if resp2.status_code == 200:
                ok("GET /a2a/tasks/{id}", f"state={resp2.json().get('status', {}).get('state')}")
            else:
                fail("GET /a2a/tasks/{id}", f"status {resp2.status_code}")

            # GET with history_length
            resp3 = httpx.get(f"{BASE_URL}/a2a/tasks/{rest_task_id}?history_length=0", timeout=10)
            if resp3.status_code == 200 and resp3.json().get("history") == []:
                ok("GET with history_length=0")
            else:
                fail("GET with history_length=0")
    else:
        fail("POST /a2a/tasks/send", f"status {resp.status_code}")

    # GET /a2a/tasks/nonexistent
    resp404 = httpx.get(f"{BASE_URL}/a2a/tasks/nonexistent-xyz", timeout=10)
    if resp404.status_code == 404:
        ok("GET nonexistent task returns 404")
    else:
        fail("GET nonexistent task", f"status {resp404.status_code}")


# -------------------------------------------------------------------------
# 10. Agent Card caching
# -------------------------------------------------------------------------
def test_card_caching() -> None:
    print("\n[10] Agent Card caching")
    t0 = time.monotonic()
    resp1 = httpx.get(f"{BASE_URL}/.well-known/agent.json", timeout=10)
    t1 = time.monotonic()
    resp2 = httpx.get(f"{BASE_URL}/.well-known/agent.json", timeout=10)
    t2 = time.monotonic()

    if resp1.status_code == 200 and resp2.status_code == 200:
        ok("both requests returned 200")
        if resp1.json() == resp2.json():
            ok("identical response (cache hit)")
        else:
            fail("responses differ")
        # Second should be faster (cached), but not a hard assertion
        d1, d2 = t1 - t0, t2 - t1
        ok(f"timing: first={d1:.3f}s, second={d2:.3f}s")
    else:
        fail("card caching", f"status1={resp1.status_code}, status2={resp2.status_code}")


# -------------------------------------------------------------------------
# Run all
# -------------------------------------------------------------------------
def main() -> None:
    print("A2A Live Smoke Test")
    print(f"Target: {BASE_URL}")
    print("=" * 60)

    # Quick connectivity check
    try:
        httpx.get(f"{BASE_URL}/.well-known/agent.json", timeout=5)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException):
        print(f"\nERROR: Cannot connect to {BASE_URL}")
        print("Make sure PocketPaw is running with A2A enabled:")
        print('  set "POCKETPAW_A2A_ENABLED=true"')
        print("  uv run pocketpaw")
        sys.exit(1)

    test_agent_card()
    test_message_send()
    test_tasks_get()
    test_terminal_guard()
    test_output_mode_rejection()
    test_cancel()
    test_streaming()
    test_jsonrpc_errors()
    test_rest_endpoints()
    test_card_caching()

    print("\n" + "=" * 60)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if failed > 0:
        sys.exit(1)
    else:
        print("\nAll checks passed!")


if __name__ == "__main__":
    main()
