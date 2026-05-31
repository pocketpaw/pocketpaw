# Resumable Chat Runs — Design

**Date:** 2026-05-22
**Status:** Approved design — ready for implementation planning
**Scope:** `backend/ee/pocketpaw_ee/cloud/` (PocketPaw cloud chat) + `paw-enterprise/` (desktop client)

## Problem

In the cloud agent chat, an assistant turn is **owned by the HTTP request that started it**:

- `POST /cloud/chat/{scope}/{scope_id}/agent` returns a `StreamingResponse`; the agent
  loop only advances while that SSE generator is being pulled. A page refresh or tab
  close drops the connection, cancels the generator, and **kills the agent run** —
  the work is thrown away, not finished in the background.
- The assistant message is persisted only at `stream_end`, and *not at all* if the
  run was cancelled (`agent_router.py:688` — `if cancelled or not full_text.strip(): return`).
  So a refresh mid-stream loses the response **permanently**.
- The frontend keeps the in-progress message in volatile `$state`
  (`paw-enterprise/src/lib/stores/chat.svelte.ts`). Refresh wipes it; switching
  sessions fires `AbortController.abort()` and kills the stream.

**Result:** refresh or session-switch mid-stream loses the whole assistant turn and
stops the agent. The user must wait for completion before doing anything.

There is no Redis, no message queue today — everything is in-process. Redis is
already a declared dependency (`redis[hiredis]>=5.0.0` in `backend/ee/pyproject.toml`)
and a `RedisBus` is anticipated in `_core/realtime/bus.py`.

## Goal

An agent turn survives page refresh, tab close, and session switching. Two tiers:

- **Tier 1** — the run executes detached from the HTTP request, in the web process;
  events flow through a durable Redis Stream so any reconnecting client resumes.
- **Tier 2** — the run executes in a separate `arq` worker service, so it also
  survives web-process restarts and scales independently.

## Key insight

Redis **Pub/Sub alone does not fix this** — a client that reconnects after a refresh
has missed every message sent while it was gone. The fix needs two parts:

1. **Decouple the run from the request** — run it in a background task / worker.
2. **Make the stream resumable** — push events to a **Redis Stream** (an ordered,
   persistent log with offsets), not Pub/Sub. Reconnecting clients replay from their
   last offset, then continue live.

## Decisions

| Question | Decision |
|---|---|
| Deployment | Single backend container today. arq worker = a separate Coolify service sharing the image. Redis = a new Coolify service. |
| Abandoned runs (tab closed, never returns) | **Always run to completion.** Response saved to history; no "no-listeners" reaper. |
| Worker crash / deploy mid-run | **Mark `interrupted`; user retries manually.** No auto-retry — avoids double token charges and duplicated output. |
| Approach | **`RunExecutor` seam**, ship `InProcessExecutor` (Tier 1) first, add `ArqExecutor` (Tier 2) as a follow-up behind a config flag. |
| Transport adapter | **`RunStreamTransport` Protocol** with one concrete `RedisStreamTransport`. Dragonfly/Valkey are drop-in via config (Redis wire protocol). Non-Redis backends (NATS JetStream, etc.) implement the Protocol. |

## Architecture

An agent turn becomes a **Run** — a first-class, addressable object that outlives any
HTTP connection. The web process never runs the agent; it writes runs to an executor
and reads their event streams.

```
  POST /chat/.../agent          GET /chat/runs/{id}/stream
        | persist user msg            | XREAD the Redis Stream
        | create Run doc              | replay buffered + live events
        | executor.submit(spec)       | -> SSE to browser
        v                             ^
   +-------------+   XADD events   +--+--------------+
   | RunExecutor | --------------> |  Redis Stream   |
   +------+------+                 | run:{id}:events |
          |                        +-----------------+
   +------+---------------+
   | InProcessExecutor    |  Tier 1 - asyncio.create_task in web process
   | ArqExecutor          |  Tier 2 - enqueue arq job -> worker service runs it
   +----------------------+
```

### Two protocols, two axes

The agent run sits behind **two protocols**, each abstracting a different concern.
Together they cover the realistic swaps without over-DI'ing.

- **`RunExecutor`** — *where* the agent runs (web process vs worker service).
  Tier 1 vs Tier 2.
- **`RunStreamTransport`** — *how* events flow from the executor to readers
  (Redis Streams today; could be NATS JetStream / Kafka / etc. tomorrow).

