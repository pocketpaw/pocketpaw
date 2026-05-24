# P6 — Edit coverage report (pocketpaw-pocket-specialist skill + merge endpoint)

**Branch:** `feat/pocket-edit-skill-merge` (HEAD `b2cb4490`) in the worktree
`pocketpaw/.claude/worktrees/agent-ad01f90d44321e5bb`. Live testing 2026-05-24
21:25–21:50 IST against pocket `6a1317909d38ba3cc87de0c2` via the
paw-enterprise UI on the captain's running paw-enterprise instance, talking
to a freshly-started pocketpaw backend on `:8888` with
`POCKETPAW_POCKET_SPECIALIST_USE_SKILL=true` and `POCKETPAW_POCKET_ROUTER_ENABLED=false`.

## Coverage matrix

| # | Scenario | Old 17-tool surface this replaces | New path verified |
|---|---|---|---|
| P5b | Change page-header subtitle | `set_node_prop(node_id, "subtitle", "...")` (Layer 2) | ✓ POST /spec/merge → 200, only subtitle changed |
| P6.1 | Mark Globex Corp as won | `set_state("cards[0].status", "won")` (Layer 1 data) | ✓ POST /spec/merge → 200, only that card's status changed |
| P6.2 | Rename kanban column "Lead" to "Prospects" | `set_prop_array_item(kanban_id, "columns", {by_field:"id", equals:"lead"}, {title:"Prospects"})` (Layer 2.5) | ✓ POST /spec/merge → 200, only column[0].title changed, id "lead" preserved so cards still route |
| P6.3 | Add "Total Pipeline Value" stat widget between form-row and kanban | `add_node(root_id, {type:"stat",...}, after_id="<form>")` (Layer 3 structural) | ✓ POST /spec/merge → 200, new `n_stat0001` inserted at index 2, all existing nodes preserved by id, agent computed $1,082,000 sum |
| P6.4 | Remove the Add-deal form row entirely | `remove_node(form_row_id)` (Layer 3 structural) | ✓ POST /spec/merge → 200, form row gone, other 3 children preserved by id, 8 cards untouched |

Five mutations stacked on the same pocket across the session. Every prior
edit was preserved by the next one (the merge primitive's by-id semantics
worked exactly as designed).

## Final state (after all 5 edits, screenshotted at 21:50)

```
Sales Pipeline
Q3 2026 · Live Pipeline View         ← P5b (was "Q2 2026 · Deal Tracker")

Total Pipeline Value                  ← P6.3 (new stat widget)
$1,082,000

[ Prospects ][ Qualified ][ Proposal ][ Won ]
  Pinnacle    Stark        Helios       Globex Corp ← P6.1 (was lead)
              Axiom        Crestview    NovaTech
                                        Meridian
                                                       ← P6.4 (form row gone)
```

UI children: `[page-header, stat, kanban]` (form-row `n_rr44f8p8` removed).

## Log evidence — the new path engaged every time

Every test emitted **exactly one** `skill-pointer kit returned (USE_SKILL=true)`
followed by **exactly one** `POST /api/v1/pockets/<id>/spec/merge HTTP/1.1 200 OK`.
No `kind_for_op` errors, no `agent-mode op '<op>' raised`, no per-op pydantic
validation failures. The five POSTs are at log lines 202, 427, 492, 647, 734.

Server-side `validate_against_catalog_strict` + `validate_action_wiring_strict`
ran on every merge and returned no warnings.

## What's covered vs what's not

**Covered** (the dominant 80% of edit traffic):
- Layer 1 state mutations (set_state equivalent via `merge: { state: { ... } }`)
- Layer 2 single-prop changes (re-emit one node with one prop changed)
- Layer 2.5 prop-array-item edits (re-emit one node with a modified prop-array)
- Layer 3 add_node (re-emit parent's children array with the new id inserted)
- Layer 3 remove_node (re-emit parent's children array with the id omitted)

**Not yet covered** (lower-frequency, defer to PR-2):
- `move_node` — same shape as remove+add; not tested in this session because the
  current layout doesn't have a sensible move target. The merge() helper already
  handles re-stating two parent nodes' children arrays atomically.
- `set_source` / `set_action` — bindings to external HTTP APIs. The agent would
  emit a partial with `state.sources` / `state.actions` populated. Validated
  by the action-wiring gate but not exercised live yet.
- Out-of-order patch where a child id appears in patch.ui but not reachable
  from any parent. The merge helper's `tests/cloud/pockets/test_merge_spec.py`
  test `test_orphan_id_in_patch_is_reported_and_dropped` already pins this
  behaviour (auto-orphan + warn, not error).

**Not in scope for this PR** (separate workstreams):
- CREATE path (`run_specialist`) still uses `persist_pocket_for_agent` and the
  old subagent/agent-mode draft kit. Same skill+merge swap applies — captain
  flagged this as a follow-up workstream.
- `pocket_router` deterministic Tier 0/1 short-circuit. Was disabled during
  testing because it bypasses the specialist entirely. Production needs both
  paths to coexist — either route the router's deterministic ops through the
  new merge endpoint too, or leave it as a parallel cheap path. Documented
  in the PR body as known follow-up.

## Caveats the PR body needs to call out

1. **Auth bypass is loopback-only.** The `X-PocketPaw-Internal: true` header
   gate is dev-grade — fine for the MVP, needs a short-lived JWT in PR-2.
2. **Old path coexists, not deleted.** The 17 LangChain tools, `_apply_ops`,
   and the `_for_agent` helpers all still ship. Removal happens in a follow-up
   once the new path proves itself in production for a review cycle.
3. **pocket_router needs a decision.** Disabled during testing; production
   needs explicit policy on whether to route through it or always escalate.

## Recommendation

**Ship the PR.** The win is concrete and reversible:

- Coexists with the 17-tool path via `POCKETPAW_POCKET_SPECIALIST_USE_SKILL=true`
  (defaults off — zero risk to current behaviour).
- 5 cumulative edits proven live, covering every layer of the old surface.
- 7 unit tests pass (`tests/cloud/pockets/test_merge_spec.py`).
- 143 cloud tests pass (no regressions in the broader pocket service).
- 57 specialist tests pass.
- Same `validate_against_manifest` / catalog / action-wiring gates run as
  before — server-side correctness floor unchanged.

PR target: `dev`. Branch: `feat/pocket-edit-skill-merge`. Recommended
title: `feat(pocket-specialist): skill + merge endpoint as an MVP alternative
to the 17-tool LangChain edit surface`. Body covers the empirical evidence
from P5b + P6.1-P6.4 plus the three caveats above.

Follow-up PRs queued in soul memory at importance 10:
- PR-2: tighten auth (short-lived JWT instead of loopback header bypass).
- PR-3: apply the same skill+merge pattern to the CREATE path.
- PR-4: pattern generalization — file creation, knowledge article create,
  fabric link create, instinct rule create. Per-PR scope, same shape.
- PR-N: delete the 17-tool LangChain surface once the new path holds.
