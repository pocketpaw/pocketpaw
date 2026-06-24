# jobs — Workspace jobs primitive: named, durable, server-side pocket work

> The workspace jobs primitive runs named, parameterized, server-side async work for a pocket — durably, in the ARQ worker, under a synthetic workspace identity rather than the triggering user's session. A job is dispatched by a `kind:"job"` action on a pocket, runs to completion even after the user's socket closes, and writes its result back into the pocket's rippleSpec `state` so any open canvas updates live. It exists to give pockets a "do this in the background and tell me when it's done" capability without granting an unbounded agent loop.

**Categories:** background work, async execution, ARQ, multi-tenancy, realtime
**Concepts:** pocket job, job registry, JobCallable, WorkspaceJobDoc, kind="job" action, writeback, system:workspace_job identity, xproc bridge, state-only result, PII allowlist, custom jobs, entry-point registration
**Version:** 2

---

## What a pocket job is (and is not)

A **pocket job** is a named, server-side async callable registered in an in-process registry, parameterized by a JSON `params` dict. It is:

- **NOT an HTTP call** — that is a write *action* (`action_executor.run_action`).
- **NOT a belt station run** — that is a separate develop-station surface.
- run under the **workspace service identity** (`system:workspace_job`), never the user's session.
- allowed to call `merge_spec` / connectors / Fabric / bus / audit; **not** allowed to touch another workspace, write code, or run unbounded.

Durability comes from **ARQ**: a job runs in the same worker process as resumable chat runs, so a final `emit(PocketUpdated(...))` reaches the bus even after the user's 30-minute session expires. The worker pins `xproc.set_role("worker")` at boot, so worker-side emits route over the cross-process bridge to the web bus, where the live listeners are wired.

## The contracts

### Trigger action (`rippleSpec.actions`)

```json
{"actions": {"score_applications": {"kind": "job", "job": "score_applications",
  "params": {"batch_size": 20, "connector": "snctm-api"}, "label": "Score Next Batch",
  "requires_instinct": false}}}
```

`POST /pockets/{id}/actions/run` branches on `kind == "job"` **before** the HTTP-write path: registry lookup (`job.unknown` 400 on miss), merge the action's declared params, create a `queued` `WorkspaceJobDoc`, enqueue `execute_workspace_job(job_id)` on the `paw:jobs` queue, emit `WorkspaceJobQueued`, and return `{ok: true, code: "job_enqueued", job_id}`. A missing `kind` falls through unchanged (back-compat). `requires_instinct: true` is rejected with `job.instinct_not_yet_supported` (deferred to v1.1).

### Registry (`jobs/registry.py`)

A module-level dict of `name → JobCallable`, following the `OptimisticCompensationRegistry` shape. `JobCallable` is a Protocol with a `name` attribute and `async __call__(*, workspace_id, pocket_id, job_id, params) -> dict`. Built-ins register at mount time via `register_builtins()`, after `init_realtime()`; workspace-custom jobs register right after that via `load_entrypoint_jobs()` (`jobs/plugins.py`) — see **Custom jobs (entry-point registration)** below.

### Status doc (`models/workspace_job.py`)

A Beanie document recording `workspace`, `pocket_id`, `action`, `job_name`, `params`, `triggered_by` (the viewer id, audit only), `status` (`queued`/`running`/`done`/`failed`), `arq_job_id`, `result`, `error`, and timestamps. Indexed by `(workspace, createdAt)` and `(workspace, pocket_id, createdAt)`.

### Writeback

On success the worker calls `merge_spec(workspace_id, user_id="system:workspace_job", pocket_id, {"merge": result})`, which fires `PocketUpdated`. The `result` is a **partial spec that may write ONLY `state`** — `ui` / `actions` / `sources` / `shape` are template-owned and rejected by `validate_job_result`, which marks the job `failed` (never a silent drop). On any failure (exception, validation reject, or timeout) the worker writes `{state: {"<action>_status": "failed", "<action>_error": "<msg>"}}` so the triggering button never hangs.

### Status poll

`GET /api/v1/workspaces/{ws}/jobs/{job_id}` returns `{job_id, status, timestamps, error}` (the result is already in `state`). Any workspace member may read; the service re-fetches by id and re-checks `workspace_id`, returning 404 on a cross-tenant mismatch.

