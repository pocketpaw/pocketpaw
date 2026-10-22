<!-- Runbook: Daytona org resource limits, the sites memory budget builds are admitted
     against, how site builds behave when the org is full, and how to free capacity
     from the Daytona dashboard. -->

# Runbook — Daytona org capacity and sandbox cleanup

## The limit

Daytona caps an organization's **total** CPU, memory and disk across every sandbox
that is running (stopped sandboxes free CPU and memory; archived and deleted ones
also free disk). Our org's memory cap is **10 GiB**. When a create would cross it,
the SDK raises:

```
DaytonaValidationError: Failed to create sandbox: Total memory limit exceeded. Maximum allowed: 10GiB.
```

What each of our sandboxes asks for, and how long it lives:

| Caller | cpu / mem / disk | auto-stop | auto-archive | auto-delete | Torn down |
|---|---|---|---|---|---|
| Site publish / preview build (`sites/daytona_runner.py`) | 2 / 4 GiB / 10 GiB | build budget + margin | SDK default | 0 (on stop) | explicit delete when the build ends; by name if create failed; reaper |
| html browser verify (`sites/browser_check.py`) | 1 / 2 GiB / 5 GiB | verify budget + 10 min | SDK default | 0 (on stop) | explicit delete when the check ends; by name if create failed; reaper |
| Code Mode sandbox (`cloud/websandbox/provision.py`) | per request | 5 min | 5 min | 0 (on stop) | reaper + Daytona; project lives in S3 |
| Workspace VM (`cloud/daytona/router.py`, agent `pocketpaw_daytona` tools) | 2 / 4 GiB / 10 GiB | 30 min (config, clamped 5 min to 1 day) | 1 day | never | persistent; resumed on next use |

So two builds plus one idle workspace VM already fill 10 GiB. Before this change the
workspace VM stopped after 3600 **minutes** (60 hours; the config value was seconds
but reached Daytona unconverted), which is how idle agent boxes starved builds.

## How build and verify sandboxes are cleaned up

Build (`paw-build-<ms>`) and verify (`paw-verify-<ms>`) sandboxes are torn down in
three layers, so a box cannot hold quota after the job that made it is gone:

1. **Explicit delete.** When the build or check ends, the runner deletes the box by id.
   If the SDK create raised or the job was cancelled before create returned an id (the
   SDK registers the box, then waits up to 60 s for `started`), the runner looks the box
   up by its unique name and deletes it before re-raising. That cleanup is shielded, so
   a second cancel does not abandon it.
2. **Re-run cleanup.** Every box carries Daytona labels: `paw_lane` (`site-build` or
   `site-verify`), and from the build jobs `paw_pocket` and `paw_job` (the arq job id).
   Before a create, a job deletes any box already labelled with its own `paw_job`: that
   is a box an earlier attempt left when its worker died (for example on a redeploy).
3. **Reaper cron.** Every 10 minutes the site-build worker runs
   `sites/sandbox_reaper.py`, which deletes `paw-build-*` and `paw-verify-*` boxes older
   than the longest sites job budget plus 15 minutes, in any state, including one stuck
   in `creating` that auto-stop never touches. It never touches `paw-ws-*` or other
   names, and does nothing when Daytona is not configured. Worker log:
   `sandbox_reaper: deleted orphan` and `reaped N orphaned build sandbox(es)`.

Daytona's own auto-stop plus `auto_delete_interval=0` stays as the last backstop.

## The sites memory budget (admission)

The sites lane runs `POCKETPAW_SITES_ARQ_MAX_JOBS` (default 4) jobs per worker, times
the worker replicas, and each build asks for 4 GiB. That is 16 GiB of demand per
worker against a 10 GiB org. When admission is on, each build and html verify first
takes a **memory lease** (`sites/admission.py`) for the memory it is about to request,
and holds it until its sandbox is deleted.

- **Off by default.** With `PAW_SITES_DAYTONA_MEMORY_BUDGET_GIB` unset or empty (or not
  a positive integer, which also logs a warning) the lease is a pass-through: no Redis
  call, no wait, builds go straight to Daytona as before. Turn it on once the logs
  (below) show memory is the limit being hit.
- **Budget:** `PAW_SITES_DAYTONA_MEMORY_BUDGET_GIB=<GiB>`. A lease waits until its GiB
  fits under the budget. Waiters are served in arrival order. A request bigger than the
  whole budget runs only when nothing else holds a lease.
- **Shared across replicas.** The site-build worker points the ledger at its Redis on
  startup (`{paw-sites-admission}:*` keys, one hash tag so it also works on Redis
  Cluster), so every worker and replica draws from one budget. A lease expires on its
  own after the reaper's orphan age (longest sites job budget plus 15 minutes), so a
  crashed worker's lease frees when the reaper deletes its box. Without Redis, or if
  Redis errors, the budget is per process.
- **Bounded wait.** A lease waits at most the capacity retry window (270 s) in total,
  including any time spent on Redis before it failed over. After that the build goes
  to Daytona anyway and gets the normal capacity handling below, so nothing waits
  forever. The arq job timeout always includes this wait (on or off, so the
  registered timeout and the code that reads it never disagree), so a wait never eats
  the build's own budget.
- **While it waits** a preview poll reads `queued` / `waiting_for_capacity`. Worker
  log: `admission: ... waiting for the budget`, and `going to Daytona unleased` when
  the wait ran out.

### Which limit is actually hit

Every failed sandbox create logs one WARNING line from `pocketpaw_ee.sites.admission`:

