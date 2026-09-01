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

## Sizing the worker

`max_jobs` was unset until 2026-09-01, so every deploy before that ran on arq's
default of **10 concurrent jobs per worker** — and that ceiling is shared by all
six registered functions, not divided among them. Ten concurrent site publishes
leave no slot for a chat message. The failure mode is invisible from the outside:
job 11 is not rejected and, with `max_tries = 1`, not retried either. It waits in
Redis behind a `job_timeout` of up to 30 minutes, so the user just sees a reply
that never starts.

Total cluster concurrency is `POCKETPAW_ARQ_MAX_JOBS x worker replicas`. Both
levers work; they cost different things.

**Raise `max_jobs` when the runs are IO-bound** (waiting on the model API). Costs
memory in one container, and nothing else.

**Add replicas when the runs are CPU- or memory-bound** (site builds, `/ship`
deploys). Costs a whole container each, and they are safe to add: arq uses a
single Redis-backed queue, so each job goes to exactly one worker.

Two constraints bound how far `max_jobs` can go:

- **Memory, not CPU, binds first.** The default `claude_agent_sdk` backend spawns
  a Node subprocess per run. Raise the container's memory limit in the same change
  — the Coolify worker ships with a 4G limit, which will not hold 10 concurrent
  SDK runs, let alone more. The `pydantic_ai` backend runs in-process and is much
  cheaper per run if you are concurrency-bound rather than capability-bound.
- **Mongo connections.** Each worker opens its own pool (pymongo's default
  `maxPoolSize` is 100 and nothing overrides it), so replicas multiply
  connections against a single-node Mongo.

**Set `POCKETPAW_CLOUD_WORKER_BOOT_SWEEP=false` before adding the second
replica** — it defaults to false, so this is a "do not turn it on" rather than a
change, but it is the one setting that is actively unsafe multi-replica: the boot
sweep marks other workers' in-flight runs as `interrupted`.

The per-process knobs (`POCKETPAW_AGENT_POOL_MAX_INSTANCES`,
`POCKETPAW_SESSION_WARM_MAX_*`) bound each process independently, so they need to
clear `max_jobs` on the worker — a worker allowed 40 concurrent jobs but only 20
pool instances will queue on the pool instead. `SESSION_WARM_MAX_PER_TENANT` is
the one to keep deliberately low: it is what stops a single busy workspace
holding every warm slot on the box.

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
| `POCKETPAW_ARQ_MAX_JOBS` | `10` | Concurrent jobs ONE worker runs, shared across every registered lane. Set on the **worker**; the web service ignores it. See "Sizing the worker" below |
| `POCKETPAW_AGENT_POOL_MAX_INSTANCES` | `20` | Per-process AgentPool ceiling. Applies to the web process AND each worker |
| `POCKETPAW_SESSION_WARM_MAX_PER_TENANT` | `8` | Per-process warm session slots one workspace may hold. The per-tenant fairness knob |
| `POCKETPAW_SESSION_WARM_MAX_GLOBAL` | `64` | Per-process warm session slots across all workspaces |
| `POCKETPAW_REDIS_URL` | — | Required for both tiers; web + worker must share |
| `CLOUD_MONGODB_URI` | `mongodb://localhost:27017/paw-enterprise` | Web + worker must share |
| `POCKETPAW_CLOUD_RUN_STREAM_TTL` | `3600` | Redis Stream retention after a run terminates |
| `POCKETPAW_CLOUD_STREAM_TRANSPORT` | `redis` | Future hook for non-Redis backends |
| `PAW_SITES_BUILD_TIMEOUT_SEC[_<ENGINE>]` | `600` (floor) | Per-build sandbox budget for the site-build function. Read at worker **import**, so a change takes effect on the worker restart a deploy performs — see below |
| `PAW_SITES_SVELTE_ASYNC_BUILD` | unset (off) | Routes STATIC svelte publishes to the Daytona build lane instead of building them inline. Read per call, so it takes effect without a restart — but set it on **both** the web service and the worker: the web side decides whether to enqueue, the worker side does the build. See the caveat below before turning it on |
| `PAW_SITES_ARTIFACT_STORE` | `filesystem` | Set to `s3` to keep built preview artifacts in blob storage instead of the container's disk, so a view on one replica serves what another built and a redeploy does not empty the cache. Same `(pocket_id, content_hash)` key either way. Read per call. Only meaningful with `POCKETPAW_UPLOAD_ADAPTER=s3` — see below |
| `PAW_SITES_ARTIFACT_S3_TIMEOUT_SEC` | `10` | Per-call deadline on one artifact blob round-trip. The store's seam is synchronous and runs on the request's event loop, so a wedged bucket must not block indefinitely; on a timeout the call degrades to a cache miss |

