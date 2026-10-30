# Mission Control — operator surface spec

*Status: design + mock UI first; backend wiring follows. Vocabulary aligns with the captain's prior Paw OS Enterprise Strategy (`paw-enterprise/docs/enterprise/PAW-OS-STRATEGY.md`): **The Tray** (approvals), **Pawprints** (audit), **Nudges** (proposed actions), **Instinct** (decision pipeline).*

*Owners: Prakash (direction), Rohit (backend integration after UI lands). Reviewers: anyone touching `ee/cloud/instinct/`, `ee/cloud/pockets/`, or paw-enterprise's `os/` components.*

---

## Why this exists

PocketPaw today has the right primitives for human-agent collaboration — Pockets, Instinct, Nudges, Pawprints, agent runtime — but **no single screen that lets a human see all of it at once.** Each pocket is a scoped decision context. Mission Control is the *opposite-shape* surface: a cross-cutting operator view across every pocket, every agent, every pending Nudge, every recent Pawprint.

For Nerve Systems clients (Shawn, SNCTM, NexWrk hospitality engagements), this is the moneyshot screen. The pitch is: *"Log in once a day. This screen shows what your agents did, what needs your eyes, what shipped. Everything else is delegated."* That's not "look at how this Pocket works" — it's "look at this one screen that shows your whole company."

## Why standalone, not a Pocket

A Pocket is a *scoped workspace*: one job, one team's data, one Instinct policy. Mission Control is a *cross-pocket aggregate view* — same shape relationship as filesystem-vs-file or task-manager-vs-task. Trying to build it as a Pocket means putting the manager-of-X inside one X. Wrong layer.

Two separate constraints reinforce this:

1. **Mission Control needs latency + interactivity** (sub-second updates on Nudges, click-expand-approve, real-time activity stream) that the current Pocket primitive can't deliver until granular UI-tree mutation ops land (see [`2026-05-ripple-mutation-handover.md`](./2026-05-ripple-mutation-handover.md) PR 1).
2. **The shell is operator-meta**, not domain-specific. RBAC, workspace switching, global search, keyboard shortcuts, cross-pocket navigation are best as plain Svelte code. Generating them as a spec gives no benefit; the per-client customization happens at the **pane** level.

The right shape: **a standalone shell route in paw-enterprise** (`/mission-control`) that hosts panes. Panes are bespoke Svelte today, Ripple-rendered later as PR 1's granular ops land. Per-client customization happens by swapping pane specs, not rewriting the shell.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  workspace ▼   global search                🔔 4 pending   user ▼   │   ← shell header
├──────────────┬──────────────────────────────────────────┬───────────┤
│              │  ┌─ The Tray (Nudges queue) ──────────┐  │           │
│  Pockets     │  │ • email to Stripe (sales pkt)      │  │           │
│   • sales    │  │ • vendor confirm (events pkt)      │  │  context  │
│   • support  │  │ • schedule shift (ops pkt)         │  │  panel    │
│   • events ●│  └────────────────────────────────────┘  │  (full    │
│              │                                          │   trans-  │
│  Agents (3)  │  ┌─ Agent activity feed ─────────────┐   │   cript,  │
│   • notes    │  │ 10:42 sales-agent reading hubspot  │  │   related │
│   • triage●  │  │ 10:41 ops-agent posting to slack   │  │   pocket, │
│   • coord    │  │ 10:40 events-agent waiting (you)   │  │   related │
│              │  └────────────────────────────────────┘  │   Nudges) │
│  Tasks (you) │                                          │           │
│   • approve  │  ┌─ Outcomes (24h) ───────────────────┐  │           │
│   • review   │  │   shipped 47   reverted 2  pending │  │           │
│              │  └────────────────────────────────────┘  │           │
│              │                                          │           │
│              │  ┌─ Pawprints (recent audit) ─────────┐  │           │
│              │  │ • 10:42 ops-agent: schedule update │  │           │
│              │  │ • 10:41 sales-agent: email sent    │  │           │
│              │  └────────────────────────────────────┘  │           │
├──────────────┴──────────────────────────────────────────┴───────────┤
│  3 agents · 4 Nudges pending · system: healthy · 12 actions / hr     │
└─────────────────────────────────────────────────────────────────────┘
```

**Shell** owns: layout, workspace context, RBAC, search, navigation, real-time fan-out (SSE), keyboard shortcuts, notification badges.

**Panes** (independent components, each backed by one API call + optional SSE subscription):

| Pane | Source endpoint | Status |
|---|---|---|
| The Tray (Nudges queue) | `GET /api/v1/instinct/nudges?status=pending` | ✅ exists (ApprovalsPanel.svelte today) |
| Agent activity feed | `GET /api/v1/agents/activity` + SSE | 🟡 partial (activity route exists, no unified endpoint) |
| Outcomes (24h) | `GET /api/v1/instinct/outcomes?window=24h` | ⛔ missing — needs aggregation endpoint |
| Pawprints (recent) | `GET /api/v1/audit?limit=20` | ✅ exists (AuditLogPanel.svelte today) |
| Tasks-for-humans | `GET /api/v1/tasks?assignee=me&status=open` | ⛔ missing — needs new entity |
| Left rail: pockets | `GET /api/v1/pockets` | ✅ exists |
| Left rail: agents | `GET /api/v1/agents?status=active` | 🟡 partial (no status filter) |
| Context panel | `GET /api/v1/instinct/nudges/{id}` | 🟡 partial (need full envelope) |

**Real-time updates** flow through the existing `RealtimeClient` at `paw-enterprise/src/lib/core/realtime/client.ts`. Event types to subscribe in Mission Control:

- `nudge.proposed` → prepend to The Tray
- `nudge.resolved` → remove from The Tray, push to Pawprints
- `agent.activity` → prepend to activity feed
- `pawprint.recorded` → prepend to Pawprints
- `outcome.metered` → refresh outcomes pane

## ee/cloud conformance

Per `pocketpaw/CLAUDE.md` § ee/cloud Code Rules, every new/modified endpoint follows the 4-file shape. Two new entities ship as Mission Control evolves:

### 1. Aggregation endpoints on existing `instinct/`

Existing `ee/cloud/instinct/` likely gains:

```
ee/cloud/instinct/
  domain.py         # Nudge value object (existing)
  dto.py            # + ListNudgesRequest, NudgeResponse, OutcomeSummaryResponse
  service.py        # + agent_list_nudges(ctx, body), agent_outcomes_summary(ctx, body)
  router.py         # + GET /nudges, GET /outcomes, POST /nudges/{id}/approve, POST /nudges/{id}/reject
