# Resumable Chat Runs v2 — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship resumable agent chat runs that survive refresh, session switch, and tab close+reopen — without recreating the dual-store / cursor / per-scope-run-state bugs that killed the v1 client.

**Architecture:** Backend Run + Redis Stream durability layer already exists. POST `/agent` already streams SSE (shipped 2026-05-23). Only new backend work: TTL bump, a stale-run sweeper, and a few tests. Frontend gets one new pure-transport helper (`subscribeRunStreamSSE` in `src/lib/core/chat/service.ts`) plus per-surface resume on history load. Each surface (os/ChatPanel via chatRoomsStore, explorer/ChatSidebar via chatStore) owns its own SSE callbacks. No shared `resumeRun()`, no cursors, no per-scope run state.

**Tech Stack:** Backend — Python 3.11, FastAPI, asyncio, Redis (Streams), Mongo via Beanie, pytest. Frontend — SvelteKit 2, Svelte 5 runes, TypeScript, Vitest, Bun. Both repos use the existing `feat/resumable-chat-runs-tier1` (backend) and `feat/chat-resume-via-repost` (frontend, cut from dev).

**Design doc:** `D:\paw\backend\docs\plans\2026-05-23-resumable-chat-runs-v2-design.md`

---

## Pre-flight

### Task 0: Verify branch state and current backend

**Files:** none

**Step 1: Confirm backend branch**

Run: `git -C /d/paw/backend branch --show-current`
Expected: `feat/resumable-chat-runs-tier1`

**Step 2: Confirm frontend branch**

Run: `git -C /d/paw/paw-enterprise branch --show-current`
Expected: `feat/chat-resume-via-repost`

**Step 3: Confirm backend agent_router on this branch already streams SSE**

Run: `grep -n "StreamingResponse" /d/paw/backend/ee/pocketpaw_ee/cloud/chat/agent_router.py | head`
Expected: line 19 import + line 56 return type + line 133 the return.

**Step 4: Confirm frontend service.ts has streamAgentSSE**

Run: `grep -n "export async function streamAgentSSE" /d/paw/paw-enterprise/src/lib/core/chat/service.ts`
Expected: one line with the function declaration.

If any check fails, stop and resolve before proceeding.

---

## Phase 1 — Backend: TTL + sweeper

### Task 1: Bump default run stream TTL from 900s to 3600s

**Files:**
- Modify: `D:\paw\backend\ee\pocketpaw_ee\cloud\chat\runs\run_core.py:43-44`
- Modify: `D:\paw\backend\CLAUDE.md` (the env-var doc block that mentions `POCKETPAW_CLOUD_RUN_STREAM_TTL`)

**Step 1: Update the default in run_core.py**

Find:
```python
def _stream_ttl() -> int:
    return int(os.environ.get("POCKETPAW_CLOUD_RUN_STREAM_TTL", "900"))
```

Replace `"900"` with `"3600"`.

**Step 2: Update the env-var doc**

In `D:\paw\backend\CLAUDE.md`, find the line beginning `POCKETPAW_CLOUD_RUN_STREAM_TTL` and change the default value reference from `900` to `3600`.

**Step 3: Run the runs tests to confirm no regression**

Run: `cd /d/paw/backend && uv run pytest tests/cloud/runs -q`
Expected: all pass.

**Step 4: Commit**

```bash
cd /d/paw/backend
git add ee/pocketpaw_ee/cloud/chat/runs/run_core.py CLAUDE.md
git commit -m "feat(runs): bump default run stream TTL 900s → 3600s"
```

---

### Task 2: Add a failing test for the stale-run sweeper

**Files:**
- Test: `D:\paw\backend\tests\cloud\runs\test_sweeper.py` (create)

**Step 1: Write the failing test**

Create `D:\paw\backend\tests\cloud\runs\test_sweeper.py`:

```python
"""Stale-run sweeper marks queued/running ChatRunDocs as interrupted when
they've outlived the threshold — the backend process died, the executor task
is gone, but Mongo still says ``running``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.chat.runs import sweeper
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

pytestmark = pytest.mark.asyncio


def _make_run(*, status: str, created_minutes_ago: int) -> ChatRunDoc:
    created = datetime.now(UTC) - timedelta(minutes=created_minutes_ago)
    return ChatRunDoc(
        run_id=f"r-{status}-{created_minutes_ago}",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id=f"c-{status}-{created_minutes_ago}",
        user_message_id="um1",
        status=status,  # type: ignore[arg-type]
        createdAt=created,
    )


async def test_sweep_marks_stale_running_as_interrupted(mongo_db):  # noqa: ARG001
    stale = _make_run(status="running", created_minutes_ago=30)
    fresh = _make_run(status="running", created_minutes_ago=2)
    queued_stale = _make_run(status="queued", created_minutes_ago=30)
    completed = _make_run(status="completed", created_minutes_ago=30)
    await stale.insert()
    await fresh.insert()
    await queued_stale.insert()
    await completed.insert()

    n = await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert n == 2  # stale + queued_stale
    refreshed = await ChatRunDoc.find_one(ChatRunDoc.run_id == stale.run_id)
    assert refreshed is not None and refreshed.status == "interrupted"
    refreshed_fresh = await ChatRunDoc.find_one(ChatRunDoc.run_id == fresh.run_id)
    assert refreshed_fresh is not None and refreshed_fresh.status == "running"
    refreshed_completed = await ChatRunDoc.find_one(ChatRunDoc.run_id == completed.run_id)
    assert refreshed_completed is not None and refreshed_completed.status == "completed"


async def test_sweep_with_no_stale_runs_returns_zero(mongo_db):  # noqa: ARG001
    fresh = _make_run(status="running", created_minutes_ago=2)
    await fresh.insert()

    n = await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert n == 0
```

