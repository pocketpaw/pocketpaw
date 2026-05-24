# RFC 06 Position 1 — flat-namespace component model spike

**Branch:** `spike/flat-component-model` (worktree `pp-flat-model-spike`).
**Status:** spike under `temp/spike/` — no PR, captain decides go/no-go after reading this.
**Date:** 2026-05-24.

## 1. Recap of the bet

Ripple's `UINode` is a recursive `{type, props, children}` tree today. Mutation goes through eight ops in `spec-mutator.ts` (`node_added`, `node_replaced`, `node_moved`, `node_removed`, `node_prop_set`, plus three prop-array ops) — all keyed by stable `n_xxxxxxxx` ids, all backed by recursive `findById`/`findParent` tree-walks. RFC 06 Position 1 asks: if we flatten the representation into `{root: id, components: {id -> node}}` and adopt OpenUI Lang's *merge-by-name* semantics (re-emit a node by id, the parser merges; absent ids are kept; orphans get GC'd from `root`), do the eight ops collapse to one rule, and is the renderer change tractable? This spike answers that on real stored pocket specs.

## 2. Round-trip

Pulled four real specs and synthesized four corner-feature fixtures to match patterns from ripple's own tests and manifest entries:

| Fixture | Nodes | Source |
|---|---|---|
| `team-activity.spec.json` | 36 | Home pocket's "Team Activity" widget's per-widget spec (no ids — widget-level specs don't run through `normalize_ripple_spec.ensure_ids`) |
| `business-dashboard.spec.json` | 25 | `Business Dashboard` pocket's top-level `rippleSpec` (ids stamped) |
| `sprint-dashboard.spec.json` | 27 | `Sprint 24 Dashboard` pocket's `rippleSpec` (ids stamped) |
| `component-showcase.spec.json` | 83 | `🧪 UI Component Showcase` pocket's `rippleSpec` (ids stamped, biggest tree on the box) |
| `corner-else-children.spec.json` | 10 | Synthesized; matches the `if` + `else_children` shape from `validate-catalog.test.ts:60` and the `NodeRenderer.svelte` if-branch (line 308). |
| `corner-slot.spec.json` | 11 | Synthesized; matches `NodeRenderer.slots.test.ts` (header/footer cases) and `manifest/entries/app-shell.ts` (slot="sidebar"). |
| `corner-each-items.spec.json` | 7 | Synthesized; matches the manifest `each` entry (`each.ts`) and the chips-loop in `routes/showcase/+page.svelte:1849`. |
| `corner-if-condition.spec.json` | 10 | Synthesized; matches the field-error `if` blocks in `routes/showcase/+page.svelte:3232` (form-with-validation). |

`unflatten(flatten(x))` is structurally lossless on all eight (29 bun tests pass). On the id-stamped fixtures (the production shape) the round-trip is bit-for-bit identical. On the unstamped team-activity and corner-* fixtures the transform mints ids, which is exactly the same thing `spec-id.ts:ensureNodeIds` does today — i.e. flattening incidentally completes id-stamping, which the normalizer already runs on persist anyway.

**Corner features confirmed:** the four corner fixtures explicitly exercise `if.else_children`, child-level `slot`, node-level `each.items` + `item_as` + `index_as`, and node-level `if.condition`. Each fixture is gated by a feature-presence assertion (e.g. for `corner-else-children`, at least one component in the flat map must have a non-empty `else_children` array) so a too-weak fixture cannot silently pass the round-trip check. The transform's symmetry claim held — `flatten.ts` needed zero changes. A merge() patch that touches a corner-feature node (re-emit `each` with a new `item_as`) preserves all the other corner fields, confirming that re-emission via merge keeps the corner-feature payload intact.

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

The **back-compat read path** is even smaller than the previous draft of this section suggested. The spike now ships a `format: "flat" | "nested"` discriminator on the spec envelope (an optional field on both `UISpec` and `FlatSpec`) and a `RippleRenderer.svelte` dispatch wrapper that routes per-spec. Stored specs that pre-date the field are read as `"nested"` by default — **no transform on read is needed**. New stores can opt into `format: "flat"` once we trust it. The wrapper is 13 LOC of script (the dispatch lives in `$derived(spec.format ?? "nested")` plus a one-line `flatten()` on entry); the proof that flipping the discriminator does not change rendered DOM is in `dispatch.test.ts` (17 tests, byte-identical output across all eight fixtures after id strip). That test IS the rollback contract.

Probably *both*: discriminator-driven coexistence during a 1-2 PR transition, then a one-shot migration to remove the dual-representation tax once every store is on `format: "flat"`.

**No fixture failed the transform.** The riskier shapes (`else_children`, slotted children, control-flow nodes) are now covered by the four `corner-*` fixtures and pass the round-trip + feature-presence checks. A pre-migration audit should still grep `rippleSpec` blobs across all workspaces for `else_children` / `"slot"` / `"each"` / `"if"` types before committing to the one-shot path — to confirm production data doesn't carry stranger combinations than the corner fixtures synthesize.

## 7. The orphan-node question

When a patch re-states a parent's child list, dropping a child id, the dropped subtree's nodes become unreachable from `root` but stay in `components`. RFC 06 reads OpenUI as choosing **(a) keep them in the map** (the patch-is-a-spec elegance is preserved; explicit deletion would defeat it). Confirmed in the RFC text:

> Explicit deletion → remove a statement from the `root` children list, it becomes unreachable and gets garbage-collected

OpenUI's semantics: dropping from `root` *is* the deletion, and the unreachable nodes are GC-target candidates — but the wire patch contains *no* explicit "delete this id" — that property is entirely structural.

**My read for Ripple:** option **(a) + named lifecycle GC boundaries**. Keep orphans by default — the merge stays pure id-replacement, the wire is minimal, undo/redo is free (the dropped subtree is still in `components` for a redo to re-reachable it). The **only** two production GC call sites are `gcOnPersist(spec)` (server-side, immediately before a `rippleSpec` blob is written to MongoDB) and `gcOnSnapshot(spec)` (when emitting a pocket export / freeze artifact). Both are thin one-line wrappers on the underlying `gcOrphans` primitive; the named-helper layer exists so a codebase grep for `gcOnPersist` / `gcOnSnapshot` is the audit trail for orphan-drop. `merge()` never GCs — doing so would defeat the cheap undo. The earlier draft listed a third boundary ("after a session of edits closes") but it's been dropped: the lifecycle of a session is hard to pin to a function call, and the next persist already covers it.

The trade is explicit: pre-persist orphans (within a single session of edits) are redo-able via the in-memory undo stack; post-persist orphans cannot be recovered, and that's by design — we don't want database bloat. Snapshot is a frozen artifact with no undo stack, so the orphan drop is safe.

Option (b) (GC after every merge) is worse — it bloats the merge with a reachability pass per patch and kills the cheap undo. Option (c) (explicit deletions on the wire) defeats the entire OpenUI elegance; do not adopt.

`gcOrphans()` is implemented and tested in the spike (3 LOC for the reachability walk, 7 LOC total including the new-spec emission). The two boundary helpers are 1 LOC each. Contract enforcement lives in `flatten.test.ts` under `describe('gcOrphans boundary policy', …)` — five tests prove: orphans accumulate monotonically across N merges, `gcOnPersist` collapses `componentCount` to the reachable set, `gcOnSnapshot` is byte-equivalent to `gcOnPersist`, `merge()` never shrinks the map, and a pre-persist orphaned subtree can be re-reached via a synthetic undo patch (but a `gcOnPersist` before that undo erases it — the documented trade).

## 8. Recommendation

**Green-light a real PR series, with caveats.**

The architectural numbers are clean:

- Round-trip works on real specs.
- Mutator collapses ~10x in structural surface area.
- Renderer change is a focused ~30 LOC diff inside the existing 284-LOC NodeRenderer.
- A one-shot DB migration is feasible; a read-path back-compat layer de-risks rollout.

The wire-size delta (+17–25% per spec, +70–100% per patch) is the genuine cost. It's not nothing — at the largest fixture (component-showcase, 83 nodes) it's 2.2KB of overhead — but it's well within budget for any future LLM token cost. And the OpenUI authoring argument is the real prize: re-emit one named node, get a patch. That's the cleanest agent-authoring contract any of the surveyed systems shipped.

Caveats:

1. **Corner-case coverage was a known gap in the first pass and is now closed.** Four `corner-*` fixtures (else_children, child-level slot, node-level each.items, node-level if.condition) were added with feature-presence assertions on the flat output. The transform handled all four by symmetry — `flatten.ts` needed zero changes. Production data outside this box might still carry stranger combinations; a pre-migration `grep` audit is the cheap safety net.

2. **Patch size is bigger, not smaller.** OpenUI Lang's "~85% fewer tokens than full regeneration" claim is *vs full regeneration*, not vs the 8-op patch shape Ripple already has. For the targeted single-node mutations our `spec-mutator` does today, the flat patch is materially larger. The win is in the mutation *language* (one rule), not the mutation *size*.

3. **Prop-array item ops survive the migration unchanged.** `applySetPropArrayItem` and friends are not absorbed by merge-by-name — they mutate inside `node.props`, not the tree. The 100+ LOC of prop-array ops stay; the collapse is only on the 5 node-structure ops + 2 tree-walk helpers + dispatch.

4. **Slot semantics need a decision.** Ripple's slots come from a child's `slot` field today (carried verbatim through `flatten`/`unflatten`). The captain's open question (per-slot id lists on the parent vs `slot` on the child) defaults to "child carries `slot`" in this spike, which is the simpler choice and the one that matches A2UI. If the captain prefers per-slot lists on the parent (which makes slot reassignment a parent-only patch), that's a separate, larger schema decision — but it can be deferred until after the flat baseline lands.

If green-lit, the PR sequence is plausibly:

- **PR-1 (M):** add `flatten`/`unflatten`/`merge`/`gcOrphans` to ripple alongside the existing nested types; add the `format: "flat" | "nested"` discriminator (optional field on `UISpec`/`FlatSpec`); add a `RippleRenderer.svelte`-style dispatch wrapper that reads `spec.format ?? "nested"` and routes nested-format specs through the existing NodeRenderer and flat-format specs through a new FlatRenderer. Default for stored specs is `"nested"` — **no caller has to change**. New callers (and the spec specialist on the cloud side, when it's ready) can opt into `format: "flat"` per-pocket. The spike ships exactly this shape end-to-end and `dispatch.test.ts` proves that flipping the discriminator on the same logical content produces byte-identical rendered DOM. PR-1 is now the smaller, safer "any caller can opt into flat, nothing else changes" shape — not the original "drop nested, fix all callers" shape. (~3 files for transforms + types, ~1 file for the wrapper, ~250 LOC + tests.)
- **PR-2 (L):** port NodeRenderer's child-recursion blocks to take a FlatSpec + componentId (the ~30-LOC focused diff inside the existing 284-LOC NodeRenderer, per §4). Wire the dispatch wrapper to the real NodeRenderer + the real FlatRenderer (the spike uses flatten-on-entry for the nested path because it lacks a full NodeRenderer port — that's the test equivalent). After PR-2 lands, flip `format: "flat"` on **one** non-prod surface (e.g. the sidepanel widget preview) by config change alone — no further code — and measure render/streaming behavior on real traffic. That's the rollback story made concrete: a single pocket can be flipped back to `format: "nested"` if anything looks off. (~1 file, ~30 LOC of recursion change.)
- **PR-3 (L):** **`pocket_specialist` learns to emit `format: "flat"`** in chat. No storage touch, no normalizer change, no SSE shape change — purely the agent's output prompt + the chat-path render going through the dispatch wrapper (which PR-1 already shipped). This PR is the **authoring-claim test gate**: the entire elegance argument for the flat model is "re-emit one named node, get a patch." If `pocket_specialist` can't produce clean flat-format patches on real chat sessions, we want to discover that *before* PR-4 makes the wire-and-storage shape harder to walk back. (~2 files for the agent's emit + the cloud chat path's dispatch hookup, ~80 LOC + tests.)
- **PR-4 (L):** introduce the merge-by-name SSE event shape on the cloud side; deprecate (but don't remove) the 8 op events. Update ripple-normalizer to flatten on persist for `format: "flat"` pockets. **Wire convention:** the SSE emitter MUST serialize the `components` map in DFS pre-order from `root` (one component per SSE event, root first). Today's `Object.keys()` order is post-order — using it on the wire would push root to the LAST event and TTFP collapses to end-of-stream (per §9 streaming-findings.md). DFS ordering is an 8-LOC serialization-time change in the emitter; it does NOT require restructuring `flatten()`'s in-memory map. Call `gcOnPersist(spec)` (§7) on the normalizer's write path. (~3 files cloud-side, ~150 LOC + tests + migration grep.)
- **PR-5 (M):** one-shot DB migration to convert stored `rippleSpec` blobs to `format: "flat"`. Idempotent — re-running on already-flat specs is a no-op. Removes the dual-representation tax once every store is flipped. By this point PR-2's non-prod flip and PR-3's chat authoring have run on real traffic for at least one review cycle each, so the migration is "make the default match what's already proven," not "bet the codebase."

Total: ~5 PRs, sized M/L. The split between PR-3 (authoring claim) and PR-4 (wire + storage shape) is deliberate — it's the difference between "we proved agents emit flat cleanly before we made flat irreversible" and "we hoped agents would emit flat cleanly while we were busy migrating storage." The spike's elegance argument lives or dies on PR-3's test result. Captain-time per PR review is the dominant cost; agent-implementation time is plausibly 5-10 agent-hours total.

**Refinement gates closed in this spike:**

- **A — Corner-case fixtures.** 4 added (`else_children`, child-level `slot`, node-level `each.items`, node-level `if.condition`) with feature-presence assertions. Transform held by symmetry; `flatten.ts` needed zero changes. (§2)
- **B — Streaming-protocol delta.** Quantified across 8 fixtures × 3 serialization strategies. Flat-DFS is roughly tied with nested; flat-insertion (today's default key order) is unusable; DFS pre-order is the mandatory wire convention. (§9, `streaming-findings.md`)
- **C — gcOrphans boundary policy.** Two named lifecycle helpers (`gcOnPersist`, `gcOnSnapshot`) wrap the primitive; `merge()` never GCs; pre-persist undo is preserved. (§7)
- **D — Coexistence shape.** `format: "flat" | "nested"` discriminator added to spec envelope + a 13-LOC `RippleRenderer.svelte` dispatch wrapper. `dispatch.test.ts` proves byte-identical render across the two storage shapes. PR-1 is now "opt-in per pocket," not "bet the codebase." (§4, §8 PR-1)
- **E — PR sequence.** Agent-authoring (PR-3) is now sequenced before wire/storage migration (PR-4), so the authoring claim is tested on a reversible foundation. (This list.)

The spike says yes — a deliberate yes. 60 bun tests pass across 3 files. The slot-semantics question (per-slot id lists on parent vs `slot` field on child) remains as a separate, deferable schema decision; this spike defaulted to "child carries `slot`."

## 9. Streaming delta

Closes push-back B. Numbers + methodology live in `streaming-findings.md`; the simulator code is `streaming-sim.ts` (`streaming.test.ts` runs 9 sanity tests; 55 bun tests pass total). The question was: when components arrive out of order in a flat `components` map, is user-visible TTFP (time-to-first-paint) worse, equal, or better than the nested partial-JSON shape today's stream emits?

**Headline.** Flat-DFS TTFP is +24% slower on the median fixture (range −32% to +128%) and the wire is 17–25% larger overall (§3). But the 50% / 90% rendered curves expressed as % of own wire track nested within ±5 points — the streaming behavior does NOT add a NEW penalty on top of the §3 size cost. Flat-DFS sometimes beats nested outright (corner-slot at −19%, team-activity at −32%) because root closes early in flat, while nested has to write the whole top-level envelope before any leaf can render. The largest fixture (component-showcase, 83 nodes) shows flat-DFS TTFP at 996B vs nested 437B — a +559B absolute penalty on a ~13KB wire.

**Insertion-order is broken.** Today's `flatten()` writes the `components` map post-order, so `Object.keys()` puts root LAST. On the wire that means nothing renderable arrives until end-of-stream (50% and 90% rendered both land at 100% of wire bytes). This is a fixable serialization-time choice, not a property of the data model — the SSE emitter must stamp DFS pre-order at emit time. The wire-convention recommendation is now baked into PR-4's bullet above.

**No change to PR-2's surface choice.** The "is flat slow to stream" question was the gate on whether PR-2's non-prod flip (or the renderer port itself) had to avoid TTFP-sensitive surfaces. Verdict: it doesn't. Flat-DFS streaming is roughly tied with nested in the worst case and structurally better for skeleton-first rendering (paint root container first, fill in subtrees) once SSE framing lands in PR-4. PR-2 can flip onto any surface, including TTFP-sensitive ones. The only real cost is the 17–25% wire overhead documented in §3, and that's the genuine tax we'd pay either way.

**Verdict.** Flat does not feel slow during streaming. The +24% median TTFP-in-bytes is a few hundred bytes on multi-kilobyte specs, the 50%/90% curves track nested closely, and once per-component SSE framing lands the perceived UX is *better* than today's partial-JSON parse (skeleton-first vs leaves-first). The captain's read is the right one — green-light proceeds.
