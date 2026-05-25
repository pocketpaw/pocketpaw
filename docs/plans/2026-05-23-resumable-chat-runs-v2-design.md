# Resumable chat runs v2 — design

**Date:** 2026-05-23
**Status:** Approved (this doc); implementation plan to follow via writing-plans skill.
**Author:** Rohit + Claude
**Supersedes (frontend portion):** the frontend section of `2026-05-22-resumable-chat-runs-design.md` (the v1 frontend client on `feat/resumable-chat-runs-client` is abandoned; backend foundation from v1 is retained).

## Context

The v1 implementation on `feat/resumable-chat-runs-tier1` (backend) + `feat/resumable-chat-runs-client` (frontend) shipped the durability foundation (Runs, Redis Stream, RunExecutor) but never produced a stable user-facing experience. The frontend branch landed 14 commits, several of them debug instrumentation chasing reactivity bugs, and the final UX was: streaming worked in some surfaces but a mid-stream refresh required a second refresh to see the assistant reply, and chunks weren't visible in real time after resume.

Root causes — from post-mortem of the v1 frontend:

1. **Two stores diverged.** The OS chat panel uses `chatRoomsStore` (rooms-keyed). The `/chat` route uses `chatStore` (per-session). A shared `resumeRun()` in `src/lib/core/chat/service.ts` wrote stream events into `chatStore` regardless of which surface initiated resume. The OS panel surface therefore "received streams" via the network tab but never painted them — the chunks went to a store it doesn't subscribe to.
2. **Per-scope run state with cursors.** v1 maintained `runs[scopeKey] = { runId, lastEventId, abortController, ... }` plus `lastEventId` cursors for partial replay. Svelte 5 reactivity around this nested struct mis-fired repeatedly (chained `$derived`, proxy/ref confusion, "must return through the proxy" fixes).
3. **Two transport endpoints.** POST returned JSON `{run_id}` and required a separate GET `/runs/{id}/stream` to subscribe. The frontend had to coordinate "initial POST" vs "resume GET" plus the per-scope-run state above.

This v2 design keeps the backend foundation (it works) and rebuilds the client integration as a thin, per-surface SSE consumer with **zero shared resume state**.

## Goals

- Page refresh mid-stream: streaming continues from where it left off, same tab.
- Session switch mid-stream + switch back: backgrounded run keeps running, switching back resumes.
- Tab close + reopen (same browser, later, within run TTL): resumes.
- Cancel: clean stop endpoint, partial content preserved on cancel.
- One coherent code path across both frontend surfaces (`/chat` route and OS panel).
- No new shared client state. No cursors. No two-store reconciliation.

## Non-goals

- **Multi-tab simultaneous live chunks.** Two tabs of the same session both streaming chunks in real time. WS broadcast of `message.new` at stream_end is sufficient for "other tabs eventually see the message." If we ever want this, the design extends — but it's not in scope now.
- **Group chat resume.** Groups with agents are out of scope. Existing WS `message.new` broadcast handles group-agent reply delivery (at stream_end), which is sufficient.
- **Pure human-human resume.** Human messages in groups already go through WS reliably; nothing to resume.
- **Per-event cursor replay.** Always replay from offset 0. Redis Stream cost is negligible at run scope and eliminates a class of partial/full reconciliation bugs.
- **Cross-device resume.** A run is workspace-authorized; any device in the same workspace can resume by URL. We don't optimize this.

## Architecture

```
                ┌─────────── Frontend ───────────┐
                │                                │
   /chat route ─┼─► chatStore (per-session)     │
                │                                │
   OS panel ────┼─► chatRoomsStore (per-room)   │
                │                                │
                │   Each surface owns:           │
                │   - sendAgentMessage (POST)    │
                │   - resumeAgentRun (GET)       │
                │   - same local SSE handlers    │
                └────────────┬───────────────────┘
                             │
                ┌────────────┴──────────────┐
                │     Backend HTTP API      │
                │                           │
                │ POST /agent  → SSE        │  (initial send)
                │ GET  /runs/{id}/stream    │  (resume — opaque token)
                │ POST /agent/stop          │  (scope-level cancel)
                │ GET  /history             │  (carries active_run.run_id)
                └────────────┬──────────────┘
                             │
                ┌────────────┴──────────────┐
                │    Durability layer       │
                │                           │
                │  RunExecutor   (in-proc)  │
                │       │                   │
                │       ▼                   │
                │  Redis Stream             │ ← truth-of-progress
                │  run:{id}:events          │
                │                           │
                │  ChatRunDoc (Mongo)       │ ← run metadata + status
                └───────────────────────────┘
```