**Step 2: Run test to verify it fails**

Run: `cd /d/paw/backend && uv run pytest tests/cloud/runs/test_sweeper.py -v`
Expected: FAIL with `ModuleNotFoundError: pocketpaw_ee.cloud.chat.runs.sweeper`

**Step 3: Commit (test only — TDD red)**

```bash
cd /d/paw/backend
git add tests/cloud/runs/test_sweeper.py
git commit -m "test(runs): failing test for stale-run sweeper"
```

---

### Task 3: Implement the sweeper

**Files:**
- Create: `D:\paw\backend\ee\pocketpaw_ee\cloud\chat\runs\sweeper.py`

**Step 1: Implement**

Create `D:\paw\backend\ee\pocketpaw_ee\cloud\chat\runs\sweeper.py`:

```python
"""Stale-run sweeper.

If the backend process dies mid-run, the executor's asyncio task is gone but
Mongo still says ``running``. The sweeper marks anything that's been sitting
in queued/running past the threshold as ``interrupted`` so the client can
render a retry affordance instead of subscribing to a stream nobody is
writing to.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

logger = logging.getLogger(__name__)


async def sweep_stale_runs(*, older_than_minutes: int = 10) -> int:
    """Mark queued/running runs older than ``older_than_minutes`` as ``interrupted``.

    Returns the number of docs updated.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    stale = await ChatRunDoc.find(
        {"status": {"$in": ["queued", "running"]}},
        ChatRunDoc.createdAt < cutoff,
    ).to_list()
    if not stale:
        return 0
    now = datetime.now(UTC)
    for doc in stale:
        doc.status = "interrupted"  # type: ignore[assignment]
        doc.ended_at = now
        await doc.save()
    logger.info("sweep_stale_runs: marked %d runs as interrupted", len(stale))
    return len(stale)
```

**Step 2: Run test to verify it passes**

Run: `cd /d/paw/backend && uv run pytest tests/cloud/runs/test_sweeper.py -v`
Expected: both tests PASS.

**Step 3: Lint + type-check**

Run: `cd /d/paw/backend && uv run ruff check ee/pocketpaw_ee/cloud/chat/runs/sweeper.py && uv run mypy ee/pocketpaw_ee/cloud/chat/runs/sweeper.py`
Expected: clean.

**Step 4: Commit**

```bash
cd /d/paw/backend
git add ee/pocketpaw_ee/cloud/chat/runs/sweeper.py
git commit -m "feat(runs): stale-run sweeper marks abandoned runs as interrupted"
```

---

### Task 4: Wire sweeper into cloud lifecycle (startup + 5-min recurring tick)

**Files:**
- Modify: `D:\paw\backend\ee\pocketpaw_ee\cloud\_core\dashboard_lifecycle.py` (or wherever cloud startup lives)

**Step 1: Locate the cloud lifecycle module**

Run:
```bash
cd /d/paw/backend && grep -rln "dashboard_lifecycle\|mount_cloud" ee/pocketpaw_ee/cloud/_core/ | head -5
```

If the file is something different (e.g., `lifespan.py`), substitute it below. The memory `on_event_dead_under_lifespan` says cloud startup is wired into `dashboard_lifecycle` — that's the target.

**Step 2: Add the sweeper hook**

In the cloud lifecycle module, add a startup block that:
1. Calls `await sweep_stale_runs()` once on boot.
2. Spawns an `asyncio.create_task(...)` that loops `await asyncio.sleep(300); await sweep_stale_runs()` until shutdown.

Sketch (adjust to match the existing lifecycle pattern in this file):

```python
import asyncio
import logging
from contextlib import suppress

logger = logging.getLogger(__name__)

_sweeper_task: asyncio.Task | None = None

async def _sweeper_loop() -> None:
    from pocketpaw_ee.cloud.chat.runs.sweeper import sweep_stale_runs
    while True:
        try:
            await asyncio.sleep(300)
            await sweep_stale_runs()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweep_stale_runs tick failed")

async def start_run_sweeper() -> None:
    """Call from cloud startup. Sweeps once, then ticks every 5 minutes."""
    from pocketpaw_ee.cloud.chat.runs.sweeper import sweep_stale_runs
    global _sweeper_task
    with suppress(Exception):
        await sweep_stale_runs()  # initial pass
    _sweeper_task = asyncio.create_task(_sweeper_loop())

async def stop_run_sweeper() -> None:
    """Call from cloud shutdown."""
    global _sweeper_task
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        with suppress(asyncio.CancelledError):
            await _sweeper_task
        _sweeper_task = None
```