## Custom jobs (entry-point registration)

Built-in jobs ship inside `pocketpaw_ee`. A **workspace/deploy** ships its own jobs without editing this repo by publishing a small Python package that declares an entry-point in the `pocketpaw.jobs` group — the same SAFE discovery mechanism the OSS core uses for its optional providers (`pocketpaw._registry`). There is **no runtime import of user-supplied code**: discovery reads installed-package metadata only, and `load_entrypoint_jobs()` runs once at process startup right after `register_builtins()` in BOTH the web app (`mount_cloud`) and the ARQ worker (`_startup`), so a custom job resolves identically on either side of the dispatch boundary.

This repo ships **no** `pocketpaw.jobs` entry-point of its own (like the OSS core, it owns the group but registers nothing into it).

### The contract

A `pocketpaw.jobs` entry-point must resolve to a **zero-arg factory** that returns either a single `JobCallable` or an iterable of them. Each resolved object must satisfy the `JobCallable` protocol — a `name: str` attribute plus an `async def __call__(self, *, workspace_id, pocket_id, job_id, params) -> dict`. A provider that fails to load, whose factory raises, or that resolves to a non-`JobCallable` is **skipped with a logged warning** and never blocks startup or other valid providers.

A custom-job package author writes:

```toml
# pyproject.toml of the custom-job package
[project.entry-points."pocketpaw.jobs"]
my_jobs = "my_pkg:make_jobs"
```

```python
# my_pkg/__init__.py
def make_jobs():
    return [MyDailyDigestJob()]   # one JobCallable, or a list of them
```

Once that package is installed in the web + worker environments, the job's `name` is registered on boot and can be dispatched via a `kind:"job"` action (`{"kind":"job","job":"my_daily_digest", ...}`).

### Same security contract applies

A custom job runs under the same boundary as a built-in: writeback is **state-only** (`validate_job_result` rejects `ui`/`actions`/`sources`/`shape`), params are **credential-scrubbed** before the job sees them (`validate_job_params`), it runs under the `system:workspace_job` identity, and connector calls use the workspace's stored creds — never params-supplied tokens. **PII projection is the job author's responsibility**: because a result becomes broadcast `state`, a custom job must project rows through its own allowlist exactly as `score_applications` does (drop `email`/`phone` etc. before returning).

## Security properties

- **Identity** is hardcoded `system:workspace_job`, audited, and not user-assignable. Connector calls use the workspace's stored creds, never params-supplied tokens.
- `validate_job_params` rejects any param key matching `token` / `api_key` / `credential` / `secret` / `password` (case-insensitive) with a 400 — no credential exfil through params.
- `validate_job_result` rejects template-owned writes (`ui`/`actions`/`sources`/`shape`).
- **Tenancy** is enforced on both the worker re-fetch and the status poll.
- **Timeout** is the ARQ per-function `job_timeout` = `POCKETPAW_JOB_TIMEOUT_SECONDS` (default 900s); a timed-out job is treated as a failure with a failed-state writeback.
- **PII**: because the result becomes broadcast `state`, built-in jobs project each row through an allowlist (e.g. `score_applications` drops everything but `id`/`name`/`score`/`stage`, so `email`/`phone` never broadcast).
- Enqueue emits an INFO `AuditEvent`; failure emits a WARNING.

## Module map

| Module | Role |
|--------|------|
| `jobs/domain.py` | Pure constants/value types (identity, queue, timeout, failed-state writeback). |
| `jobs/registry.py` | The named registry + `validate_job_params` / `validate_job_result`. |
| `jobs/plugins.py` | Discovers + registers workspace-custom jobs from `pocketpaw.jobs` entry-points (`load_entrypoint_jobs`). |
| `jobs/service.py` | The only writer of `WorkspaceJobDoc`: dispatch + lifecycle + audit. |
| `jobs/worker.py` | The ARQ `execute_workspace_job` entrypoint (run → validate → writeback). |
| `jobs/router.py` | The status-poll route. |
| `jobs/dto.py` | The `JobStatusResponse` wire model. |
| `jobs/builtin/` | Built-in jobs (`score_applications`) + `register_builtins()`. |

The worker entrypoint is registered into the shared `chat/runs/worker.py` `WorkerSettings.functions` with its own per-function timeout, so it runs in the same worker process as chat runs without sharing their timeout.
