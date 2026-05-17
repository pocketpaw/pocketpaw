<!--
  2026-05-wire-activity-audit-knowledge.md — Plan doc for finishing the
  cloud wiring on the workspace-level Activity / Audit / Knowledge
  routes in paw-enterprise. Created 2026-05-17 after the captain
  reviewed those three routes and asked "are these just mock?"
  Companion to the spec at 2026-05-mission-control.md and the
  audit-backend doc alongside it. Picked up by a fresh session.
-->

# Wiring Plan — Activity / Audit / Knowledge (workspace routes)

*Status: plan. No code shipped yet. Next session picks up from PR A.*

## Why this exists

The captain spot-checked the `/activity`, `/audit`, and `/knowledge` routes after the Mission Control + planner work landed and read "activity and knowledge look mostly mock." This doc captures what's actually true (one of them is real, one looks mock for a real-but-hidden reason, one has a real security gap) and the phased plan to close the gaps without a sprawling refactor.

## Verified state — 2026-05-17

| Route | Frontend calls | Backend | Tenancy enforcement | Reality |
|---|---|---|---|---|
| **`/audit`** | `listRuntimeAudit()` → `GET /api/v1/runtime/audit` | `src/pocketpaw/audit/runtime_router.py` (LOCAL runtime, NOT `ee/cloud/`) | `workspace_id` is an **optional query param** — caller can pass anything or omit it | Real backend, **tenancy-leaky** |
| **`/activity`** | Same `listRuntimeAudit()` via `activityStore` | Same as above | Same as above | Real backend, but **MOCK fallback** hides empty state — looks mock when workspace has no real audit history |
| **`/knowledge`** | `listWorkspaceArticles()` → `GET /api/v1/knowledge/articles` | `ee/cloud/kb/knowledge_router.py` (CLOUD, 4-file shape) | `kb.read` action required + workspace pinned via `current_workspace_id` dep | **Real and properly scoped** ✓ |

### Where the "looks mock" perception comes from

- **Activity**: `paw-enterprise/src/lib/components/os/activity-store.svelte.ts:35` has `if (mapped.length > 0) this.items = mapped;`. On a fresh workspace with no audit rows, the bootstrap `MOCK_ACTIVITIES` (30 fake items) stays visible forever. The store reports success — there's just nothing to replace the seed with. Operator reads it as mock.
- **Knowledge**: Real backend, properly scoped. Looks empty because the workspace is empty. That's *correct* behavior, not mock — but the empty state could be more inviting.
- **Audit**: Reads from the LOCAL runtime audit router that accepts a `workspace_id` query param without authority checks. A signed-in user could pass `?workspace_id=<other-tenant>` and get their data. **Real security gap.**

## The plan — four PRs, ascending difficulty

### PR A — Drop activity's mock fallback (S, ~10 LOC, ship first)

**File:** `paw-enterprise/src/lib/components/os/activity-store.svelte.ts`

**Changes:**
- Bootstrap with `[]` not `MOCK_ACTIVITIES`
- Drop the `if (mapped.length > 0)` guard — assign empty array on empty response
- Keep `MOCK_ACTIVITIES` exported so explicit demo paths (Storybook, design QA) can opt in via a `?demo=true` URL flag

**Frontend copy:**
- `/activity` empty state: "No activity yet — agents will populate this as they work."
- Home `ActivityRiver` empty state: same line

**Tests:**
- Update existing vitest for activity-store: `refresh()` with empty response should set `items = []`, not preserve the seed
- New test: subscriber that mounts with no prior data sees empty list, not mock

**Why first:** No backend change. Visible bug — fake activity rows mislead operators. Ten minutes of work.

### PR B — Move audit + activity to a workspace-scoped cloud endpoint (M, ~250 LOC backend + ~30 frontend)

**Decision needed before starting** (see Open Questions below): canonical audit source.

#### Backend (pocketpaw, target branch: `ee`)

**Option B1 — New `ee/cloud/audit/` 4-file entity (recommended)**
- `domain.py` — `AuditEntry` value object, frozen, `workspace_id` required at construction
- `dto.py` — `ListAuditRequest` (q, category, limit, no workspace_id field), `AuditEntryResponse`
- `service.py` — `agent_list_audit(ctx, body)` reads `ctx.workspace_id`, NEVER trusts a body field; wraps the existing `src/pocketpaw/audit/store.py` for the FTS + filter implementation but enforces workspace at the chokepoint
- `router.py` — `GET /api/v1/audit` (workspace-scoped); explicitly rejects a `workspace_id` query param OR requires it to match `ctx.workspace_id`
- Same response envelope as the legacy runtime endpoint so the frontend swap is one method
- import-linter contract entry

**Option B2 — Reuse Instinct's Pawprints**
- Skip the new entity; point the frontend at `GET /api/v1/instinct/audit` (already cloud-scoped via the pocket-membership chain)
- Narrower (only audit-able actions go through Instinct) but tenant-safe today

**Backward compat:**
- Legacy `/api/v1/runtime/audit` stays live for the LOCAL pocketpaw OSS path (single-user, no workspace) for one release; add a Deprecation header pointing at `/api/v1/audit`

**Tests:** 10-12
- Tenancy isolation (cross-workspace request returns own data, not the queried one)
- Query-param `workspace_id` is rejected (or coerced)
- FTS + category filters still work
- Envelope shape matches the legacy endpoint

