<!-- Runbook: Daytona org resource limits, how site builds behave when the org is full,
     and how to free capacity from the Daytona dashboard. -->

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
| Site publish / preview build (`sites/daytona_runner.py`) | 2 / 4 GiB / 10 GiB | build budget + margin | SDK default | 0 (on stop) | explicit delete when the build ends |
| html browser verify (`sites/browser_check.py`) | 1 / 2 GiB / 5 GiB | verify budget + 10 min | SDK default | 0 (on stop) | explicit delete when the check ends |
| Code Mode sandbox (`cloud/websandbox/provision.py`) | per request | 5 min | 5 min | 0 (on stop) | reaper + Daytona; project lives in S3 |
| Workspace VM (`cloud/daytona/router.py`, agent `pocketpaw_daytona` tools) | 2 / 4 GiB / 10 GiB | 30 min (config, clamped 5 min to 1 day) | 1 day | never | persistent; resumed on next use |

So two builds plus one idle workspace VM already fill 10 GiB. Before this change the
workspace VM stopped after 3600 **minutes** (60 hours; the config value was seconds
but reached Daytona unconverted), which is how idle agent boxes starved builds.

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
3. Build sandboxes are named `paw-build-*` and `paw-verify-*`. One running longer than the
   build timeout (10 minutes by default) is a leak from a crashed worker: **delete** it.
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
Daytona support), not shorter retries.