```

All reads filter `workspace=ctx.workspace_id` (Rule 7). Approval/reject writes emit `nudge.resolved` events (Rule 9). No `HTTPException` in services or routers (Rule 10).

### 2. New `tasks/` entity for tasks-for-humans

Tasks are work-items explicitly assigned to a human (could be queued by an agent, by another human, or by Instinct's reject path). Distinct from Nudges (which are agent *proposed actions* awaiting approval).

```
ee/cloud/tasks/
  domain.py         # Task(id, workspace_id, assignee_id, status, source_pocket_id?, ...)
  dto.py            # CreateTaskRequest, ListTasksRequest, UpdateTaskRequest, TaskResponse
  service.py        # agent_create_task, agent_list_tasks, agent_update_task, agent_close_task
  router.py         # POST/GET/PATCH/DELETE /tasks
```

Per the touch-time rule, register `TaskDocument` in the `import-linter` contract; add `tasks/router.py`, `tasks/dto.py`, `tasks/domain.py` to the source-modules list.

### 3. Aggregated activity feed (cross-cutting read)

Activity feed is read-only across multiple sources (agent sessions, tool calls, Instinct events). Options:

- **A:** new lightweight `activity/` read-only router that subscribes to in-process bus events and materializes a small recent-N buffer per workspace. No persistence beyond the buffer.
- **B:** extend existing `chat/` or `sessions/` with `GET /activity?workspace=ctx` that joins across.

Recommend **A** — purpose-built, doesn't entangle the activity surface with chat/session lifecycle. Buffer size 200 events per workspace, TTL 1 hour, no DB writes. SSE channel `activity.recorded` fans out new events as they arrive.

### 4. Eventual Mission Control config (per-workspace pane layout)

Once panes become Ripple-rendered (PR 1 land), workspace admins can pick which panes show and in what order. Persist as `workspace.mission_control_layout: list[PaneSpec]`. Defer until v1.5.

## Mock-first development

The captain explicitly asked: *"first build the UI with mock easily replaceable with backend calls."* Right call — the shell shape needs validation before the backend bakes in.

**Mock layer design** (paw-enterprise side):

```ts
// src/lib/core/mission-control/api.ts
// Typed API client. Mock impl today; flip the imports to real `http()`
// when backend endpoints land.

import { mockApi } from './api.mock';
import { realApi } from './api.real';

const USE_MOCK = import.meta.env.VITE_MISSION_CONTROL_MOCK !== 'false';