### Three load-bearing invariants

1. **Runs survive the HTTP connection.** The executor writes to the Redis Stream independently of any open SSE response. POST gone? Run keeps going. GET later? Same Stream, full replay.
2. **The client never invents resume state.** `active_run.run_id` arrives in the history response. To resume, the client opens GET with that opaque id. The client doesn't track `lastEventId`, cursors, scope→run maps, or any persistent run state.
3. **Each surface owns its own SSE event handlers and writes to its own store.** No shared `resumeRun()` in `service.ts` that touches a store the caller doesn't own. `service.ts` helpers are pure transport — open SSE, parse frames, call callbacks. Stores are mutated only by surface-local callbacks.

## Components

### Backend (no new modules)

Everything below exists on `feat/resumable-chat-runs-tier1`.

| Module | Responsibility | Touch? |
|---|---|---|
| `runs/transport.py` (`RedisStreamTransport`) | XADD/XREAD on `run:{id}:events`, cancel flag | No |
| `runs/service.py` | `ChatRunDoc` CRUD; `find_active_run_for_scope`; status transitions | No |
| `runs/executor.py` (`InProcessExecutor`) | Submit a `RunSpec` as a tracked asyncio task; agent loop writes to Redis Stream | No |
| `runs/run_core.py` (`execute_run`) | The agent loop: iterate `AgentPool.run`, append events, persist assistant message, mark terminal | No |
| `runs/router.py` (`GET /cloud/chat/runs/{run_id}/stream`) | Tails the Redis Stream, returns SSE; terminal-from-history fallback | No |
| `agent_router.py` (`POST /cloud/chat/{scope}/{id}/agent`) | Create run, persist user message, submit to executor, stream SSE in response body | No (shipped 2026-05-23) |
| `agent_router.py` (`POST /cloud/chat/{scope}/{id}/agent/stop`) | Scope-level cancel via `transport.request_cancel(run.run_id)` | No (shipped 2026-05-23) |
| History endpoint | Returns `active_run: {run_id, status, agent_id, client_message_id}` for non-terminal scope runs | Verify (shipped in `f37f9a97` + `cf246ae0`) |
| Startup sweeper for stale `running` docs | Marks `running` docs older than threshold as `interrupted` on boot | **To add (small)** |

### Frontend (the actual work)

One new service-layer helper alongside the existing `streamAgentSSE`:

| File | Change |
|---|---|
| `src/lib/core/chat/service.ts` | Add `subscribeRunStreamSSE(runId, signal, callbacks)` — opens `GET /cloud/chat/runs/{run_id}/stream?after=0`, parses SSE frames, dispatches into the same `SSECallbacks` interface `streamAgentSSE` already takes. Pure transport — no store imports. |

Two surfaces, identical resume pattern:

| File | Change |
|---|---|
| `src/lib/components/os/ChatPanel.svelte` | (a) Extract `sendAgentMessage`'s inline callbacks into a local `buildAgentCallbacks(roomId, agentMsgId)` helper returning `SSECallbacks`. (b) In `loadAgentSessionHistory`, if response has `active_run`, mount an in-flight bubble placeholder + call `subscribeRunStreamSSE(active_run.run_id, ...)` passing the **same** `buildAgentCallbacks(...)`. |
| `/chat` route host (whichever `+page.svelte` mounts the streaming view) | Same two changes, with `chatStore`-writing callbacks. |

### What we are explicitly NOT building

- ❌ A shared `resumeRun()` in `service.ts`. That's what merged callbacks for two stores in v1.
- ❌ Per-scope run state (`runs[scopeKey] = { runId, lastEventId, ... }`) in either store. One ephemeral `runId` local to the surface mount.
- ❌ `lastEventId` cursors. Always `after=0` on resume.
- ❌ Multi-tab simultaneous chunk push.

## Wire protocol

### POST `/cloud/chat/{scope}/{scope_id}/agent`

Request:
```json
{
  "content": "...",
  "client_message_id": "<uuid>",
  "agent_id": "<optional>",
  "attachments": [...],
  "intent": "pocket_create"   // optional
}
```

Response: `Content-Type: text/event-stream`. First frame:
```
event: message.persisted
data: {"user_message_id": "...", "client_message_id": "...", "run_id": "...", "session_id": "..."}
```

Followed by frames from the run's Redis Stream until terminal (`stream_end` or `error`). Heartbeat `: ping` lines on block timeouts.

### GET `/cloud/chat/runs/{run_id}/stream?after=0`

Response: `Content-Type: text/event-stream`. Stream of events from the named run (entire history then live). Terminal frames as above.