Then call `await start_run_sweeper()` from the cloud startup path and `await stop_run_sweeper()` from shutdown. Match the existing pattern in the file — do not invent new hook plumbing.

**Step 3: Run the broader cloud tests to confirm no regression**

Run: `cd /d/paw/backend && uv run pytest tests/cloud/runs tests/cloud/test_agent_router.py tests/cloud/test_agent_router_history_rehydrate.py -q`
Expected: all pass.

**Step 4: Commit**

```bash
cd /d/paw/backend
git add ee/pocketpaw_ee/cloud/_core/dashboard_lifecycle.py  # adjust path
git commit -m "feat(runs): wire stale-run sweeper into cloud lifecycle (startup + 5min tick)"
```

---

### Task 5: Add test for POST /agent/stop idempotency

**Files:**
- Modify: `D:\paw\backend\tests\cloud\test_agent_router.py` (append)

**Step 1: Append two tests**

Add at the end of the file:

```python
async def test_post_agent_stop_cancels_active_run(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """POST /agent/stop calls request_cancel on the active run for the scope."""
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

    spec = RunSpec(
        run_id="run-to-cancel",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="um1",
        content="hi",
        history=[],
        intent=None,
        attachments=[],
        mentions=[],
        reply_to=None,
    )
    await run_service.create_run(spec)

    cancelled: list[str] = []

    class _StubTransport:
        async def request_cancel(self, run_id: str) -> None:
            cancelled.append(run_id)

    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    resp = await cloud_app_client.post("/cloud/chat/session/s1/agent/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert cancelled == ["run-to-cancel"]


async def test_post_agent_stop_is_noop_when_no_active_run(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """No active run for scope → still returns ok (idempotent)."""
    from pocketpaw_ee.cloud.chat import agent_router as mod

    cancelled: list[str] = []

    class _StubTransport:
        async def request_cancel(self, run_id: str) -> None:
            cancelled.append(run_id)

    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    resp = await cloud_app_client.post("/cloud/chat/session/s-nonexistent/agent/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert cancelled == []
```

**Step 2: Run the tests**

Run: `cd /d/paw/backend && uv run pytest tests/cloud/test_agent_router.py -v`
Expected: 4 tests pass (2 existing + 2 new).

**Step 3: Commit**

```bash
cd /d/paw/backend
git add tests/cloud/test_agent_router.py
git commit -m "test(runs): cover POST /agent/stop idempotent + no-op cases"
```

---

## Phase 2 — Frontend: pure transport helper

### Task 6: Add subscribeRunStreamSSE failing test

**Files:**
- Test: `D:\paw\paw-enterprise\src\lib\core\chat\__tests__\subscribe-run-stream.test.ts` (create)

**Step 1: Write the failing test**

Create `D:\paw\paw-enterprise\src\lib\core\chat\__tests__\subscribe-run-stream.test.ts`:

```ts
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { subscribeRunStreamSSE } from "../service";

const ORIGINAL_FETCH = globalThis.fetch;

function makeSseBody(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      controller.close();
    },
  });
}

describe("subscribeRunStreamSSE", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });
  afterEach(() => {
    globalThis.fetch = ORIGINAL_FETCH;
  });

  it("parses SSE frames and dispatches to callbacks", async () => {
    const body = makeSseBody([
      'event: stream_start\ndata: {"run_id":"r1"}\n\n',
      'event: chunk\ndata: {"content":"hello"}\n\n',
      'event: chunk\ndata: {"content":" world"}\n\n',
      'event: stream_end\ndata: {"assistant_message_id":"m1"}\n\n',
    ]);
    (globalThis.fetch as any).mockResolvedValue(
      new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    );

    const chunks: string[] = [];
    let started = false;
    let ended: any = null;
    await subscribeRunStreamSSE("r1", new AbortController().signal, {
      onStreamStart: () => (started = true),
      onChunk: (d) => chunks.push(d.content),
      onStreamEnd: (d) => (ended = d),
    });
    expect(started).toBe(true);
    expect(chunks).toEqual(["hello", " world"]);
    expect(ended).toEqual({ assistant_message_id: "m1" });
  });

  it("calls onAbort when the signal aborts", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        // never close; will be cancelled
        controller.enqueue(new TextEncoder().encode('event: chunk\ndata: {"content":"x"}\n\n'));
      },
    });
    const ctrl = new AbortController();
    (globalThis.fetch as any).mockImplementation((url: string, opts: RequestInit) => {
      opts.signal?.addEventListener("abort", () => {
        try { body.cancel(); } catch { /* ok */ }
      });
      return Promise.resolve(
        new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
      );
    });
    const onAbort = vi.fn();
    const p = subscribeRunStreamSSE("r1", ctrl.signal, { onAbort, onChunk: () => {} });
    queueMicrotask(() => ctrl.abort());
    await p;
    expect(onAbort).toHaveBeenCalled();
  });

  it("skips unparseable frames without killing the stream", async () => {
    const body = makeSseBody([
      "event: chunk\ndata: not-json\n\n",
      'event: chunk\ndata: {"content":"after-bad"}\n\n',
      'event: stream_end\ndata: {}\n\n',
    ]);
    (globalThis.fetch as any).mockResolvedValue(
      new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    );

    const chunks: string[] = [];
    await subscribeRunStreamSSE("r1", new AbortController().signal, {
      onChunk: (d) => chunks.push(d.content),
      onStreamEnd: () => {},
    });
    expect(chunks).toEqual(["after-bad"]);
  });

  it("calls onFetchError when fetch rejects", async () => {
    (globalThis.fetch as any).mockRejectedValue(new TypeError("network down"));
    const onFetchError = vi.fn();
    await subscribeRunStreamSSE("r1", new AbortController().signal, { onFetchError });
    expect(onFetchError).toHaveBeenCalledWith(expect.stringContaining("network"));
  });
});
```

