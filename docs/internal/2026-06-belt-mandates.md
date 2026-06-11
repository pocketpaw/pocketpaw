<!-- docs/internal/2026-06-belt-mandates.md
     Created: 2026-06-11 (feat/belt-mandates) — anatomy, endpoints, and
     demo-bar concessions for the MANDATE primitive.
     Updated: 2026-06-11 (UI contract sync) — response envelopes, dual-shape
     feedback, pawprint item shape, `patrols` toggles, POST .../plan/resolve,
     and the `belt_plan` realtime topic. -->

# Belt Mandates — the standing JOB primitive

A **mandate** is a standing job the Belt holds over time — the FDE-retainer
counterpart to the Belt's one-shot develop-station runs. Instead of a human
handing the station a task, the mandate **senses** its surface, **judges** what
(if anything) is worth doing, routes that judgment through a **human gate**,
and only then dispatches work.

Validated in simulation (5/5 scenarios) before this implementation;
productionized here at **demo bar** (manual shift trigger, stubbed advisory
data, synthetic dispatch).

## Anatomy

```
MANDATE  (charter: goal, KPIs, says_no, boundaries, budget, cadence; surface: repo)
   │
   ├── PATROLS sense the surface (scoped by the
   │   mandate's `patrols` toggles) ───────────► SIGHTINGS
   │     • deps      — manifest scan (pyproject/package.json) vs advisory table
   │     • feedback  — human intake (POST .../feedback)
   │
   └── SHIFT (manual trigger, one per cycle)
         1. sense   — run patrols, persist new sightings (deduped)
         2. judge   — the FOREMAN makes ONE LLM call over:
                      charter (verbatim, BOUNDARIES first) + sighting digest
                      since last shift + last 3 shifts' outcomes + soul recall
         3. validate — machine checks on ACTION fields ONLY
                      (budget cap, evidence refs, boundary phrases in
                      title/expected_outcome — the `why` narration is NEVER
                      scanned: a good foreman names forbidden things when
                      refusing them)
         4. gate    — PlanProposal lands as an Instinct `belt_plan` Action
                      (the third blob peer of `_pocket_write`/`_code_change`)
            ── or ── stood_down: empty plan, a SUCCESS state; the chain opens
                      and closes in the trigger, no human gate for a no-op
         5. dispatch — on human APPROVE, the plan executor re-validates
                      (mandate active, budget unchanged) and dispatches each
                      task as a Belt run; on REJECT the router closes the
                      chain and the shift records the reason
```

### Decision chains (RFC 09)

One shift = one chain, **exactly one `decision.completed`** per chain:

| Path        | Chain |
|-------------|-------|
| dispatched  | `agent.proposed → human.corrected(accepted\|edited) → decision.completed(passed=True, action_outcome="dispatched", task_count, run_refs)` |
| rejected    | `agent.proposed → human.corrected(rejected) → decision.completed(passed=False, action_outcome="rejected")` (router owns the close) |
| stood_down  | `agent.proposed → decision.completed(passed=True, action_outcome="stood_down")` (trigger owns both; no human event) |
| gate failure| `agent.proposed → human.corrected → decision.completed(passed=False, action_outcome="failed", error_class=…)` (executor `_fail` chokepoint) |

The executor mirrors `ee.cloud.belt.executor` exactly: a single `_fail`
chokepoint per failure path, success emits once at the end, chain emits are
best-effort and never break the approve response.

### The foreman

`ee/pocketpaw_ee/cloud/mandates/foreman.py`. One judgment call per shift
through a pluggable `PlanLlm` protocol, selected by `POCKETPAW_MANDATE_LLM`:

- `claude` (default) — shells `claude -p <prompt> --output-format json`,
  parses the envelope's `result`, tolerates fenced JSON.
- `mock` — deterministic (one task per sighting, severity-ranked, budget-capped;
  `no_action` on a quiet digest). Tests script it via `foreman.set_mock_plan`.

The prompt encodes every sim-validated rule: charter verbatim with BOUNDARIES
prominent; at most `budget.max_tasks_per_shift` tasks; every task cites
sighting ids and names an expected KPI direction; an empty plan with a reason
is correct and respected; boundaries override KPI opportunities; never repeat
a failed approach without stating what changed; strict JSON only.