Both are plain `typing.Protocol` interfaces selected from env vars at startup. The
agent loop (`execute_run`) depends only on the protocols; the wiring layer picks
the concrete class.

### `RunExecutor` seam

A `RunExecutor` protocol with one method, `submit(spec: RunSpec)`. Two
implementations, selected by `POCKETPAW_CLOUD_RUN_EXECUTOR`:

- **`InProcessExecutor`** — `asyncio.create_task`; tasks tracked on `app.state` so
  shutdown can mark them interrupted. Tier 1. Needs only Redis. Also the dev/fallback
  path and the OSS-friendly option.
- **`ArqExecutor`** — `arq_pool.enqueue_job("execute_run", spec)`; a separate worker
  service picks it up. Tier 2.

Both call the **same** `execute_run(spec)` coroutine — the agent loop currently
inlined in `agent_router.py` (~lines 518-688) moves there verbatim. The only
difference between tiers is *where that coroutine runs*.

`RunSpec` is a plain JSON-serializable value object (so arq can pickle it):
workspace/scope/user/agent ids, `run_id`, the user message, and loaded history.

### Code placement

A new `runs/` entity under `ee/pocketpaw_ee/cloud/chat/`, following the cloud 4-file
rule:

- `runs/domain.py` — `RunSpec`, `RunStatus`, `StreamEvent` value objects
- `runs/dto.py` — request/response DTOs
- `runs/service.py` — `chat_runs` doc CRUD (only file importing the Beanie doc),
  `execute_run`, the interrupted-run sweep
- `runs/router.py` — `GET /runs/{run_id}/stream`, `POST /runs/{run_id}/stop`
- `runs/transport.py` — `RunStreamTransport` Protocol + `get_stream_transport()`
  factory
- `runs/redis_stream.py` — `RedisStreamTransport` concrete impl (XADD/XREAD)
- `runs/executor.py` — `RunExecutor` protocol + `InProcessExecutor` + `ArqExecutor`
- `runs/worker.py` — arq `WorkerSettings`

A Redis client singleton (e.g. `_core/redis.py`) initialized in `mount_cloud()`.

## Data model & transport

### Run document — MongoDB collection `chat_runs`

Only `runs/service.py` touches the Beanie doc (cloud rule 2).

| Field | Purpose |
|---|---|
| `run_id` (uuid) | Stable handle; used in URLs and the Redis key |
| `workspace_id`, `context_type`, `scope_id`, `session_key`/`group` | Locate by scope; tenant filter on every read |
| `user_id`, `agent_id` | Who triggered it / which agent |
| `client_message_id` | Idempotency — dedupes a re-submitted message |
| `user_message_id` | The already-persisted user message |
| `assistant_message_id` | Set when the assistant message is persisted |
| `status` | `queued -> running -> completed` \| `interrupted` \| `failed` \| `cancelled` |
| `partial_text` | Snapshot of streamed text; written on terminal-but-incomplete states so the partial survives the Redis TTL |
| `error` | Failure detail |
| `created_at`, `started_at`, `ended_at` | Lifecycle timestamps |

### Event transport — `RunStreamTransport`

The transport is abstracted behind a small Protocol:

```python
class RunStreamTransport(Protocol):
    async def append_event(self, run_id: str, event: str, data: dict) -> str: ...
    def read_events(self, run_id: str, *, after: str, block_ms: int) -> AsyncIterator[StreamEvent]: ...
    async def set_ttl(self, run_id: str, ttl_seconds: int) -> None: ...
    async def request_cancel(self, run_id: str) -> None: ...
    async def is_cancelled(self, run_id: str) -> bool: ...
    async def stream_exists(self, run_id: str) -> bool: ...
```

`execute_run`, the stream router, and the worker all take a `RunStreamTransport`
via the `get_stream_transport()` factory — no module-level Redis calls anywhere
outside the concrete impl.

**Default impl — `RedisStreamTransport`** — backed by Redis Streams. One stream per
run, key `run:{run_id}:events`. Each `XADD` is one SSE event with fields `event`
(type) and `data` (JSON). Redis assigns each entry a monotonic id (`<ms>-<seq>`),
which becomes the SSE `id:` field, so a reconnecting client requests "everything
after id X."