**Step 2: Run the test**

Run: `cd /d/paw/paw-enterprise && bun run test src/lib/core/chat/__tests__/subscribe-run-stream.test.ts 2>&1 | tail -20`
Expected: FAIL — `subscribeRunStreamSSE` doesn't exist.

**Step 3: Commit the failing test**

```bash
cd /d/paw/paw-enterprise
git add src/lib/core/chat/__tests__/subscribe-run-stream.test.ts
git commit -m "test(chat): failing test for subscribeRunStreamSSE"
```

---

### Task 7: Implement subscribeRunStreamSSE

**Files:**
- Modify: `D:\paw\paw-enterprise\src\lib\core\chat\service.ts` (add new export)

**Step 1: Add the function**

In `D:\paw\paw-enterprise\src\lib\core\chat\service.ts`, find the end of the existing `streamAgentSSE` function (the closing `}` followed by a blank line before `export async function agentChat`). Insert ABOVE `agentChat`:

```ts
/**
 * Subscribe to an in-flight run's event stream via GET — pure transport.
 *
 * Used to resume a run on history-load when ``active_run.run_id`` is present.
 * Identical callback shape to ``streamAgentSSE`` so each surface reuses its
 * existing handlers; this helper is intentionally side-effect free (no store
 * imports, no shared state) so the dual-store divergence from v1 cannot recur.
 */
export async function subscribeRunStreamSSE(
  runId: string,
  signal: AbortSignal,
  callbacks: SSECallbacks,
): Promise<void> {
  const url = `${BASE_URL}/cloud/chat/runs/${encodeURIComponent(runId)}/stream?after=0`;

  let res: Response;
  try {
    res = await fetch(url, {
      method: "GET",
      credentials: "include",
      headers: authHeaders(),
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      callbacks.onAbort?.();
      return;
    }
    callbacks.onFetchError?.(friendlyErrorMessage(err));
    return;
  }

  if (!res.ok) {
    callbacks.onPreStream4xx?.(res.status, null);
    return;
  }

  const reader = res.body?.getReader();
  if (!reader) {
    callbacks.onFetchError?.("No readable stream from backend");
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";

      for (const part of parts) {
        let eventType = "message";
        let eventData = "";
        for (const line of part.split("\n")) {
          if (line.startsWith("event:")) eventType = line.slice(6).trim();
          else if (line.startsWith("data:")) eventData = line.slice(5).trim();
        }
        if (!eventData) continue;
        let data: any;
        try {
          data = JSON.parse(eventData);
        } catch {
          continue;
        }
        switch (eventType) {
          case "stream_start": callbacks.onStreamStart?.(data); break;
          case "chunk":
          case "content": callbacks.onChunk?.(data); break;
          case "thinking": callbacks.onThinking?.(data); break;
          case "tool_start": callbacks.onToolStart?.(data); break;
          case "tool_result": callbacks.onToolResult?.(data); break;
          case "ripple": callbacks.onRipple?.(data); break;
          case "pocket_created": await callbacks.onPocketCreated?.(data); break;
          case "pocket_mutation": callbacks.onPocketMutation?.(data); break;
          case "ask_user_question":
          case "ask_user": callbacks.onAskUserQuestion?.(data); break;
          case "session_titled": callbacks.onSessionTitled?.(data); break;
          case "error": callbacks.onError?.(data); break;
          case "stream_end": callbacks.onStreamEnd?.(data); break;
        }
      }
    }
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      callbacks.onAbort?.();
      return;
    }
    callbacks.onFetchError?.(friendlyErrorMessage(err));
  }
}
```

If `SSECallbacks` doesn't already include `onSessionTitled`, add it to the type definition above (it's referenced in the existing `_handleCloudSSEEvent` so it should — verify).

**Step 2: Run the test**

Run: `cd /d/paw/paw-enterprise && bun run test src/lib/core/chat/__tests__/subscribe-run-stream.test.ts 2>&1 | tail -15`
Expected: all 4 tests PASS.

**Step 3: Type-check**

Run: `cd /d/paw/paw-enterprise && bun run check 2>&1 | tail -10`
Expected: no new errors in `service.ts`.

**Step 4: Commit**

