# Mission Control — backend integration + primitive audit

*Companion to [`2026-05-mission-control.md`](./2026-05-mission-control.md). The vision doc says what Mission Control is; this doc lists, primitive by primitive, **what already exists in the codebase vs what needs to be built** to wire the mock UI to real data. Honest accounting, not aspirational scoping.*

*Audience: Rohit (backend), reviewer of any Mission Control v0.5 / v1 work.*

---

## TL;DR

**~60% of Mission Control wires to endpoints that already exist.** The Tray + Pawprints + per-item audit are essentially shipped — `ee/instinct/router.py` already exposes propose/approve/reject/query-audit. Fabric, Pockets, Soul, Connectors, Notifications are all in place. The two new entities we genuinely need:

1. **Tasks** — a unified `WorkItem` entity covering Nudges + agent tasks + audit projections, with cycle assignment. Doesn't exist.
2. **Cycles** — time-boxed work windows + a per-cycle daily series for the burnup chart. Doesn't exist.

Plus one aggregation endpoint (Outcomes summary) and one cross-cutting activity feed buffer.

Everything else is glue.

---

## What exists in `ee/` today

```
ee/
├── instinct/                ← Nudge proposal / approval / audit pipeline
│   ├── router.py            (Tray + Pawprints endpoints live here)
│   ├── store.py
│   ├── models.py
│   ├── trace.py
│   ├── correction.py
│   └── correction_soul_bridge.py
├── fabric/                  ← Typed ontology + provenance
│   ├── router.py            (objects, links, types, query)
│   ├── journal_store.py
│   ├── policy.py
│   ├── projection.py
│   └── store.py
└── cloud/                   ← Workspace SaaS surfaces (per CLAUDE.md 4-file shape)
    ├── pockets/             (CRUD + widgets · already 4-file)
    ├── agents/              (CRUD · already 4-file)
    ├── notifications/       (list / read / clear)
    ├── connectors/
    ├── kb/
    ├── chat/
    ├── workspace/
    ├── auth/
    ├── sessions/
    ├── files/
    └── uploads/
```

`ee/instinct/` and `ee/fabric/` are *engine primitives* — they live at the `ee/` root, not under `ee/cloud/`. `ee/cloud/` is the multi-tenant SaaS surface. Mission Control is hosted in `ee/cloud` but consumes both.

---

## Primitive-by-primitive audit

### 1. Instinct → The Tray + Pawprints + per-item action history

**What Mission Control needs.** List pending Nudges scoped to the current user + workspace. Approve / reject / bulk-approve a Nudge. Query the audit log (Pawprints) with filters. Fetch a single Pawprint's full reasoning trace.

**What's already in `ee/instinct/router.py`:**

| Endpoint | Status | Maps to |
|---|---|---|
| `POST /propose_action` | ✅ exists | Agent proposes a Nudge (called from agent runtime, not MC) |
| `GET /pending_actions?pocket_id=…` | ✅ exists | The Tray feed — close but needs `assignee` filter |
| `GET /actions` | ✅ exists | Full Instinct action list |
| `POST /{action_id}/approve` | ✅ exists | Single-item approve from detail panel |
| `POST /{action_id}/reject` | ✅ exists | Single-item reject from detail panel |
| `GET /audit` (`query_audit`) | ✅ exists | Pawprints section feed |
| `GET /audit/{id}` (`get_audit_entry`) | ✅ exists | Per-Pawprint detail expand |
| `GET /audit/export` | ✅ exists | "Export" affordance (future) |
| `GET /corrections` | ✅ exists | Reasoning-trace surface (future) |

**Gaps for v0.5:**