- A reader does `XREAD BLOCK <ms> STREAMS run:{id}:events <cursor>` — replays
  buffered events, then blocks for live ones. Many readers (tabs, resumed sessions)
  read the same stream independently.
- **TTL:** after the terminal event, `EXPIRE run:{id}:events 900`
  (`POCKETPAW_CLOUD_RUN_STREAM_TTL`, default 900s). After expiry, the run doc +
  persisted message in Mongo are the durable record.

**Swapping backends:**

- **Dragonfly / Valkey** — Redis wire protocol. No code change; point
  `POCKETPAW_REDIS_URL` at the new server.
- **Non-Redis (NATS JetStream / Kafka / etc.)** — write a new
  `JetStreamTransport(...)` class implementing `RunStreamTransport`, register it
  in `get_stream_transport()` behind `POCKETPAW_CLOUD_STREAM_TRANSPORT=jetstream`.
  Nothing else changes — `execute_run` and the router are transport-agnostic.

### Cancellation / control

The in-process `asyncio.Event` registry (`agent_router.py:49-51`) cannot reach an
arq worker in another process. Replaced by a Redis key `run:{run_id}:cancel` —
`execute_run` checks it once per agent-event iteration; `POST /stop` sets it. One
mechanism, identical for both executors.

### Finding the live run on refresh

No separate registry. The history/session-load response gains an `active_run`
field: a scope-filtered query for the newest `chat_runs` doc with non-terminal
status returns `{run_id, status}`. The frontend uses it to auto-resume.

### Persistence timing

- User message: persisted up front (unchanged).
- Assistant message: persisted by `execute_run` on `completed`, **and** on
  `interrupted`/`failed` when `partial_text` is non-empty — the partial is marked
  `interrupted: true` in metadata so the UI can show a Retry affordance. (Today a
  cancelled run persists nothing — that is the data-loss bug.)

## Request / stream flow & API surface

### POST becomes a fast JSON endpoint

`POST /cloud/chat/{scope}/{scope_id}/agent` no longer streams:

```
1. persist user message (as today)
2. cancel any active run for this scope (set its cancel key)
3. load history -> build RunSpec
4. create chat_runs doc (status=queued)
5. executor.submit(spec)
6. return { run_id, user_message_id }   <- JSON, immediate
```

### New streaming endpoint

`GET /cloud/chat/runs/{run_id}/stream?after=<entry_id>` — first view and resume use
the same path:

```
1. load run doc; 404 if missing; authz = scope member
2. cursor = after ?? "0"   (0 = replay whole stream)
3. loop: XREAD BLOCK 15s STREAMS run:{id}:events <cursor>
       -> emit "id: <entry_id>\nevent: <e>\ndata: <json>\n\n"
       -> advance cursor; send ": ping" heartbeat on idle
4. stop on terminal event or client disconnect
5. if stream key already expired -> fall back to the run doc:
   emit one synthetic terminal event from Mongo (final/partial text)
```

### Stop endpoint

`POST /cloud/chat/runs/{run_id}/stop` — sets the `run:{run_id}:cancel` key. Replaces
the old `/agent/stop`.

### End-to-end lifecycle

| Moment | What happens |
|---|---|
| Send message | POST returns `{run_id}` in ~ms; client opens the stream GET |
| Streaming | `execute_run` (in-process or worker) `XADD`s each event; readers see them live |
| Refresh mid-stream | Reload -> history load returns `active_run` -> client reopens `GET .../stream?after=0` -> full partial replays, then live |
| Switch session mid-stream | Client closes the stream fetch; the run keeps running. Switch back -> reopen stream, replay + resume |
| Run completes | `execute_run` persists assistant message, sets run `completed`, `XADD stream_end`, sets stream TTL |
| Tab closed forever | Run still completes; next open it is plain history |

Covers all four scopes (`dm`/`group`/`pocket`/`session`) — they already share the
one agent endpoint.

## Frontend changes (`paw-enterprise/`)

The frontend keeps streaming state **global** — a single `streamingContent` /
`messages` on `chatStore` (`src/lib/stores/chat.svelte.ts`). That is why switching
sessions nukes the stream.

1. **Per-scope streaming state** — replace the single `streamingContent` with a map
   keyed by scope/session id: `runs: Record<scopeKey, { runId, status, text,
   lastEventId }>`. The group-chat store (`core/chat/store.svelte.ts`) already keys
   messages by `groupId` — copy that precedent. The visible view derives from
   `runs[activeScopeKey]`.