```bash
cd /d/paw/paw-enterprise
git add src/lib/core/chat/service.ts
git commit -m "feat(chat): add subscribeRunStreamSSE pure-transport helper"
```

---

## Phase 3 — Frontend surface 1: os/ChatPanel (chatRoomsStore)

### Task 8: Extract buildAgentCallbacks in os/ChatPanel

**Files:**
- Modify: `D:\paw\paw-enterprise\src\lib\components\os\ChatPanel.svelte` (the `sendAgentMessage` block)

**Step 1: Read the current shape**

Read lines around `async function sendAgentMessage` in `os/ChatPanel.svelte`. The current code is a big `await streamAgentSSE(scope, sessionMongoId, body, signal, { onChunk, onError, onStreamEnd, ... })` with inline callbacks. We're going to lift the inline object out so it can be reused by both POST (live send) and GET (resume).

**Step 2: Add the helper**

Inside the `<script>` block, near other helpers, add:

```ts
import { subscribeRunStreamSSE, streamAgentSSE } from "$lib/core/chat/service";
// (these imports may already exist — adjust)

/** Build the SSE callbacks for an agent reply landing in this room.
 *  Used by both live send and resume — the latter passes the same
 *  callbacks so the streaming bubble paints in the same place regardless
 *  of which transport opened the connection. */
function buildAgentCallbacks(roomId: string, agentMsgId: string, clientMessageId: string | null) {
  let agentContent = "";
  return {
    onMessagePersisted: (data: any) => {
      const cid = data?.client_message_id;
      const realId = data?.user_message_id;
      if (cid && realId) chatRoomsStore.swapMessageId(roomId, cid, realId);
    },
    onChunk: (data: any) => {
      if (!data?.content) return;
      agentContent += data.content;
      const msgs = chatRoomsStore.messages[roomId] ?? [];
      const existing = msgs.find((m) => m.id === agentMsgId);
      if (existing) {
        existing.content = agentContent;
        chatRoomsStore.messages = { ...chatRoomsStore.messages, [roomId]: [...msgs] };
      } else {
        const room = chatRoomsStore.rooms.find((r) => r.id === roomId);
        const name = room?.name || "PocketPaw";
        chatRoomsStore.addMessage(roomId, {
          id: agentMsgId,
          role: "agent",
          content: agentContent,
          sender: name,
          senderInitials: room ? getContactInitials(room) : "PA",
          senderColor: room ? getContactColor(room) : "#5E5CE6",
          time: nowTime(),
          timestamp: nowISO(),
        });
      }
      scrollToBottom();
    },
    onStreamEnd: (data: any) => {
      const realId = data?.assistant_message_id;
      if (realId) chatRoomsStore.swapMessageId(roomId, agentMsgId, realId);
      isTyping = false;
      scrollToBottom();
    },
    onError: (data: any) => {
      isTyping = false;
      chatRoomsStore.addMessage(roomId, {
        id: `err${Date.now()}`,
        role: "system",
        content: `Error: ${data?.detail || data?.message || "Stream failed"}`,
        time: nowTime(),
        timestamp: nowISO(),
      });
      scrollToBottom();
    },
    onAbort: () => {
      isTyping = false;
      scrollToBottom();
    },
    onFetchError: (msg: string) => {
      isTyping = false;
      if (!agentContent) {
        chatRoomsStore.addMessage(roomId, {
          id: `err${Date.now()}`,
          role: "system",
          content: `Failed to reach agent: ${msg}`,
          time: nowTime(),
          timestamp: nowISO(),
        });
      }
      scrollToBottom();
    },
    onPreStream4xx: (status: number, errBody: any) => {
      isTyping = false;
      const msg = errBody?.message || errBody?.detail || `HTTP ${status}`;
      if (!agentContent) {
        chatRoomsStore.addMessage(roomId, {
          id: `err${Date.now()}`,
          role: "system",
          content: `Failed to reach agent: ${msg}`,
          time: nowTime(),
          timestamp: nowISO(),
        });
      }
      scrollToBottom();
    },
  };
}
```

**Step 3: Replace the inline callback object in sendAgentMessage**

In `sendAgentMessage`, replace the inline `{ onChunk, onStreamEnd, ... }` object passed to `streamAgentSSE` with `buildAgentCallbacks(roomId, agentMsgId, clientMessageId)`. Keep the rest of `sendAgentMessage` unchanged.

**Step 4: Type-check**

Run: `cd /d/paw/paw-enterprise && bun run check 2>&1 | tail -10`
Expected: no new errors in `os/ChatPanel.svelte`.

**Step 5: Manually run dev server + send a message**

Run: `cd /d/paw/paw-enterprise && bun run dev` (in another shell `cd /d/paw/backend && uv run pocketpaw --dev`)
Then open `localhost:1420`, log in, send a message to PocketPaw.
Expected: streaming behaves identically to before the refactor — chunks visible, message lands.

**Step 6: Commit**

```bash
cd /d/paw/paw-enterprise
git add src/lib/components/os/ChatPanel.svelte
git commit -m "refactor(chat/os): extract buildAgentCallbacks for reuse by resume"
```

