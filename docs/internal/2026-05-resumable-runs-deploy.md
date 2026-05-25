# Tier 2 deploy — resumable chat runs with `arq` worker

This is the operational guide for switching cloud chat runs from the
**Tier 1 in-process** executor to the **Tier 2 arq worker** executor.

Tier 1 still works after this PR ships; the worker is opt-in via env var so
deployments can stage the rollout. Until `POCKETPAW_CLOUD_RUN_EXECUTOR=arq`
is set, the web process runs the agent in-process exactly as before.

Design + plan: `docs/plans/2026-05-22-resumable-chat-runs-design.md` +
`docs/plans/2026-05-22-resumable-chat-runs.md`.

## What changes operationally

| Tier | Where the agent runs | Survives... |
|------|---------------------|-------------|
| 1 (default) | inside the web process (`asyncio.Task`) | refresh / session switch / explicit Stop |
| 2 (`arq`)  | separate worker service | + web-process restart, + worker can scale independently |

Both tiers stream events through Redis (`run:{run_id}:events`), so resume on
reconnect works identically.

## Coolify topology

Add a **second service** to the same Coolify project, pointing at the same
backend image and git ref as the web service.

| Setting | Web service (existing) | Worker service (new) |
|---------|-----------------------|----------------------|
| Image / build | unchanged | same as web |
| Start command | `uv run pocketpaw` (unchanged) | `uv run arq pocketpaw_ee.cloud.chat.runs.worker.WorkerSettings` |
| Replicas | unchanged | start with 1; horizontal-scale by replica count |
| Public port | unchanged | none (worker has no HTTP) |
| Healthcheck | unchanged | none — arq is a pull worker; healthcheck the queue depth in Redis instead |

### Required env on the worker

Copy from the web service:

- `POCKETPAW_REDIS_URL` — **must point at the same Redis** as the web service.
- `CLOUD_MONGODB_URI` — **must point at the same Mongo** as the web service.
- `ANTHROPIC_API_KEY` and any other agent-backend credentials the web service has.
- Every `POCKETPAW_*` setting the agent uses at runtime (model selection,
  feature flags, KB scopes, etc.). When in doubt, mirror the full env.

The worker does **not** need: dashboard/web-only vars (`POCKETPAW_DASHBOARD_*`),
auth secrets unique to the HTTP layer, or any `*_PUBLIC_URL`.

### Flip the web service

On the **web** service add:

```
POCKETPAW_CLOUD_RUN_EXECUTOR=arq
```

Redeploy the web service. POST `/api/v1/cloud/chat/{scope}/{scope_id}/agent`
will now enqueue an `execute_run_job` instead of spawning an `asyncio.Task`.

## Manual end-to-end verification (staging)

Run with worker + web both up and `POCKETPAW_CLOUD_RUN_EXECUTOR=arq` on the web:

1. **Happy path** — send a message in the desktop client. The reply streams
   token-by-token. Proves: web → arq enqueue → worker pickup → worker writes
   to Redis Stream → web GET-stream endpoint → client.
2. **Refresh mid-stream** — send a message, press Ctrl-R while it's streaming.
   The chat reappears after reload and the partial response keeps streaming
   to completion. (Same Tier 1 behaviour, re-verified.)
3. **Session switch mid-stream** — send a message in session A, switch to B,
   then back. A's response is there and still streaming or completed.
4. **Worker restart mid-stream** — send a message; while the worker is
   processing, restart the worker service in Coolify. Expected:
   - the run flips to `interrupted` (boot sweep marks it within ~5s)
   - the partial that already streamed stays visible to the user
   - the SSE subscriber on the web side gets a terminal `interrupted`
     frame and finalises (instead of waiting out the heartbeat)
   - the message renders with the Retry affordance (frontend PR)
5. **Two sessions concurrent** — open two sessions, send in both at once;
   both should stream independently with no interference.

## Rollback

Two-step rollback, web-first:

1. On the **web** service: unset `POCKETPAW_CLOUD_RUN_EXECUTOR` (or set to
   `inprocess`). Redeploy the web service. New runs now execute in-process
   again.
2. Drain & stop the **worker** service. Any in-flight jobs will be marked
   `interrupted` by the in-process heartbeat sweeper (10-minute cutoff) or by
   the next worker boot — whichever happens first. Users see the Retry
   affordance and can resend.

Rollback is independent of any data migration: the `chat_runs` collection
schema is identical between tiers, and the Redis Stream layout is unchanged.

## Crash policy

`WorkerSettings.max_tries = 1` — no auto-retry. A job that raises lands as
`failed` (or `interrupted` if killed mid-execution). LLM token streams cannot
be resumed mid-generation, so silently re-running would double-bill and risk
emitting a partial duplicate. The user decides via Retry.

## Operational notes

- Worker boot sweep uses a **5-second cutoff** (`worker._BOOT_SWEEP_OLDER_THAN_SECONDS`).
  A run that the web enqueued less than 5s before the worker booted is left
  alone for the worker to pick up.
- Heartbeat sweep on the web side still runs every 5 minutes with a 10-minute
  cutoff — that catches in-process orphans during the Tier-1 mode and also
  serves as a safety net under Tier 2 if both worker replicas crash without
  rebooting.
- Multiple worker replicas are safe: arq uses a single Redis-backed queue, so
  each job goes to exactly one worker.

## Files / env reference

- Worker entry: `ee/pocketpaw_ee/cloud/chat/runs/worker.py:WorkerSettings`
- Executor seam: `ee/pocketpaw_ee/cloud/chat/runs/executor.py:get_executor`
- arq executor: `ee/pocketpaw_ee/cloud/chat/runs/arq_executor.py:ArqExecutor`
- Sweep: `ee/pocketpaw_ee/cloud/chat/runs/sweeper.py:sweep_stale_runs`

Env vars (all also documented in `backend/CLAUDE.md` → Key Conventions):

| Var | Default | Purpose |
|-----|---------|---------|
| `POCKETPAW_CLOUD_RUN_EXECUTOR` | `inprocess` | Set to `arq` on the web service to enable Tier 2 |
| `POCKETPAW_REDIS_URL` | — | Required for both tiers; web + worker must share |
| `CLOUD_MONGODB_URI` | `mongodb://localhost:27017/paw-enterprise` | Web + worker must share |
| `POCKETPAW_CLOUD_RUN_STREAM_TTL` | `3600` | Redis Stream retention after a run terminates |
| `POCKETPAW_CLOUD_STREAM_TRANSPORT` | `redis` | Future hook for non-Redis backends |