2. **Send flow** (`service.ts` `streamAgentSSE`) — POST returns `{run_id}`; then open
   the stream reader against `GET /cloud/chat/runs/{run_id}/stream?after=<lastEventId
   ?? 0>`. The existing fetch + `ReadableStream` SSE parser is reused — it points at
   the GET and records each frame's `id:` into `lastEventId`.
3. **Session switch** — drop the `AbortController.abort()` that kills the run.
   Switching away just closes the stream reader; the run keeps going server-side.
   No `/stop` call. `/stop` is sent only on an explicit Stop click.
4. **Resume on mount** — after history loads in `switchSession`, if the response
   carries `active_run`, immediately open the stream reader at `?after=0`; the Redis
   Stream replays the whole partial, then continues live. A cold refresh has no
   `lastEventId`, so `after=0` is the default — the cursor never needs to be
   persisted to disk.
5. **Interrupted runs** — when history contains an assistant message with
   `interrupted: true` metadata, render it with a **Retry** button that re-sends the
   originating user message.
6. **Concurrent sessions** — per-scope state lets two sessions stream at once;
   switching swaps which `runs[key]` the view reads.

## Error handling & edge cases

| Case | Handling |
|---|---|
| Worker crash / deploy mid-run | On worker startup, sweep `chat_runs` with status `running`/`queued` -> mark `interrupted`, persist `partial_text` as an assistant message (`interrupted: true`), `XADD` a terminal `interrupted` event if the stream is alive. arq job retry = 0. |
| Web process restart (in-process executor) | Same sweep on web startup. In-process runs die and are marked `interrupted` — the gap Tier 2 closes. |
| Stream key expired before reconnect | Reader falls back to the run doc — emits one synthetic terminal event from Mongo's final/partial text. |
| Redis unavailable | POST fails fast with a `CloudError` (503-class); no silent half-states. |
| Duplicate submit (same `client_message_id`) | POST returns the existing run's `run_id` instead of creating a second run. |
| New message while a run is active in that scope | Old run's cancel key is set, then the new run starts — matches today's behaviour. |
| Multiple tabs / readers | Each opens its own `XREAD`; Redis Streams fan out to all. |

## Testing

- **Unit** — `RunExecutor` implementations against a fake agent iterator; assert the
  `XADD` event sequence. Reader endpoint: seed a stream, assert SSE frames and correct
  `after=<id>` resume. Use `fakeredis` so unit tests need no infra.
- **Integration** — full POST -> stream -> mid-stream reconnect -> resume; cancel via
  `/stop`; the interrupted-run sweep. Run against a real Redis (CI service container)
  alongside the MongoDB the cloud tests already require.
- **Frontend** — session-switch keeps the run alive; refresh resumes from `after=0`;
  interrupted message renders Retry.

## Config

| Env var | Purpose | Default |
|---|---|---|
| `POCKETPAW_REDIS_URL` | Redis connection | — |
| `POCKETPAW_CLOUD_RUN_EXECUTOR` | `inprocess` (Tier 1) or `arq` (Tier 2) | `inprocess` |
| `POCKETPAW_CLOUD_STREAM_TRANSPORT` | Concrete `RunStreamTransport` to use (`redis` today; `jetstream`/etc. later) | `redis` |
| `POCKETPAW_CLOUD_RUN_STREAM_TTL` | Run event-stream TTL after completion (seconds) | `900` |

## Rollout — two PRs

1. **Tier 1** — `runs/` entity, Redis Streams, `InProcessExecutor`, the GET stream
   endpoint, slimmed POST, frontend per-scope state + resume. Deploy the Redis
   service; executor flag = `inprocess`. **Refresh and session-switch are fixed here.**
2. **Tier 2** — `ArqExecutor` + `worker.py` + the startup sweep; deploy the worker as
   a Coolify service; flip the flag to `arq`. No call-site changes.

## Out of scope

- Live-tier streaming for non-caller scope members beyond today's WebSocket
  `message.new` broadcast (the resumable stream is per-run; group fan-out of token
  chunks to other members is a later enhancement).
- An in-process fallback that streams directly when Redis is down.
- A "no-listeners" reaper (explicitly rejected — runs always complete).