Three terminal scenarios:
- Run actively writing → live tail.
- Run terminal AND stream present → replay events from Stream until terminal.
- Run terminal AND stream TTL'd → synthesize one `stream_end {from_history: true, assistant_message_id, cancelled}` from `ChatRunDoc`.

### POST `/cloud/chat/{scope}/{scope_id}/agent/stop`

Idempotent. Find the active run for `(workspace, scope, scope_id)`. If found, `transport.request_cancel(run_id)`. Return `{status: "ok"}` either way.

### History response — `active_run` field

When a non-terminal run exists for the requested scope:
```json
{
  "messages": [...],
  "active_run": {
    "run_id": "...",
    "status": "queued" | "running",
    "agent_id": "...",
    "client_message_id": "...",
    "user_message_id": "..."
  }
}
```

When the most recent run is terminal but `interrupted`/`failed`:
```json
{
  "messages": [...],
  "active_run": {
    "run_id": "...",
    "status": "interrupted" | "failed",
    "partial_text": "...",
    "error": "..."   // when status=failed
  }
}
```

Client uses `status` to decide: subscribe (queued/running), retry affordance (interrupted/failed), no-op (no field at all).

## Data flow

### Live send

```
Surface         POST /agent                 Backend                       Redis Stream
  │ optimistic user message added (tagged with client_message_id)
  │                  ├────────────────────────►│
  │                  │  create_run + persist user_message
  │                  │  submit(spec) ─────────►│ executor task ──► XADD events
  │                  │                                                         │
  │ ◄────────────────┤  message.persisted, stream_start, chunk×N, stream_end   │
  │ surface callbacks mutate own store
```

### Refresh mid-stream

```
[F5] → surface mounts → GET /history
                          ◄── messages[], active_run: { run_id }
  │ render in-flight placeholder
  │ subscribeRunStreamSSE(run_id, signal, buildAgentCallbacks(...))
  │     ◄── stream_start, chunk×N (replay), chunk×M (live), stream_end
  │ same store writes as live path
```

### Session switch mid-stream + back

A's SSE fetch aborted locally on switch. Run keeps going server-side. Switching back loads A's history, sees `active_run`, opens GET with the same `run_id`, replays from offset 0.

### Tab close + reopen

Identical to refresh. Works until TTL (`POCKETPAW_CLOUD_RUN_STREAM_TTL`, default 900s) expires. After TTL: history shows either the persisted assistant message (if run completed before TTL) or `active_run.status = "interrupted"` (if sweeper marked it). Graceful degrade either way.

### Cancel

`POST /agent/stop` → backend writes cancel flag → next loop iteration in executor breaks → emits `stream_end {cancelled: true}`. Local SSE abort runs in parallel for instant UI response.

### Stream closed before subscriber attached

Backend `GET /runs/{id}/stream` already handles this: when run is terminal AND stream is gone, synthesize one `stream_end {from_history: true}` frame.

## Error handling

| Failure | Treatment |
|---|---|
| Network blip on SSE (POST or GET) | Run keeps running. No client-side auto-reconnect. Next history reload resumes. |
| Backend process restart mid-run | Run task dies. Startup sweeper marks stale `running` docs as `interrupted`. Client sees `active_run.status = "interrupted"` → renders retry affordance. |
| Agent error inside run loop | `execute_run` catches, appends `error` event, marks doc `failed`, sets TTL. Client SSE receives `error` frame. |
| Mongo down at user-message persist | POST fails 5xx, no run created. Standard error toast. |
| Mongo down at assistant-message persist | Stream emits `error`. Run doc marked `failed`. |
| Redis down at run start | First XADD fails → `execute_run` exception path → `mark_terminal(failed)`. Subscribers either skip (status=failed) or get synthetic `stream_end {from_history: true}`. |
| Redis down mid-run | XREAD errors in `GET /runs/{id}/stream` → SSE response terminates. Client falls back to history-reload pattern. |
| Cross-workspace / nonexistent run on GET | `_authorize` raises `NotFound` → 404. Client treats as "no resume." No info leak. |
| Double cancel | `request_cancel` is a SET — idempotent. |
| Stop after stream_end | `find_active_run_for_scope` returns nothing. Endpoint returns `{status: ok}`. |
| Stop, then new POST while old still running | New POST cancels prior via the same `find_active_run_for_scope` + `request_cancel`. Prior emits clean `stream_end {cancelled: true}`. |
| SSE frame JSON parse error | Log + skip frame. Stream continues. |
| Surface unmount mid-stream | AbortController on fetch. Run continues server-side. |
| Resume opens but run already terminal | One `stream_end {from_history: true}` from backend, finalize as usual. |
| Double resume call (re-mount race) | Surface tracks local `resumingRunId`. If equal to incoming, no-op. |