---

### Task 9: Wire resume on history load — os/ChatPanel

**Files:**
- Modify: `D:\paw\paw-enterprise\src\lib\components\os\ChatPanel.svelte` (`loadAgentSessionHistory`)

**Step 1: Add a local resume-in-flight tracker**

Inside the `<script>` block:

```ts
// Track the run_id we're currently subscribed to so a re-mount during the
// same render cycle doesn't double-subscribe (single mount, single fetch).
let resumingRunId = $state<string | null>(null);
let resumeAbort: AbortController | null = null;
```

**Step 2: Modify loadAgentSessionHistory to subscribe on active_run**

Find `async function loadAgentSessionHistory(agentRoomId: string, sessionId: string)` in `os/ChatPanel.svelte`. After the existing history population (after `chatRoomsStore.messages = { ... }; roomSessionMap[agentRoomId] = sessionId; await tick(); scrollToBottom();`), append:

```ts
        // If a run is in flight for this session, subscribe to its stream.
        const activeRun = (res as any)?.active_run;
        if (
          activeRun &&
          typeof activeRun.run_id === "string" &&
          (activeRun.status === "queued" || activeRun.status === "running") &&
          resumingRunId !== activeRun.run_id
        ) {
          // Abort any prior in-flight resume before starting a new one.
          resumeAbort?.abort();
          resumeAbort = new AbortController();
          resumingRunId = activeRun.run_id;
          isTyping = true;
          const agentMsgId = `m${Date.now() + 1}`;
          const callbacks = buildAgentCallbacks(agentRoomId, agentMsgId, null);
          // Wrap onStreamEnd / onError / onAbort to clear the resume tracker.
          const clear = () => { resumingRunId = null; resumeAbort = null; };
          const originalEnd = callbacks.onStreamEnd;
          const originalError = callbacks.onError;
          const originalAbort = callbacks.onAbort;
          callbacks.onStreamEnd = (d) => { clear(); originalEnd?.(d); };
          callbacks.onError = (d) => { clear(); originalError?.(d); };
          callbacks.onAbort = () => { clear(); originalAbort?.(); };
          subscribeRunStreamSSE(activeRun.run_id, resumeAbort.signal, callbacks).catch((err) => {
            console.warn("[chat] resume failed", err);
            clear();
            isTyping = false;
          });
        }
```

**Step 3: Abort the resume on unmount**

Find the `onDestroy` / cleanup hook in the file (or add one). Append:

```ts
onDestroy(() => {
  resumeAbort?.abort();
  resumeAbort = null;
});
```

If there's no `onDestroy` import, add `import { onDestroy } from "svelte";` at the top.

**Step 4: Type-check**

Run: `cd /d/paw/paw-enterprise && bun run check 2>&1 | tail -10`
Expected: clean.

**Step 5: Manual verify M1 (refresh mid-stream)**

Backend running (`uv run pocketpaw --dev`), frontend running (`bun run dev`), log in, send a long-ish prompt to PocketPaw, then refresh the page during streaming.
Expected: page returns, chunks already on screen replay, more chunks fill in, stream completes.

**Step 6: Manual verify M2 (session switch mid-stream)**

Send a long prompt, click a different session mid-stream, wait ~10s, click back.
Expected: A's streaming continues.

**Step 7: Commit**

```bash
cd /d/paw/paw-enterprise
git add src/lib/components/os/ChatPanel.svelte
git commit -m "feat(chat/os): resume in-flight run on history load via active_run"
```

---

### Task 10: Integration test — os/ChatPanel resume invocation

**Files:**
- Test: `D:\paw\paw-enterprise\src\lib\components\os\__tests__\chat-panel-resume.test.ts` (create)

**Step 1: Write the test**

Create the file with a focused test that:
- Mocks `getSessionHistory` to return `{ messages: [], active_run: { run_id: "r1", status: "running" } }`.
- Spies on `subscribeRunStreamSSE`.
- Mounts the panel, navigates to an agent-dm route.
- Asserts `subscribeRunStreamSSE` was called once with `"r1"` as the first argument.

Use Vitest's standard approach for Svelte 5 components. Reference the existing `src/lib/core/chat/__tests__/agent-chat-unified.test.ts` for mock patterns; mirror the structure.

