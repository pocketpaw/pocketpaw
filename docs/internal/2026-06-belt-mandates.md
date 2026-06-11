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

## Demo-bar concessions (each marked in code)

1. **Manual shift trigger only.** `cadence: "weekly"` is stored but not
   scheduled; the autopilot PR wires the scheduler (and Foresight — explicitly
   NOT integrated here).
2. **Deps patrol advisory data is a hardcoded table** (`patrols.KNOWN_STALE`).
   The manifest parsing + sighting plumbing are production-shaped; only the
   data source is stubbed.
3. **Task dispatch announces, it does not run.** `BusTaskDispatcher` emits
   `belt_run_updated(status="dispatched", stage="station")` through the
   existing belt service under a synthetic run id (`<plan_action_id>:t<n>`).
   Wiring an autonomous develop-station runner that picks the task up is the
   autopilot PR's job. The `TaskDispatcher` protocol is the swap point.
4. **LLM transport is the `claude` CLI shell-out** behind the `PlanLlm`
   protocol; an SDK transport can replace it without touching the foreman.
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