## Testing

### Backend

| Test | Status |
|---|---|
| POST /agent streams SSE with `message.persisted` first | ✅ shipped |
| POST /agent idempotent on `client_message_id` | ✅ shipped |
| POST /agent rehydrates history into `RunSpec` | ✅ shipped |
| POST /agent pre-stream errors return 4xx | ✅ shipped |
| GET /runs/{id}/stream full replay from offset 0 | verify existing |
| GET /runs/{id}/stream terminal-from-history fallback | verify existing |
| GET /runs/{id}/stream 404 on cross-workspace + nonexistent | verify existing |
| POST /agent/stop idempotent + no-op on inactive | **to add** |
| Sweeper marks stale `running` → `interrupted` | **to add** |
| Executor crash → `error` event + `failed` doc | verify existing |
| History endpoints include `active_run` | ✅ shipped |

### Frontend unit (Vitest)

| Test |
|---|
| `subscribeRunStreamSSE` parses SSE frames into callbacks |
| `subscribeRunStreamSSE` calls `onFetchError` on network failure |
| `subscribeRunStreamSSE` calls `onAbort` when signal aborts |
| `subscribeRunStreamSSE` skips unparseable frames without killing stream |
| OS panel `buildAgentCallbacks` writes into `chatRoomsStore` (mocked) |
| OS panel `buildAgentCallbacks` is idempotent on duplicate `stream_end` |

**Anti-pattern lint:** no test should assert behavior across both stores from a single code path. If a test would need to, the design is wrong.

### Frontend integration (component / DOM)

| Test |
|---|
| Mock `getSessionHistory` to return `active_run`; assert `subscribeRunStreamSSE` called with right `run_id` (both surfaces) |
| Surface unmount aborts in-flight fetch (both surfaces) |
| Double-subscribe to same `run_id` is a no-op |
| Cancel button POSTs `/agent/stop` with correct scope/scope_id |

### Manual / E2E

| # | Scenario | Pass criterion |
|---|---|---|
| M1 | Long prompt → refresh mid-stream | Partial chunks already on screen, more fill in live, completes |
| M2 | Switch session mid-stream, come back ~10s later | Resumes seamlessly |
| M3 | Close tab, reopen within 15min | Resumes |
| M4 | Close tab, reopen after 15min | Persisted message OR retry affordance |
| M5 | Cancel during long tool call | `cancelled: true`, partial preserved |
| M6 | Offline mid-stream, back online, no auto-reconnect | Spinner stays; session switch + return resumes |
| M7 | Force agent error | Error renders cleanly; subsequent send works |

## Regression watchlist

The bugs that killed v1. If any of these appear in PRs going forward → stop.

1. Any `import` of a store inside `src/lib/core/chat/service.ts`. Service is pure transport.
2. Any function in `service.ts` named `resumeRun` or similar that mutates state instead of parsing frames.
3. Any per-scope `runs` map in either store.
4. Any reference to `lastEventId` or cursor offsets in client code.

Encode as code-review checklist items.

## Rollout

1. Land backend on `feat/resumable-chat-runs-tier1` (already there, plus the sweeper + a few tests).
2. Cut frontend work on `feat/chat-resume-via-repost` (branch already created off `dev`).
3. Frontend ships as one PR. Each surface's resume function is tested in isolation against mocked transport callbacks. The previous PR-per-commit pattern is explicitly not repeated.
4. Manual verify M1–M7 before merge.
5. Merge backend first, then frontend. They're independent: backend with no frontend changes still works for the live-send path; frontend resume code is dormant until backend ships `active_run` (already shipped).

## Resolved decisions

- **Default run stream TTL** bumped from 900s → 3600s (`POCKETPAW_CLOUD_RUN_STREAM_TTL`). One hour of "close tab, come back" works. Storage cost in Redis is small (per-run event log, evicted in 1h).
- **Sweeper schedule.** Runs on startup AND every 5 minutes via a background task. Each sweep marks `running` docs older than 10 minutes as `interrupted`. Cheap indexed query; prevents orphans from accumulating between restarts.

## References

- v1 design (historical): `2026-05-22-resumable-chat-runs-design.md`
- v1 backend implementation plan: `2026-05-22-resumable-chat-runs.md`
- Backend code: `ee/pocketpaw_ee/cloud/chat/runs/`
- Frontend transport: `src/lib/core/chat/service.ts`
- Frontend surfaces: `src/lib/components/os/ChatPanel.svelte`, `/chat` route
