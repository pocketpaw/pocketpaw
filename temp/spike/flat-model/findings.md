# RFC 06 Position 1 — flat-namespace component model spike

**Branch:** `spike/flat-component-model` (worktree `pp-flat-model-spike`).
**Status:** spike under `temp/spike/` — no PR, captain decides go/no-go after reading this.
**Date:** 2026-05-24.

## 1. Recap of the bet

Ripple's `UINode` is a recursive `{type, props, children}` tree today. Mutation goes through eight ops in `spec-mutator.ts` (`node_added`, `node_replaced`, `node_moved`, `node_removed`, `node_prop_set`, plus three prop-array ops) — all keyed by stable `n_xxxxxxxx` ids, all backed by recursive `findById`/`findParent` tree-walks. RFC 06 Position 1 asks: if we flatten the representation into `{root: id, components: {id -> node}}` and adopt OpenUI Lang's *merge-by-name* semantics (re-emit a node by id, the parser merges; absent ids are kept; orphans get GC'd from `root`), do the eight ops collapse to one rule, and is the renderer change tractable? This spike answers that on real stored pocket specs.

## 2. Round-trip

Pulled four real specs:

| Fixture | Nodes | Source |
|---|---|---|
| `team-activity.spec.json` | 36 | Home pocket's "Team Activity" widget's per-widget spec (no ids — widget-level specs don't run through `normalize_ripple_spec.ensure_ids`) |
| `business-dashboard.spec.json` | 25 | `Business Dashboard` pocket's top-level `rippleSpec` (ids stamped) |
| `sprint-dashboard.spec.json` | 27 | `Sprint 24 Dashboard` pocket's `rippleSpec` (ids stamped) |
| `component-showcase.spec.json` | 83 | `🧪 UI Component Showcase` pocket's `rippleSpec` (ids stamped, biggest tree on the box) |

`unflatten(flatten(x))` is structurally lossless on all four (15 bun tests pass). On the id-stamped fixtures (the production shape) the round-trip is bit-for-bit identical. On the unstamped team-activity fixture the transform mints ids, which is exactly the same thing `spec-id.ts:ensureNodeIds` does today — i.e. flattening incidentally completes id-stamping, which the normalizer already runs on persist anyway.

**Edge cases the transform handled cleanly:** none of the four fixtures actually exercise `else_children`, `slot`, `each.items` (node-level), or `if.condition` — the production pocket corpus is dominated by `flex` / `grid` / leaf-widget layouts. The transform supports all of those by symmetry (same code path as `children`), but **we did not stress them on real data.** That gap is a known follow-up if a real PR moves forward; sample specs from the test suite (`NodeRenderer.slots.test.ts`, `spec-mutator.test.ts`) should be added to the fixture corpus before declaring full coverage.

## 3. Token / byte-size delta

| Fixture | Nodes | Nested (raw) | Nested (id-stamped) | Flat | Flat vs stamped |
|---|---|---|---|---|---|
| team-activity | 36 | 3,225B | 3,873B | 4,826B | **+24.6%** |
| business-dashboard | 25 | 3,885B | 3,885B | 4,552B | **+17.2%** |
| sprint-dashboard | 27 | 3,911B | 3,911B | 4,630B | **+18.4%** |
| component-showcase | 83 | 11,619B | 11,619B | 13,794B | **+18.7%** |

The fair comparison is the middle two columns — the flat form always carries an id per node, so the apples-to-apples nested form is the id-stamped one (which production already emits via the normalizer). Flat is **~17–25% larger on the wire** than id-stamped nested. The overhead comes from (a) the literal `"components":{` envelope, (b) the per-node `"id":"n_xxxxxxxx"` being a JSON object key *and* a value in the parent's `children` array — id strings appear twice per node, and (c) opening/closing braces around every node instead of nesting brace-saving via positional containment. This is the cost of addressability.

**Per-mutation patch sizes** (one node prop change on team-activity, one subtree swap):

| Mutation | Nested op (today) | Flat patch (OpenUI shape) |
|---|---|---|
| `text` prop change on a leaf | 81B | 136B |
| Subtree replace (swap parent's first child) | 116B | 229B |

Flat patches are **~70-100% larger per mutation** because the partial spec includes the parent node verbatim when re-stating its `children` array (vs the nested op shape which carries only the operation discriminant + minimal operands). The flat form does *not* win on patch size for the small mutations we benchmarked. It wins on agent-side authoring (re-emit one named node, no op shape to memorize) and on uniform mutation language (one rule, not eight).

## 4. Renderer change

| File | LOC (non-blank, non-comment) |
|---|---|
| `NodeRenderer.svelte` (current) | 284 |
| `FlatRenderer.svelte` (spike) | 36 |

The 36 vs 284 gap is *not* the real delta — FlatRenderer is a stripped demo that skips bind/expression-resolution/event-handler-wiring/slot-bucket logic. That machinery is orthogonal to flat-vs-nested (it lives at the *node*, not the *child-list*). In a real port, the existing NodeRenderer would lose its child-recursion blocks and replace each `{#each node.children as child}` with `{#each childIds as id} <Self componentId={id} spec={spec}>`. That is a **~30 LOC change to the recursion machinery**, leaving the other ~250 LOC of bind/event/slot code essentially intact. The renderer change is tractable; it's a focused diff, not a rewrite.

## 5. Mutator collapse

| Surface | LOC | Notes |
|---|---|---|
| Current: `spec-mutator.ts` total | 312 | 9 op functions + dispatch + tree-walk helpers |
| `spec-mutator.ts` op functions only (`apply*`) | 193 | The 9 ops + dispatch (`applyOp`) |
| `spec-mutator.ts` tree-walk helpers (`findById`, `findParent`) | 29 | Recursive walks that locate a node by id |
| Spike: `merge` in `flatten.ts` | **18** | One function, no helpers needed |

The eight node ops + the dispatch switch + the recursive locators (~222 LOC of *necessary* mutation machinery) collapse to a single 18-LOC `merge(base, patch)` that does Object.assign-style replacement keyed by id. The recursive `findById`/`findParent` (29 LOC) disappear entirely — a flat namespace makes lookup O(1) by definition. **Net structural reduction: ~10x on the mutator surface area.**

Prop-array item ops (`applySetPropArrayItem` etc., 100+ LOC for chart.data / table.rows / feed.items surgical writes) are *not* collapsed by the flat model — they mutate inside `node.props.<array>`, not the tree. They remain as-is in either world. So the honest collapse is on the 5 node-structure ops + the 2 helpers + the dispatch, not on the full file.

## 6. Stored-spec migration

The 31 pockets on this box all carry either an empty `rippleSpec` or one that passes through `flatten()` cleanly (3 of 4 fixtures with stamped ids round-trip bit-for-bit; the unstamped widget-level spec round-trips after id-minting). **A one-shot DB migration is feasible.** The shape of the work mirrors the existing `normalize_ripple_spec` `_lift_*` passes: walk every `rippleSpec` blob, call `flatten()`, write back. The transform is pure JS, but the Python side would need a port — the `_stamp_node_ids` infrastructure in `pocketpaw_ee/cloud/pockets/spec_ops.py` is the precedent; a `_flatten_ui_spec` next to it is the natural shape.

A **back-compat read path** (flatten on read, store nested forever) is also viable and lower-risk for the rollout phase. The `normalizer.ts` pattern in ripple already supports two representations on the wire (UISpec vs UniversalSpec); adding "flat or nested" as a third axis is the same shape of problem. Probably *both*: read-path flattening during a 1-2 PR transition, then a one-shot migration to remove the dual-representation tax.

**No fixture failed the transform.** The riskier shapes (`else_children`, slotted children, control-flow nodes) aren't in the corpus we have here. A pre-migration audit should grep `rippleSpec` blobs across all workspaces for `else_children` / `"slot"` / `"each"` / `"if"` types before committing to the one-shot path.

## 7. The orphan-node question

When a patch re-states a parent's child list, dropping a child id, the dropped subtree's nodes become unreachable from `root` but stay in `components`. RFC 06 reads OpenUI as choosing **(a) keep them in the map** (the patch-is-a-spec elegance is preserved; explicit deletion would defeat it). Confirmed in the RFC text:

> Explicit deletion → remove a statement from the `root` children list, it becomes unreachable and gets garbage-collected

OpenUI's semantics: dropping from `root` *is* the deletion, and the unreachable nodes are GC-target candidates — but the wire patch contains *no* explicit "delete this id" — that property is entirely structural.

**My read for Ripple:** option **(a + opportunistic GC at boundaries)**. Keep orphans by default — the merge stays pure id-replacement, the wire is minimal, undo/redo is free (the dropped subtree is still in `components` for a redo to re-reachable it). Run `gcOrphans()` at well-defined boundaries: (1) on the server before persisting (avoid storing dead nodes long-term), (2) after a session of edits closes (compact the in-memory state), (3) never inside the merge step itself. This matches OpenUI's "unreachable = GC candidate, not protocol-level deletion."

Option (b) (GC after every merge) is worse — it bloats the merge with a reachability pass per patch and kills the cheap undo. Option (c) (explicit deletions on the wire) defeats the entire OpenUI elegance; do not adopt.

`gcOrphans()` is implemented and tested in the spike (3 LOC for the reachability walk, 7 LOC total including the new-spec emission).

## 8. Recommendation

**Green-light a real PR series, with caveats.**

The architectural numbers are clean:

- Round-trip works on real specs.
- Mutator collapses ~10x in structural surface area.
- Renderer change is a focused ~30 LOC diff inside the existing 284-LOC NodeRenderer.
- A one-shot DB migration is feasible; a read-path back-compat layer de-risks rollout.

The wire-size delta (+17–25% per spec, +70–100% per patch) is the genuine cost. It's not nothing — at the largest fixture (component-showcase, 83 nodes) it's 2.2KB of overhead — but it's well within budget for any future LLM token cost. And the OpenUI authoring argument is the real prize: re-emit one named node, get a patch. That's the cleanest agent-authoring contract any of the surveyed systems shipped.

Caveats:

1. **The fixture corpus does not exercise corner cases.** None of the 4 fixtures use `else_children`, `slot`, node-level `each.items`, or `if.condition`. A real PR series must add fixtures covering those before the migration ships. The transform supports them by symmetry, but production data did not stress them in this spike.

2. **Patch size is bigger, not smaller.** OpenUI Lang's "~85% fewer tokens than full regeneration" claim is *vs full regeneration*, not vs the 8-op patch shape Ripple already has. For the targeted single-node mutations our `spec-mutator` does today, the flat patch is materially larger. The win is in the mutation *language* (one rule), not the mutation *size*.

3. **Prop-array item ops survive the migration unchanged.** `applySetPropArrayItem` and friends are not absorbed by merge-by-name — they mutate inside `node.props`, not the tree. The 100+ LOC of prop-array ops stay; the collapse is only on the 5 node-structure ops + 2 tree-walk helpers + dispatch.

4. **Slot semantics need a decision.** Ripple's slots come from a child's `slot` field today (carried verbatim through `flatten`/`unflatten`). The captain's open question (per-slot id lists on the parent vs `slot` on the child) defaults to "child carries `slot`" in this spike, which is the simpler choice and the one that matches A2UI. If the captain prefers per-slot lists on the parent (which makes slot reassignment a parent-only patch), that's a separate, larger schema decision — but it can be deferred until after the flat baseline lands.

If green-lit, the PR sequence is plausibly:

- **PR-1 (M):** add `flatten`/`unflatten`/`merge`/`gcOrphans` to ripple alongside the existing nested types; expose both representations from `UISpec` exports. No renderer change yet; no migration. (~3 files, ~250 LOC + tests.)
- **PR-2 (L):** port NodeRenderer to take a FlatSpec + componentId. Add fallback that wraps a nested UISpec in `flatten()` on entry so old call sites keep working. (~1 file, ~30 LOC of recursion change + a wrapper.)
- **PR-3 (L):** introduce the merge-by-name SSE event shape on the cloud side; deprecate (but don't remove) the 8 op events. Update ripple-normalizer to flatten on persist. (~3 files cloud-side, ~150 LOC + tests + migration grep.)
- **PR-4 (M):** one-shot DB migration to convert stored `rippleSpec` blobs. Idempotent — re-running on already-flat specs is a no-op.

Total: ~4 PRs, sized M/L. Captain-time per PR review is the dominant cost; agent-implementation time is plausibly 4-8 agent-hours total.

The spike says yes — but it's a deliberate yes, with the corner-case fixtures and the slot-semantics question called out as gates before PR-1 ships.