### Turning the svelte lane on (`PAW_SITES_SVELTE_ASYNC_BUILD`)

Off by default. On, a **static** svelte publish stops blocking the API process on
`bun install` + `bun run build` and goes through the same lane react has used since SL-3.
A **dynamic** svelte site is never routed there whatever the flag says — its
adapter-cloudflare artifact is rendered by a `_worker.js` whose imports sit outside the
tarred directory, so the artifact cannot execute.

**Requires Daytona.** The lane has no local fallback: with `DAYTONA_API_URL` /
`DAYTONA_API_KEY` unset, `run_build` raises and the publish settles
`sandbox_unavailable:run_build_raised`. Turning this on where Daytona is not configured
converts working svelte publishes into failures.

**What you give up, and what you do not.** An inline publish runs `paw-sites`'s
`runWorkerdSmokeRender`, which makes two checks the sandbox has no paw-sites to make:

- The known-workerd-marker scan — **recovered**. The wrapper greps the whole build log
  for the same markers and fails the build on a hit, which is stricter than the inline
  path's own read of a truncated stderr tail.
- The **resting-visibility guard** — **not recovered**. It judges a clean build by reading
  the prerendered `index.html` and the emitted CSS to catch a page that is blank at rest,
  and nothing about an exit code substitutes for it. Until the lane can run that verdict,
  a site published through it is not checked for the blank-at-rest failure.

That gap is why the flag is off by default rather than a straight flip. Its fix is a
`paw-sites-gen` subcommand that judges an already-built tree, called by the worker on the
unpacked artifact.

### Sharing the preview artifact cache (`PAW_SITES_ARTIFACT_STORE`)

Off by default, which keeps built preview artifacts on the container's own disk
(`~/.pocketpaw/site-artifacts/<pocket_id>/<hash>.json`). That is correct for one box and
wrong for several: a view routed to replica B misses what replica A built, a redeploy
empties the cache, and a miss costs a full `bun install` + build.

`PAW_SITES_ARTIFACT_STORE=s3` keeps the same `(pocket_id, content_hash)` key and puts the
artifact in blob storage instead. **Set `POCKETPAW_UPLOAD_ADAPTER=s3` with it** — that is
the knob that decides which backend `pocketpaw.uploads.build_adapter` returns, and with it
left on `local` you get a local-disk adapter writing the same layout as before: no crash,
no sharing. `cloud/uploads/bootstrap.verify_cloud_storage_backend` already warns about
that at boot (and refuses to boot under `POCKETPAW_REQUIRE_S3_IN_CLOUD`).

Nothing here is load-bearing for correctness. A miss, a corrupt object, a timeout, an
unreachable bucket or a failed write all degrade to "rebuild this artifact" — the same
best-effort contract the on-disk store has. A box where no adapter can be built falls back
to the on-disk store rather than failing previews.

Two behaviours differ from the on-disk store, both deliberate:

- **A pocket whose rendered body or CSS carries a per-site capture key is never stored.**
  That secret's exposure was only ever acceptable because it lives in a container that is
  then destroyed (see the headers on `sites/build_job.py` and `sites/daytona_runner.py`);
  a bucket is not destroyed. Such a pocket rebuilds on every view, and the refusal is
  logged per pocket so it is visible rather than mysterious.
- **No eviction.** `PAW_SITES_ARTIFACT_KEEP` prunes the on-disk store because a container
  disk is small and shared. Put a lifecycle rule on the bucket's `site-artifacts/` prefix
  instead of paying a LIST + DELETE on every write.

### The worker runs three functions, each with its own timeout

`WorkerSettings.functions` carries `execute_run_job` (chat runs),
`execute_workspace_job` (workspace jobs, 900s) and `run_site_build` (site builds,
`sites/build_job.site_build_job_timeout_seconds()` — 1020s at the defaults). The three
budgets are deliberately independent: a long build must not be clipped by the chat-run
timeout and vice versa.

The site-build timeout is derived from `PAW_SITES_BUILD_TIMEOUT_SEC*` rather than fixed,
because the in-sandbox `timeout(1)` has to be the thing that fires first. If arq cancels
the job first, the build's result sentinel is never written and the lane records a
healthy-but-slow build as lost infrastructure. So when you lengthen a build budget on the
web service, lengthen it on the worker too — they are the same env vars, and the worker
reads them once at import.