- **Bulk approve / bulk reject.** Current router takes a single `action_id` in the path. Mission Control's bulk action bar selects N items and POSTs them in one call. Two options: (a) add `POST /actions/bulk-approve` body `{ids, note?}` in `ee/instinct/router.py`; (b) the frontend fans out N parallel single-item calls. **Recommend (a)** — atomic semantics, single audit transaction per bulk operation, cheaper round-trip. ~20 lines of router code + a `bulk_approve` service method.
- **`assignee` filter on `pending_actions`.** Today it filters by `pocket_id`. For The Tray we want `pending_actions?assignee=<user_id>` so we don't show other humans' pending items. ~5-line change in the existing handler.
- **Response shape alignment.** Current `pending_actions` likely returns `Action` shape (proposal-flavored). Mission Control consumes `WorkItem` shape (status + assignee + cycle_id + source). Map at the cloud-layer boundary: `ee/cloud/mission_control/service.py` (new) wraps `instinct.pending_actions()` and projects to `WorkItemResponse`. Don't change Instinct's internal types — wrap.

**ee/cloud 4-file conformance** (per `pocketpaw/CLAUDE.md`): new wrapper lives at `ee/cloud/mission_control/{domain.py, dto.py, service.py, router.py}`. Service imports `ee.instinct.store` for reads, never touches Beanie directly for Instinct entities. Tenant filter (`workspace_id`) on every read.

### 2. Fabric → typed object references in items

**What Mission Control needs.** When a Work Item references a Fabric object (e.g., a Nudge proposing to update `Customer:Stripe → ContractStatus:active`), the detail panel surfaces the typed reference with link + provenance.

**What's already in `ee/fabric/router.py`:**

| Endpoint | Status |
|---|---|
| `GET /fabric/types` | ✅ exists |
| `GET /fabric/objects` | ✅ exists |
| `GET /fabric/objects/{id}` | ✅ exists |
| `POST /fabric/objects` | ✅ exists |
| `POST /fabric/query` | ✅ exists |
| `GET /fabric/links` | ✅ exists |
| `POST /fabric/links` | ✅ exists |
| `GET /fabric/stats` | ✅ exists |

**Gaps for v0.5:** **None for read-side.** Mission Control just needs to display Fabric references; the existing `GET /fabric/objects/{id}` covers the look-up. The WorkItem shape needs an optional `fabric_refs: FabricRef[]` field, and the detail panel renders chips for each.

**Gaps for v1:** Inline editing of a Fabric object from the detail panel (when approving a Nudge that proposes a Fabric mutation, show the diff inline before commit). Defer until UX tested.

### 3. Pockets → scope for cycles and work items

**What Mission Control needs.** Items + cycles are scoped to a Pocket. The left rail lists Pockets to filter the feed; cycles attach to a Pocket.

**What's already in `ee/cloud/pockets/router.py`:**

| Endpoint | Status |
|---|---|
| `GET /pockets` | ✅ exists |
| `GET /pockets/{id}` | ✅ exists |
| `POST /pockets` | ✅ exists |
| `PATCH /pockets/{id}` | ✅ exists |
| `DELETE /pockets/{id}` | ✅ exists |
| `POST /pockets/{id}/widgets` etc. | ✅ exists (used by ripple) |

**Gaps:** **None.** Mission Control reads Pockets but does not mutate them. Left-rail filter is purely a frontend concern over the existing `GET /pockets` payload.

### 4. Soul → agent identity + memory exposure

**What Mission Control needs.** When viewing an agent-assigned item, surface the agent's Soul context: name, age, last-evolved timestamp, the memory layer that informs this proposal (semantic vs episodic). For v0.5 just the static metadata; v1 lets the operator drill into the relevant memory.

**What's available.** Soul Protocol exposes `soul status`, `soul recall`, etc. via the soul-protocol CLI + MCP tools (per `paw-workspace/.claude/CLAUDE.md`). No HTTP endpoint inside `ee/cloud/`; soul reads happen in-process via the soul-protocol library.

**Gaps:** **A thin read endpoint at `ee/cloud/agents/router.py:GET /agents/{id}/soul-summary`** that returns `{ name, archetype, age_days, last_evolved_at, ocean_traits }`. Backed by a service method that calls `Soul.status()` on the agent's bound soul file. ~30 lines. Already partially present? Worth a grep before building; if `agent.get_status()` returns soul fields, this collapses to a frontend display change.

### 5. Notifications → bridges into Mission Control