export const missionControlApi = USE_MOCK ? mockApi : realApi;
```

Both impls share the same TypeScript interface, defined once:

```ts
export interface MissionControlApi {
  listNudges(opts?: { status?: NudgeStatus }): Promise<Nudge[]>;
  approveNudge(id: string, note?: string): Promise<void>;
  rejectNudge(id: string, reason: string): Promise<void>;
  listAgentActivity(opts?: { limit?: number }): Promise<ActivityEvent[]>;
  listPawprints(opts?: { limit?: number }): Promise<PawprintEntry[]>;
  outcomesSummary(window: '1h' | '24h' | '7d'): Promise<OutcomeSummary>;
  listTasks(opts?: { assignee?: string; status?: TaskStatus }): Promise<Task[]>;
}
```

The mock impl returns realistic fake data (with timestamps, agent names matching configured agents, plausible Nudge descriptions). It also emits fake `RealtimeClient` events on a timer so the live-update path can be tested before real SSE wires up. **Flipping from mock to real is one env var.**

## Phasing

| Phase | Window | What ships | Gate |
|---|---|---|---|
| **v0 — mock UI** | now → 1 week | `/mission-control` route in paw-enterprise. Shell + The Tray + Pawprints + outcomes + activity panes wired to mock API. Existing `ApprovalsPanel.svelte` and `AuditLogPanel.svelte` reused in the new shell. | Captain demo's the screen, signs off on layout. |
| **v0.5 — real wiring for existing endpoints** | 1 → 3 weeks | Flip `VITE_MISSION_CONTROL_MOCK=false`. The Tray + Pockets + Pawprints panes hit real `/api/v1/instinct/*` and `/api/v1/audit/*` endpoints (these exist today). Outcomes + tasks panes stay mocked. | First real Nudge approved end-to-end through Mission Control on a dev workspace. |
| **v1 — new backend surfaces** | 3 → 8 weeks | Outcomes aggregation endpoint, activity feed buffer + SSE, tasks entity. ee/cloud 4-file shape, lint contract, tests. All panes go live against real data. | First Nerve Systems client demoed against Mission Control. |
| **v1.5 — Ripple-rendered panes** | After PR 1 (granular UI-tree mutation ops) lands | Panes migrate from bespoke Svelte to Ripple specs. Per-workspace layout configurability. Per-client pane variants for Nerve Systems engagements. | Shawn's Mission Control has different default panes than another client's, served from the same shell. |

**v0 unblocks demos this week.** v1 unblocks first paying Nerve Systems engagement at full capability. v1.5 unblocks productization.

## Out of scope (this doc)

These come after v1 ships:

- **Multi-window Mission Control** (operator monitors multiple workspaces side-by-side) — Pro/Enterprise tier feature.
- **Operator console for Nerve Systems team** (cross-client view across all our deployments). Separate surface, same primitives. Wait until 3+ live clients.
- **Mission Control as a Pocket type** for workspace-scoped variants — defer until Ripple-rendered panes prove out.
- **Mobile responsive Mission Control** — desktop-only for v0/v1. Mobile gets channel-adapter surface (Slack/Telegram inline approval), not a full Mission Control.
- **Webhook / API surface for external mission-control integrations** — defer until customer pull.

## Open questions

These need a decision before v0.5 work starts. Recommend a 20-minute sync.

1. **Endpoint naming.** Is the surface `/api/v1/instinct/nudges` (vocabulary-aligned) or `/api/v1/instinct/proposals` (more generic)? Recommend **nudges** — matches the captain's prior strategy doc, distinctive, won't collide with anything.
2. **Activity feed persistence.** In-memory buffer only (cheap, lossy on restart) or persist to a capped collection (durable, more infra)? Recommend in-memory buffer for v1, persist via Pawprints (which we already write) — operator can scroll back through Pawprints if they need older activity.
3. **Tasks entity scope.** Should a Task be a first-class entity, or just a Nudge-with-status="awaiting-human" + assignee? Recommend **first-class entity** — Tasks have different lifecycle (long-running, status updates, comments) than Nudges (one-shot decision).
4. **Mock layer location.** `src/lib/core/mission-control/api.mock.ts` (lives with the real impl) or `src/lib/mocks/`? Recommend **alongside real impl** — keeps the interface contract co-located.

---

## Cross-references

- [Vision doc — Pocket specialist + Ripple mutation](./2026-05-pocket-specialist-and-ripple-mutation.md) — the architectural foundation v1.5 builds on.
- [Implementation handover — Ripple mutation](./2026-05-ripple-mutation-handover.md) — PR 1 (granular UI ops) is the prerequisite for v1.5.
- `paw-enterprise/docs/enterprise/PAW-OS-STRATEGY.md` — the captain's prior naming map (Tray, Pawprints, Nudges, Instinct).
- `docs/roadmap/future-upgrades/engineering-patterns/adoption-plan.md` — ee/cloud 4-file shape and import-linter contract this work conforms to.
- `docs/roadmap/future-upgrades/engineering-patterns/paw-enterprise.md` — frontend API chokepoint at `src/lib/core/shared/http.ts`, ESLint rule mock layer must respect.
- `paw-enterprise/src/lib/components/os/ApprovalsPanel.svelte` — existing component to reuse as The Tray pane.
- `paw-enterprise/src/lib/components/os/AuditLogPanel.svelte` — existing component to reuse as Pawprints pane.
- `paw-enterprise/src/lib/core/realtime/client.ts` — SSE channel the panes subscribe to for live updates.