(If full-mount component testing isn't already set up in the project for `os/ChatPanel.svelte`, scope this test smaller: directly test the resume-decision branch in isolation by extracting it into a small exported function `maybeResumeAgentRun(historyResponse, callbacks, signal)` — but only do that extraction if simpler.)

**Step 2: Run the test**

Run: `cd /d/paw/paw-enterprise && bun run test src/lib/components/os/__tests__/chat-panel-resume.test.ts 2>&1 | tail -15`
Expected: PASS.

**Step 3: Commit**

```bash
cd /d/paw/paw-enterprise
git add src/lib/components/os/__tests__/chat-panel-resume.test.ts
git commit -m "test(chat/os): assert resume on active_run history payload"
```

---

## Phase 4 — Frontend surface 2: explorer/ChatSidebar (chatStore)

This surface uses `chatStore` (per-session) and routes through `chatStore.sendMessage` → `service.streamChat` → `agentChat`. Resume must hook into chatStore's history-load path.

### Task 11: Locate the chatStore history-load entry point

**Files:** investigation only

**Step 1: Find where chatStore receives history**

Run: `grep -n "loadHistory\|history\|switchSession" /d/paw/paw-enterprise/src/lib/stores/chat.svelte.ts /d/paw/paw-enterprise/src/lib/core/sessions/service.ts | head -20`

Identify:
- `chatStore.loadHistory(messages)` signature.
- The function in `sessions/service.ts` that calls it (likely `switchSession` → fetches history → calls `chatStore.loadHistory(messages)`).
- The response shape returned by the history API — does it already include `active_run`?

**Step 2: Document findings**

Note in a comment at the bottom of the task what you found. The next tasks depend on this.

(No commit — investigation only.)

---

### Task 12: Plumb active_run through to chatStore

**Files:**
- Modify: `D:\paw\paw-enterprise\src\lib\stores\chat.svelte.ts` (add resume method)
- Modify: the sessions service / loadHistory caller from Task 11 to pass active_run through.

**Step 1: Add a chatStore method for "begin resume"**

In `chat.svelte.ts`, add:

```ts
private resumeAbort: AbortController | null = null;
private resumingRunId: string | null = null;

/** Begin resuming an in-flight run for the current session. Idempotent —
 *  a re-call with the same run_id is a no-op. Caller must already have
 *  populated ``this.messages`` from the history response. */
async beginResume(runId: string): Promise<void> {
  if (this.resumingRunId === runId) return;
  this.resumeAbort?.abort();
  this.resumeAbort = new AbortController();
  this.resumingRunId = runId;

  const { subscribeRunStreamSSE } = await import("$lib/core/chat/service");
  const callbacks = this.buildResumeCallbacks();
  try {
    await subscribeRunStreamSSE(runId, this.resumeAbort.signal, callbacks);
  } catch (err) {
    console.warn("[chat] resume failed", err);
  } finally {
    if (this.resumingRunId === runId) {
      this.resumingRunId = null;
      this.resumeAbort = null;
    }
  }
}

private buildResumeCallbacks() {
  // Same write semantics as the existing agentChat callbacks — mutate
  // this.streamingContent / messages / streaming flags. Mirror the live-
  // path callback shape defined in service.ts:agentChat. Do NOT call into
  // service.ts for this — keep callbacks owned by the store.
  return {
    onChunk: (data: any) => {
      this.streamingContent += data?.content ?? "";
      if (!this.isStreaming) this.isStreaming = true;
    },
    onStreamEnd: (data: any) => {
      // Persist streamingContent as the assistant message, clear streaming.
      if (this.streamingContent) {
        this.messages.push({
          role: "assistant",
          content: this.streamingContent,
          timestamp: new Date().toISOString(),
          ...(data?.assistant_message_id ? { id: data.assistant_message_id } : {}),
        });
      }
      this.isStreaming = false;
      this.streamingContent = "";
      this.streamingStatus = null;
    },
    onError: (data: any) => {
      this.error = data?.message || data?.detail || "Stream failed";
      this.isStreaming = false;
      this.streamingContent = "";
    },
    onAbort: () => {
      this.isStreaming = false;
    },
    onFetchError: (msg: string) => {
      this.error = msg;
      this.isStreaming = false;
    },
    onThinking: () => { this.streamingStatus = "Thinking..."; },
    onToolStart: (data: any) => { this.streamingStatus = `Working on ${data?.tool || "..."}`; },
    onToolResult: () => { this.streamingStatus = "Thinking..."; },
  };
}
```

**Step 2: Call beginResume from the history loader**

In the function identified in Task 11 (likely `sessions/service.ts:switchSession`), after `chatStore.loadHistory(...)` is called, inspect the response for `active_run` and call `chatStore.beginResume(active_run.run_id)` when present and non-terminal.

**Step 3: Add unmount/cleanup if needed**

If there's a route-leave hook for the `/chat` route's host, abort `chatStore.resumeAbort` on leave. If not, this is fine — the next history load will abort the prior resume via the `resumingRunId === runId` guard + new AbortController.

**Step 4: Type-check**

Run: `cd /d/paw/paw-enterprise && bun run check 2>&1 | tail -10`
Expected: clean.

**Step 5: Commit**

```bash
cd /d/paw/paw-enterprise
git add src/lib/stores/chat.svelte.ts src/lib/core/sessions/service.ts
git commit -m "feat(chat/store): resume in-flight run on history load via active_run"
```

---

### Task 13: Manual verify the explorer sidebar surface

**Files:** none

**Step 1: Manual M1 in the explorer sidebar**

Open the explorer sidebar (route that uses `chat/ChatPanel.svelte` — typically `/explore` or wherever `ChatSidebar.svelte` is mounted). Send a long prompt. Refresh.
Expected: chunks already in place + continue.

**Step 2: Manual M2 in explorer sidebar**

Switch sessions mid-stream, switch back.
Expected: resume kicks in.

**Step 3: If anything's off**

The store-method-based resume is a different shape from the os panel's component-local resume. If reactivity bugs appear here, the fix is most likely: don't mutate `this.streamingContent +=` from a callback — instead set it via an explicit reactive setter. Check the regression watchlist in the design doc.

(No commit unless fix needed.)

---

## Phase 5 — Polish + final verification

### Task 14: Update CLAUDE.md env-var docs

**Files:**
- Modify: `D:\paw\backend\CLAUDE.md` (the env-var block)

**Step 1: Edit**

In the "Resumable chat runs config" bullet, ensure the documented default matches `3600` (from Task 1) and add a new line documenting that the run sweeper runs on startup and every 5 minutes. No new env var needed (the 5-min cadence and 10-min threshold are hardcoded per the design — open a follow-up if we want to make them configurable later).

**Step 2: Commit**

```bash
cd /d/paw/backend
git add CLAUDE.md
git commit -m "docs(runs): document sweeper cadence + updated TTL default"
```

---

### Task 15: Full backend test sweep

**Step 1: Run impacted tests + broader sweep**

Run:
```bash
cd /d/paw/backend && uv run pytest tests/cloud/runs tests/cloud/test_agent_router.py tests/cloud/test_agent_router_history_rehydrate.py tests/cloud/test_agent_router_errors.py tests/cloud/sessions -q
```

Expected: all impacted tests pass. The 5 pre-existing failures in `tests/cloud/chat/test_message_emits.py` + `test_search_messages_v2.py` are env-data drift unrelated to this work (per `reference_backend_test_env` memory) — do not touch them.

**Step 2: Ruff + mypy on changed backend files**

Run:
```bash
cd /d/paw/backend && uv run ruff check ee/pocketpaw_ee/cloud/chat/runs/ tests/cloud/runs/ && uv run mypy ee/pocketpaw_ee/cloud/chat/runs/
```

Expected: clean.

(No commit.)

---

### Task 16: Full frontend test sweep

**Step 1: Run all chat-related tests**

Run:
```bash
cd /d/paw/paw-enterprise && bun run test src/lib/core/chat src/lib/components/os 2>&1 | tail -20
```

Expected: all pass.

**Step 2: Type-check the full frontend**

Run: `cd /d/paw/paw-enterprise && bun run check 2>&1 | tail -20`
Expected: no new errors beyond the pre-existing baseline documented in `paw-enterprise/CLAUDE.md` (~43 type errors in 4 buckets).

**Step 3: Lint**

Run: `cd /d/paw/paw-enterprise && bun run lint src/lib/core/chat src/lib/components/os src/lib/stores/chat.svelte.ts 2>&1 | tail -10`
Expected: no new errors.

(No commit.)

---

### Task 17: Manual E2E walk-through (M1–M7)

**Files:** none

For each scenario in §5.4 of the design doc, run the manual test and note the result. If any fail, file a follow-up task; do not paper over with retries.

| # | Scenario | Result |
|---|---|---|
| M1 | Refresh mid-stream | |
| M2 | Session switch mid-stream + return | |
| M3 | Tab close + reopen within 15min | |
| M4 | Tab close + reopen after 1h (TTL boundary) | |
| M5 | Cancel during long tool call | |
| M6 | Offline mid-stream, then session-switch resume | |
| M7 | Force agent error | |

(No commit — manual verification.)

---

### Task 18: Commit design + implementation plan docs

**Files:**
- `D:\paw\backend\docs\plans\2026-05-23-resumable-chat-runs-v2-design.md`
- `D:\paw\backend\docs\plans\2026-05-23-resumable-chat-runs-v2-implementation.md`

**Step 1: Commit both docs**

```bash
cd /d/paw/backend
git add docs/plans/2026-05-23-resumable-chat-runs-v2-design.md docs/plans/2026-05-23-resumable-chat-runs-v2-implementation.md
git commit -m "docs(runs): resumable chat runs v2 design + implementation plan"
```

---

## Done definition

- Backend: TTL bumped, sweeper running, POST /agent/stop tested, all impacted tests green, ruff + mypy clean.
- Frontend: `subscribeRunStreamSSE` shipped + unit-tested, os/ChatPanel resumes on active_run, chatStore resumes on active_run via the explorer sidebar surface, integration tests cover both surfaces, M1–M3 + M5 manually verified passing.
- Two PRs to review: backend `feat/resumable-chat-runs-tier1` → `dev`, frontend `feat/chat-resume-via-repost` → `dev`.

## Out of scope (do not let scope creep)

- Multi-tab simultaneous chunks.
- Group chat resume.
- Cursor-based partial replay.
- Auto-reconnect on SSE network drops (covered by "next history load resumes" pattern).
- Configurable sweeper cadence (hardcoded 5min/10min for now).

## Reference

- Design doc: `D:\paw\backend\docs\plans\2026-05-23-resumable-chat-runs-v2-design.md`
- v1 historical: `D:\paw\backend\docs\plans\2026-05-22-resumable-chat-runs-design.md`
- Abandoned frontend v1 branch (recoverable): `origin/feat/resumable-chat-runs-client`