**What Mission Control needs.** When a new Nudge lands, notify the assigned human via the existing notification surface. When Mission Control is open, the live SSE event surfaces it directly; when Mission Control is closed, a notification persists. Don't duplicate channels — the existing `ee/cloud/notifications/` handles this.

**What's already there:**

| Endpoint | Status |
|---|---|
| `GET /notifications` | ✅ exists |
| `POST /notifications/{id}/read` | ✅ exists |
| `POST /notifications/clear` | ✅ exists |

**Gaps:** A listener that creates a `Notification` whenever a Nudge in `awaiting_approval` state is routed to a human. Hook lives in `ee/instinct/` (after `propose_action` resolves to a human assignee) → emits `nudge.proposed` → notification service subscribes → creates a notification.

### 6. Channels → multi-surface deploy of approvals

**Not v0.5 scope.** Mission Control is the in-app operator surface. Channel adapters (Slack / Telegram / WhatsApp) for approve-from-message are a follow-up. The pocketpaw channel adapter framework is already in place for this; wiring is straightforward when we get there.

---

## What needs to be built

Three new ee/cloud entities. Each follows the 4-file shape (`domain.py + dto.py + service.py + router.py`) per `pocketpaw/CLAUDE.md`, with `import-linter` contract entry.

### A. `ee/cloud/mission_control/` (the entry-point façade)

**Why a wrapper entity?** Mission Control composes data from Instinct + new Tasks + new Cycles + the activity buffer. Rather than make the frontend stitch four endpoints together, expose a workspace-aware façade that returns the canonical `WorkItem` shape from a single endpoint.

```
ee/cloud/mission_control/
├── domain.py     ← WorkItem value object (frozen, workspace_id required)
├── dto.py        ← WorkItemResponse, ListWorkItemsRequest, BulkActionRequest,
│                   OutcomeSummaryResponse, ListSectionResponse
├── service.py    ← agent_list_work_items(ctx, body)
│                   agent_bulk_approve(ctx, body)
│                   agent_bulk_reject(ctx, body)
│                   agent_outcomes_summary(ctx, body)
│                   agent_list_activity(ctx, body)
└── router.py     ← GET /mission-control/items?section=…&agent=…&pocket=…
                   POST /mission-control/items/bulk-approve
                   POST /mission-control/items/bulk-reject
                   POST /mission-control/items/bulk-reassign
                   POST /mission-control/items/bulk-snooze
                   GET  /mission-control/outcomes?window=24h
                   GET  /mission-control/activity?limit=30
```

Service implementation reads from `ee.instinct.store` (Nudges + Pawprints), `ee.cloud.tasks.service` (the new Tasks entity), and an in-memory `activity_buffer` (see C below). Projects everything into `WorkItem` shape on the way out.

### B. `ee/cloud/tasks/` (the assignable-to-agent work primitive)

**Why a new entity?** A Nudge is a proposal awaiting human approval. A Task is durable work assigned to either a human or an agent. They share a lot of shape but lifecycles differ: a Task spans `proposed → in_progress → done | blocked | failed`, may produce intermediate Nudges, and ties to a Cycle. Modeling them as the same Mongo collection makes queries simpler; keeping `Nudge` as a domain type within `Task` (status = `awaiting_approval`) is cleaner than a separate entity.

```
ee/cloud/tasks/
├── domain.py     ← Task (assignee polymorphism, status enum, cycle_id?,
│                   source kind, priority, fabric_refs[])
├── dto.py        ← CreateTaskRequest, UpdateTaskRequest, ListTasksRequest,
│                   TaskResponse, ClaimTaskRequest
├── service.py    ← agent_create_task, agent_list_tasks,
│                   agent_update_task, agent_claim_task,
│                   agent_complete_task, agent_block_task,
│                   agent_reassign_task
└── router.py     ← POST /tasks
                   GET  /tasks?assignee=…&status=…&cycle_id=…
                   PATCH /tasks/{id}
                   POST /tasks/{id}/claim       (agent picks it up)
                   POST /tasks/{id}/complete
                   POST /tasks/{id}/block
                   POST /tasks/{id}/reassign
```