## Endpoints (`/api/v1/belt/mandates`, RBAC mirrors the belt console)

| Method | Path | Gate | What |
|--------|------|------|------|
| POST | `/belt/mandates` | `belt.manage` | Create (charter body + `patrols` senses toggles) → `{mandate}` |
| GET | `/belt/mandates` | `belt.read` | `{mandates}` + health (last shift state, open gate count, sighting count) |
| GET | `/belt/mandates/{id}` | `belt.read` | Bare detail: charter, patrols, recent shifts, sightings-by-patrol |
| POST | `/belt/mandates/{id}/feedback` | `belt.manage` | Intake patrol → Sighting. TWO shapes, discriminated on `kind`: general `{text, severity?, source}` → sighting dict (autopilot keeps using this); teaching `{kind: reject\|edit\|plan, reason, shift_no?, task_title?}` → `{ok: true}` (the gate UI's channel) |
| GET | `/belt/mandates/{id}/sightings` | `belt.read` | `{sightings}`, newest-first |
| POST | `/belt/mandates/{id}/shift` | `belt.manage` | Run a shift (manual trigger) → `{shift: {shift_id, no, state, plan_action_id, task_count, no_action_reason}}` |
| POST | `/belt/mandates/{id}/plan/resolve` | `belt.manage` | The console's gate action: `{shift_no, decisions: [{index (0-based), decision: approve\|reject\|edit, edited_title?, reason?}]}` → `{shift}`. Every task needs exactly one decision. |
| POST | `/belt/mandates/{id}/autopilot` | `belt.manage` | Start/stop Foresight-seeded simulated users feeding the feedback patrol: `{action: start\|stop, users?: int (default 3, max 10)}` → `{mandate}`. START persists `autopilot={on, users}`, runs ONE cycle immediately, spawns the background loop; STOP cancels it. |
| GET | `/belt/mandates/{id}/pawprints` | `belt.read` | `{pawprints}` past-tense feed; item shape `{id, mandate_id, shift_no, kind, summary, evidence_refs, ts}` |

Pawprint `kind`s: the UI consumes `executed` / `rejected` / `edited` /
`stood_down`; the feed also emits `proposed` / `approved` / `failed` /
`planning` (a documented superset, same item shape). `edited` fires when the
approval carried human edits (Corrections exist on the plan Action).

**The Instinct gate stays the single chain authority.** `plan/resolve` maps the
console's per-task verdicts onto the REAL instinct paths: any approved/edited
subset becomes an approve-WITH-EDITS (the blob's task list filtered/retitled —
the standard Corrections machinery; the executor dispatches the kept tasks),
and an all-reject becomes a plain reject (the router closes the chain).
Rejected tasks are recorded as teaching sightings. Direct
`POST /instinct/actions/{id}/approve|reject` (the Tray, MCP, bulk endpoints)
keeps working unchanged — both surfaces hit the same transition, so the chain
still closes exactly once.

**Realtime:** when a plan proposal lands at the gate, the service emits a
`belt_plan` event on the workspace bus (payload `{workspace_id, mandate_id,
proposal}`), mirroring `belt_run_updated`'s audience fan-out; the mandates page
subscribes to that topic.

## Plan-feature gating posture

The mandates router intentionally matches the belt console's posture: routes
are gated by license + RBAC (`belt.read` / `belt.manage`) but carry **no**
`require_plan_feature` tier gate at demo bar (the belt console router doesn't
either). Tighten both surfaces together before GA.

## Storage

4-file entity at `ee/pocketpaw_ee/cloud/mandates/` (+ `patrols.py`,
`foreman.py`, `executor.py`, `soul_link.py`, `events.py` supporting modules —
the same beyond-four shape the belt console uses). Beanie docs (`MandateDoc`,
`ShiftDoc`, `SightingDoc`; all workspace-keyed) live in `mandates/domain.py`,
imported ONLY by `mandates/service.py`, and register into `init_beanie` via a
lazy import in `cloud/models/__init__.py` (the calendar-doc pattern).

## Soul wiring (demo bar)

When a mandate binds `soul_path`, `soul_link.py` recalls up to 5 memories
before planning and appends an episodic shift summary after every terminal
(dispatched / rejected / stood_down) via the real soul-protocol API
(`Soul.awaken → recall/remember → save_local`). Best-effort throughout — a
soul failure never wedges a shift.

## Autopilot — Foresight-seeded simulated users (feat/belt-autopilot)

`POST /belt/mandates/{id}/autopilot {action, users?}` turns a mandate's feedback
patrol into a self-feeding loop. When ON, a per-mandate background asyncio task
(`autopilot.start_autopilot`, registered in the module's process-local `_TASKS`
registry keyed by mandate id — the same create-task + cancel-and-await shape as
`decisions._action_sweeper`, but per-mandate so STOP cancels exactly one) runs a
cycle every `POCKETPAW_MANDATE_AUTOPILOT_INTERVAL` seconds (default 300); START
also runs ONE cycle immediately (synchronously, inside the request, so START's
response already reflects the first cycle's sightings).

Each cycle:

1. Reads the bound repo's surface — the README's first ~800 chars + up to 10
   recent commit titles (`git log`, argv-only subprocess).
2. Builds N personas (1-10, default 3) from a deterministic palette, each seeded
   with a **Foresight `OceanDrift`** temperament (`ee.foresight.persona.OceanDrift`
   — the genuine bridge to the sim module).
3. Each persona emits 1-3 structured `{text, severity 1-5}` feedback items
   through a pluggable `UserSim` interface, POSTed through the EXISTING
   `service.file_feedback` path (NOT raw HTTP) with `source="autopilot:<persona>"`
   — so they become feedback Sightings the next shift's foreman cites.

**Which Foresight path + why.** The brief allows a lighter persona LLM call when
foresight's scenario runner is too heavyweight per-cycle. We took the **lighter
path**: foresight's `run_scenario` / OASIS substrate is a tick-based *world*
simulation (CAMEL + OASIS + a YAML scenario config, anchors, prediction records)
built to rehearse a *decision across a population*, not "use a product and emit
free-text feedback" — spinning it up per cycle would pull in torch/igraph/pandas
and a multi-tick loop, and its action vocabulary (`action/rationale/put`) is the
wrong shape. Instead the persona transport reuses the **foreman's proven
pluggable pattern** (`POCKETPAW_MANDATE_LLM=claude|mock` — the SAME env) behind
the `UserSim` interface, and bridges foresight's `OceanDrift` value object for
the persona seed. Mock mode is deterministic + seeded (a per-persona RNG seeded
on the persona name) so tests get stable sightings. A later PR can swap the full
scenario runner in behind `UserSim` with no caller change.

**Resilience.** Autopilot never crashes a shift or the app: every persona call,
every feedback POST, and every cycle is wrapped — a failure is logged and
swallowed per-cycle; the loop sleeps and retries next interval. The persisted
`MandateDoc.autopilot = {on, users}` is the source of truth for whether
autopilot *should* run; the live task is process-local. State rides the detail +
list wire + the `MandateAutopilotChanged` event
(`{workspace_id, mandate_id, on, users}`).

**Lifespan wiring.** A restart re-derives the loops from the persisted flags:
`reconcile_autopilot_tasks` (lifespan startup) queries every ACTIVE mandate with
`autopilot.on=True` (via `service.list_autopilot_enabled` — a deliberate
cross-workspace system read, the stale-run-sweeper posture; paused mandates are
skipped) and restarts each loop with `run_immediate=False`, so a boot never
storms a cycle per mandate — the first cycle lands after the normal interval.
`shutdown_all_autopilot_tasks` (lifespan shutdown) cancels + awaits every
registered loop so the process exits without orphaned tasks. Both hooks are
registered in `cloud/__init__.mount_cloud` under the same
`POCKETPAW_CLOUD_SCHEDULER_ENABLED=true` gate as the decisions reconciler / run
sweeper, so pytest runs never spawn background loops that outlive the test.

## Dispatcher reality — REAL station runs vs. announce-only (feat/belt-autopilot)

`POCKETPAW_MANDATE_DISPATCHER=station|bus` selects the `TaskDispatcher`
(default `station` when the belt plumbing imports, else a clean fall-back to
`bus`):

- **`station` (`StationTaskDispatcher`, the real one).** Each approved plan task
  becomes a **real Belt run** in the console Runs tab.
- **`bus` (`BusTaskDispatcher`, the prior default).** Announce-only —
  `belt_run_updated(status="dispatched", stage="station")` under a synthetic run
  id (`<plan_action_id>:t<n>`); no run record is created.

**HONESTY — is a genuinely headless station run reachable? NO.** The Belt
*develop station* is an **interactive chat-agent loop**: the `/belt` surface
preamble (`cloud/surface/handlers/belt.py`) drives a Claude chat session that
ORIENTs, DEVELOPs, and produces a unified diff, which the
`mcp__pocketpaw_belt__belt_propose_change` tool then files as a `code_change`
Instinct Action. There is **no programmatic "task → diff" runner** to call from
a dispatcher — the diff is the *output* of an LLM chat session, not a function.

So `StationTaskDispatcher` does the **closest real thing**: it files a real
`code_change` Instinct Action per task (the SAME row type the console Runs tab
reads and the belt gate executes) carrying the task text, in a **queued** state
(`station_pending=True`, no diff yet, repo pre-bound to the mandate's surface).
The runs read model surfaces it as `status=queued / stage=station`; a human opens
the `/belt` station for that queued run (one click) and drives it to a diff,
which rides the existing belt gate as normal. This is a genuine run record, not a
bus echo — the tests assert the persisted `code_change` Action + its
`station_pending` queued state, not a bus message. Because a queued run carries
no diff it is **not auto-applyable**: the belt executor refuses a
`station_pending` blob loud (`error_class="StationPending"`) if it is ever
(mistakenly) approved. When a real headless station runner lands, swap its call
into `StationTaskDispatcher` behind the same `TaskDispatcher` protocol — no
caller change.

## Demo-bar concessions (each marked in code)

1. **Manual shift trigger only.** `cadence: "weekly"` is stored but not
   scheduled (autopilot seeds *sightings*, not shift triggers — wiring the
   cadence scheduler is still a later PR).
2. **Deps patrol advisory data is a hardcoded table** (`patrols.KNOWN_STALE`).
   The manifest parsing + sighting plumbing are production-shaped; only the
   data source is stubbed.
3. **Station dispatch QUEUES a real run; it does not auto-produce the diff.**
   `StationTaskDispatcher` (default) files a real queued `code_change` run a
   human starts in the console — see *Dispatcher reality* above for why a fully
   headless diff-producing run is not reachable. `bus` mode keeps the prior
   announce-only behaviour. The `TaskDispatcher` protocol is the swap point for a
   future headless runner.
4. **LLM transport is the `claude` CLI shell-out** behind the `PlanLlm` (foreman)
   and `UserSim` (autopilot) protocols; an SDK transport can replace either
   without touching the caller.
5. **Pawprints read the store, not the journal.** The feed derives from
   ShiftDoc states + the plan Action's status/blob — the same facts the chain
   folded from; a journal-walking narrator can replace it later.

## Tests

`tests/cloud/test_belt_mandates.py`. The hard gate
(`test_full_shift_gate_one_clean_chain`) drives create → feedback → shift →
real-instinct-router approve → dispatch and asserts EXACTLY ONE
`decision.completed` (this repo's documented chain-doubling seam). Also pinned:
stood_down, budget cap, boundary-check-ignores-`why`, patrol intake, deps
patrol + dedup, tenant isolation, reject-closes-once.

`tests/cloud/test_belt_autopilot.py` (feat/belt-autopilot) pins both new pieces:
autopilot start persists state + runs an immediate cycle whose sightings carry
`source="autopilot:*"`; the background task start/stop lifecycle (asserted
directly against the module — a TestClient request runs on its own short-lived
loop, so it can't observe the task); a full loop autopilot → shift (the mock
foreman cites the autopilot sightings) → resolve-approve → the **real**
`StationTaskDispatcher` files one queued `code_change` station run per task
(`status=queued`, `station_pending=True`, repo pre-bound); the dispatcher env
selection (`station`/`bus`); the `bus` path's announce-only behaviour; the
startup reconciler (restarts exactly the ACTIVE autopilot-on mandates after a
simulated restart, skips off/paused, and the shutdown drain cancels every loop);
and tenant isolation on the autopilot endpoint. All run the deterministic mock
LLM/UserSim.