#### Frontend (paw-enterprise, target branch: `dev`)

- `src/lib/core/runtime/api.ts` — new `listWorkspaceAudit()` that hits `/api/v1/audit`. Keep `listRuntimeAudit()` for OSS-runtime callers (don't break the existing route).
- `/audit` route — swap from `listRuntimeAudit` to `listWorkspaceAudit`
- `activity-store.svelte.ts` — swap to `listWorkspaceAudit`
- 2-3 vitest updates for the new method's empty/error paths

### PR C — Knowledge empty-state polish (XS, optional, ~30 LOC)

Knowledge is real and tenant-safe. Two soft improvements only if QA confirms the empty state reads as broken:

- Replace bare "No articles" with a real CTA: "Upload a document → `/files`" or "Seed from a URL"
- Surface the agent-filter chip even when the workspace has zero articles, so the affordance is discoverable
- One vitest for the empty-state CTA renders

Defer until a real user complains about it. The route is genuinely working.

### PR D — Live updates via SSE for audit + activity (M, optional, ~150 LOC)

Currently both poll every 30s via `activityStore.subscribe()`. The cloud realtime bus already emits per-row events the dashboard could subscribe to.

**Backend:**
- Emit `runtime.audit.recorded` from the audit store on every insert (one line)
- Frontend `RealtimeClient` adds `subscribeAudit(handler)` typed on the new event

**Frontend:**
- Both routes use `subscribeAudit()` for incremental updates; polling drops to every-5-min as a safety net
- Empty-state still uses the initial fetch

**Why optional:** Polling is "fine" for an operator surface. Real-time becomes more valuable when audit becomes a high-volume feed (10+ rows/minute), which it isn't today.

## Priority + sequencing

| PR | Why | Ship now? |
|---|---|---|
| A | Visible bug — fake activity rows lie to the user | yes, first |
| B | Real security concern — cross-tenant audit reads possible | this sprint |
| C | Nice-to-have, not blocking | wait for user feedback |
| D | Real-time polish | after B lands |

## Open Questions

### Q1 — Audit canonical source for the cloud route

The cloud has two audit feeds today:

- **`/api/v1/runtime/audit`** — broad, FTS, all event categories (decisions, tools, data, config, security); what `/audit` currently reads
- **`/api/v1/instinct/audit`** — Pawprints from the Instinct approval pipeline only, workspace-scoped via Instinct's pocket-membership chain; what Mission Control's Pawprints section reads

**For the workspace `/audit` route**, which is canonical?

- *Pawprints-only* (B2): simpler, already tenant-safe, smaller code. But narrower — config/data ops outside Instinct's approval chain are invisible.
- *New `ee/cloud/audit/` wrapper* (B1): fuller coverage, but more code + a touch-time entity to maintain.

**Recommendation:** B1 if compliance / SOC 2 cares about full event coverage; B2 if MVP audit-trail-for-customers is the primary use case. Captain decides before PR B starts.

### Q2 — Should Activity + Audit share a feed surface?

Activity (`/activity`) and Audit (`/audit`) read from the same backend today. They differ only in default category filters + presentation. Question: should they be one route with view toggles ("compact ticker" vs "detailed table"), or stay as two distinct routes?

Defer until B lands. If both use the same wrapper, collapsing them later is trivial.

### Q3 — Local `MOCK_ACTIVITIES` future

After PR A, MOCK_ACTIVITIES becomes opt-in for design / demo work. Options for its location:

- Keep it in `activity-data.ts` as an exported const
- Move to a `__fixtures__/` directory (cleaner separation)
- Delete it and let Storybook generate fixtures on demand

**Recommendation:** keep as-is for now; the demo flag is rarely used and the cost is one unused const.

## Out of scope

- Cycles burnup chart redesign (separate concern, planner work)
- Real-time WebSocket migration for the broader app (audit/activity SSE is a narrow case)
- Auth chain hardening (covered by issue #1117)
- Knowledge ingest pipeline (uploads + KB compiler — separate work)

## Next-session checklist (start from here)

1. Read this doc top-to-bottom (5 min)
2. Decide Q1 — pick B1 or B2 (5 min)
3. Ship PR A first — no backend dependency, immediate visible improvement (~30 min)
4. Then PR B with the Q1 decision applied (1-2 sessions of focused work)
5. Defer PR C and PR D unless something demands them

## Cross-references

- `pocketpaw/docs/internal/2026-05-mission-control.md` — Mission Control spec (Pawprints section discusses the Instinct audit source)
- `pocketpaw/docs/internal/2026-05-mission-control-backend-audit.md` — backend audit doc (pre-implementation; stale for several primitives but still useful for endpoint inventory)
- `paw-enterprise/src/lib/components/os/activity-store.svelte.ts` — the store with the mock-fallback bug
- `pocketpaw/src/pocketpaw/audit/runtime_router.py` — the LOCAL audit endpoint that needs cloud wrapping
- `pocketpaw/ee/cloud/kb/knowledge_router.py` — canonical example of a cloud-scoped read endpoint with workspace tenancy enforcement (mirror this shape for the new audit entity)
- `pocketpaw/CLAUDE.md` § ee/cloud Code Rules — the 4-file shape the new audit entity must follow