**Agent task-claim flow** (v1):
- A user (or another agent) creates a Task with `assignee.kind = agent`. Status: `proposed`.
- The assigned agent's runtime polls `GET /tasks?assignee=<my_id>&status=proposed` on its loop OR receives the `task.proposed` SSE event.
- Agent calls `POST /tasks/{id}/claim` → status: `in_progress`.
- Agent executes. Sub-actions surface as `activity.recorded` events bound to the task id.
- Agent completes via `POST /tasks/{id}/complete` (auto-archives) or `POST /tasks/{id}/block` (surfaces as a Snag).

Pair the claim/complete tools with the existing pocket-specialist subagent's MCP surface from [#1069](https://github.com/pocketpaw/pocketpaw/pull/1069). Same architectural pattern.

### C. `ee/cloud/cycles/` (time-boxed work windows)

**Why a new entity?** Cycles are aggregate views over Tasks within a time window. Could be a derived projection, but the burnup chart needs daily snapshots that don't reconstruct cleanly from raw Tasks alone — we need a job that snapshots scope/started/completed each midnight.

```
ee/cloud/cycles/
├── domain.py     ← Cycle (pocket_id?, start, end, status, scope, started,
│                   completed, daily snapshots)
├── dto.py        ← CreateCycleRequest, CycleResponse, CycleDailyPointResponse
├── service.py    ← agent_create_cycle, agent_list_cycles, agent_get_cycle,
│                   agent_close_cycle (rolls incomplete tasks to next)
│                   _snapshot_cycle_daily (background job)
└── router.py     ← POST /cycles
                   GET  /cycles
                   GET  /cycles/{id}
                   POST /cycles/{id}/close
                   GET  /cycles/{id}/items
```