```
admission: sandbox create failed (paw-build-...; limit=memory; asked cpu=2 memory=4GiB disk=10GiB; pocket=... job=...): DaytonaValidationError: Failed to create sandbox: Total memory limit exceeded. Maximum allowed: 10GiB.
```

`limit=` is `cpu`, `memory`, `disk`, `gpu` or `sandbox_count` for a capacity refusal,
and `not a capacity refusal` for anything else; the rest is Daytona's own message with
keys and tokens redacted. Disk counts stopped sandboxes too (only archived and deleted
ones free it), so a `limit=disk` line with free memory on the dashboard points at old
stopped boxes, not at concurrency. Grep for `admission: sandbox` before choosing a
budget.

### How to size it

Set the budget to **your plan's org memory cap**. That stops the lane from
overcommitting Daytona without throttling builds when there is room.

Lower it only to reserve headroom for things the budget does not count, when you would
rather have builds wait in our queue than hit Daytona refusals:

```
budget = org memory cap
       - 4 GiB x workspace VMs you want to always have room for (paw-ws-*)
       - Code Mode sandboxes you want room for (websandbox-*, about 1 GiB each)
```

With the budget at the cap, a workspace VM or Code Mode box that is running can still
cause a refusal. That refusal goes to the capacity retries below, and (if enabled) the
idle-VM eviction. On a bigger Daytona plan, set the new cap in the worker's environment
and redeploy, for example `PAW_SITES_DAYTONA_MEMORY_BUDGET_GIB=50` for a 50 GiB org.
Raising `POCKETPAW_SITES_ARQ_MAX_JOBS` without raising the budget only adds jobs that
wait.

### Eviction of idle workspace VMs (opt-in)

Off by default. Set `PAW_SITES_EVICT_IDLE_WORKSPACE_VMS=1` on the worker to turn it
on. It is opt-in because Daytona's `last_activity_at` may not move during a long
command or a background process the agent started, so an "idle" VM can still be running
a user's dev server or test run.

When it is on and Daytona refuses a create for capacity, the build stops idle
workspace VMs once and retries the create once:

- only `paw-ws-*` VMs in `started`, idle for at least **30 minutes** (the VM's own
  auto-stop, so we never stop one sooner than Daytona would);
- idle means the later of Daytona's `last_activity_at` and the last time this worker
  process resolved the VM for a tool call (`context.last_resolved_at`). That second
  signal only sees this process; there is no shared last-tool-call record, and the
  eviction does not check for an active chat run;
- least recently active first, and only until the build's memory is freed, counted
  from each VM's reported memory (a VM whose size is unknown is skipped);
- **stop, never delete**: the disk stays, and the agent's next tool call resumes the VM
  (`cloud/daytona/context.py`, `_ensure_started`);
- never build, verify or Code Mode (`websandbox-*`) sandboxes;
- a failed stop is logged and the next VM is tried.

Worker log: `admission: stopped idle workspace VM`. If nothing could be stopped (or
eviction is off), the original refusal goes to the capacity retries below.

## What a build does when the org is full

Only a `DaytonaValidationError` whose message names an org limit (`Total memory/cpu/
disk limit exceeded`, a sandbox quota) counts as capacity. Anything else (Daytona not
configured, auth, network, a bad image) is still `sandbox_unavailable:no_sandbox`.

- The arq job re-queues itself with a jittered backoff (15, 30, 45, 60, 60, 60 s,
  ±25%), about five minutes in total.
- While it waits the build reads `queued` with reason `waiting_for_capacity`, and
  agents/users see "Build capacity is full right now; the build is queued and will
  start when a slot frees up."
- A job superseded by a newer publish or edit stops retrying.
- When the retries run out it fails with `sandbox_unavailable:capacity`: "Build
  capacity is full right now; try again in a few minutes."

Logs to grep on the worker: `Daytona capacity full` (retrying) and `Daytona capacity
still full` (gave up).

## Freeing capacity from the dashboard

1. Open the Daytona dashboard (app.daytona.io) and pick the production org.
   **Limits** (under the org settings) shows current usage against the caps.
2. Open **Sandboxes** and filter to `started`. Sort by last activity.
3. Build sandboxes are named `paw-build-*` and `paw-verify-*`; filter by the label
   `paw_lane` to see them, and `paw_pocket` / `paw_job` name the pocket and job. The
   reaper deletes old ones within about 10 minutes of them passing the job budget. One
   running longer than the build timeout (10 minutes by default) is a leak: if you
   cannot wait for the reaper, **delete** it.
4. `websandbox-*` is a Code Mode sandbox. Its files live in the durable project, so a
   long-idle one can be **deleted**; the user re-provisions on open.
5. Workspace VMs (`paw-ws-*`) hold a workspace's project files.
   **Stop** (or archive) an idle one; do **not** delete it unless the workspace is
   gone. The agent restarts a stopped or archived VM on its next tool call.
6. Existing workspace VMs keep the lifetime they were created with. To apply the new
   30-minute auto-stop to an old VM, set it in the sandbox's settings in the
   dashboard (or `PATCH /api/v1/workspace/vm/config` and re-provision).

The same from the CLI: `daytona sandbox list`, `daytona sandbox stop <id>`,
`daytona sandbox delete <id>`.

If the org is routinely full with nothing leaked, the fix is a bigger org limit (ask
Daytona support) plus a matching `PAW_SITES_DAYTONA_MEMORY_BUDGET_GIB`, not shorter
retries.