**Daily snapshot job.** A simple async loop running once per day (or per workspace policy) computes the (scope, started, completed) tuple for each active cycle and appends to its `daily` array. Cycle close moves un-done tasks to the next cycle (matches Linear's no-keep-incomplete behavior).

### D. Cross-cutting: activity buffer

**What it is.** A bounded in-memory ring buffer per workspace (~200 entries, 1-hour TTL) capturing every agent tool call, thinking step, completion, and waiting state. Feeds the live ticker in the Mission Control status bar + the full Activity tab.

**Why not persistent?** The activity feed is operational decoration, not source-of-truth. The durable record lives in Pawprints (Instinct's audit). Activity is the live ticker; Pawprints is the journal. Separate concerns, different storage.

**Implementation.** New module `ee/cloud/activity/buffer.py`. Subscribes to the in-process event bus (`ee.cloud._core.realtime.bus`) for `agent.tool_call`, `agent.thinking`, `agent.completed`. Pushes onto a per-workspace deque. Exposes `GET /mission-control/activity` (routed via the façade in A above). Emits a `activity.recorded` SSE event for each entry.

---

## SSE event surface

Existing `push_pocket_mutation` pattern in `ee/cloud/chat/agent_service.py:110` is the precedent. Mission Control adds:

| Event | When | Payload shape |
|---|---|---|
| `work_item.proposed` | new Task or Nudge enters the system | `{ item: WorkItem }` |
| `work_item.updated` | status / assignee / priority change | `{ id, changes: Partial<WorkItem> }` |
| `work_item.resolved` | terminal state (done / reverted / failed) | `{ id, status }` |
| `activity.recorded` | new agent activity entry | `{ event: ActivityEvent }` |
| `cycle.snapshotted` | nightly snapshot job appended a daily point | `{ cycle_id, daily_point }` |
| `cycle.closed` | a cycle ended; items rolled to next | `{ cycle_id, rolled_count }` |

Frontend `RealtimeClient` (`paw-enterprise/src/lib/core/realtime/client.ts`) already supports typed `on<T>()` registration. Each Mission Control pane subscribes to the events it cares about.

---

## Migration plan

Three PRs, in order:

| PR | Scope | Risk |
|---|---|---|
| **1. Mission Control façade + Instinct alignment** | `ee/cloud/mission_control/` 4-file shape. Add `assignee` filter + bulk approve/reject to `ee/instinct/router.py`. Frontend flips `VITE_MISSION_CONTROL_MOCK=false` for The Tray + Pawprints panes; other panes stay mocked. | Low — touches Instinct only at the read-filter level. Existing tests stay green. |
| **2. Tasks entity** | `ee/cloud/tasks/` 4-file shape, agent claim/complete/block tools, lint contract entry. Frontend wires "Agents in flight", "Delegated", "+ new task" → real backend. | Medium — new entity, new SSE events, new agent-side claim tool. Touch-time-migrate `notifications/` listener to subscribe to `task.proposed`. |
| **3. Cycles + activity buffer** | `ee/cloud/cycles/` 4-file shape with daily snapshot job. `ee/cloud/activity/buffer.py` + SSE wire-up. Frontend wires the Cycles tab + activity ticker. | Medium — daily job is the most novel piece. Lightweight: an async task running every 24h per workspace, or driven by a cron primitive. |

After PR 3, Mission Control is fully wired. Total estimate at agent-time scale: **~12–18 agent-hours of crew work**, mostly in PR 2 (Tasks entity carries the most novelty).

---

## Decision log (for the build)

- **Mission Control façade vs direct multi-endpoint calls** → façade. One round-trip per pane, simpler frontend, single tenant-filter chokepoint.
- **Tasks as one entity, Nudge as a status** → yes. The unified `WorkItem` design (frontend) carries through to the backend. Reduces table count, simplifies queries, matches the Linear precedent.
- **Activity buffer in-memory vs persistent** → in-memory. Pawprints is the durable record; activity is the live decoration. Buffer rebuilds on process restart (acceptable — the live ticker doesn't need history).
- **Cycle daily snapshots vs derived on read** → snapshotted. The burnup chart needs historical scope/started/completed at past timestamps which can't be reconstructed from raw Tasks after status changes. Daily snapshot is cheap.
- **Bulk approve as a new endpoint vs frontend fan-out** → new endpoint. Atomic Pawprint, single audit transaction.
- **Soul exposure** → thin read-only `agents/{id}/soul-summary`. Don't expose full memory via HTTP — that's a security surface we don't want until we have a real auth story.

---

## Open questions for the captain / Rohit

These need a call before PR 1 starts.

1. **Should `pending_actions` ever cross workspace boundaries?** Today it filters by `pocket_id`. For Mission Control we want workspace-scoped queries. Is `workspace_id` already an implicit filter via the `ctx.workspace_id` chain, or do we need to make it explicit? *My read: implicit via ctx — confirm.*
2. **Bulk-approve audit semantics.** Single Pawprint with `affected_ids: list[str]`, or N Pawprints (one per item) with a shared `bulk_id`? *Rec: N Pawprints + shared bulk_id. Replay-ability per item, query-able by bulk.*
3. **Task claim contention.** If two agents are eligible for the same Task (rare but possible), what happens? Optimistic claim with status check (first wins, second errors)? *Rec: yes, single-writer claim with Mongo `update_one` on `{id, status: 'proposed'}`.*
4. **Cycle creation — auto or manual?** Linear auto-creates a rolling schedule. For Mission Control / events-production, cycles are likely manual (each engagement is a one-off). *Rec: manual for v1; auto-create as workspace policy in v1.5.*
5. **Soul exposure on agents endpoint** — does any existing route already return soul fields? Need to grep. If yes, augment; if no, add the thin endpoint.

---

## Cross-references

- [`2026-05-mission-control.md`](./2026-05-mission-control.md) — the architectural spec this audit operationalizes.
- [`2026-05-pocket-specialist-and-ripple-mutation.md`](./2026-05-pocket-specialist-and-ripple-mutation.md) — the pocket-specialist subagent pattern from #1069 is the template for the agent task-claim tool surface.
- `pocketpaw/CLAUDE.md` § ee/cloud Code Rules — the 4-file shape every new entity above conforms to.
- `paw-enterprise/PR #181` — the mock UI that calls every endpoint listed in this audit (via `$lib/core/mission-control/api`).
