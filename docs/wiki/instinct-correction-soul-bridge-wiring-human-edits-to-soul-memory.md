---
{
  "title": "Instinct Correction Soul Bridge — Wiring Human Edits to Soul Memory",
  "summary": "Acts as the connector between the Instinct correction system and soul-protocol's memory tiers, turning captured human edits into episodic observations and promoting repeated field-level corrections into procedural rules. It degrades silently when the soul is unavailable so the approval flow is never blocked.",
  "concepts": [
    "CorrectionSoulBridge",
    "soul observe",
    "episodic memory",
    "procedural memory promotion",
    "promotion threshold",
    "silent degradation",
    "soul_manager",
    "InstinctStore",
    "synthesize rule",
    "human-in-the-loop learning",
    "pocket-scoped soul"
  ],
  "categories": [
    "instinct engine",
    "soul protocol integration",
    "correction loop",
    "enterprise edition"
  ],
  "source_docs": [
    "0ee7502451fce914"
  ],
  "backlinks": null,
  "word_count": 474,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

`CorrectionSoulBridge` answers: how does the system get smarter over time? Each time a human edits an agent proposal before approving, the system has evidence about preferences. The bridge converts that evidence into two kinds of soul memory: episodic observations (what happened) and procedural rules (how to behave in the future).

## Architecture

The bridge holds two injected dependencies: a `soul_manager` (the object with `.get_soul(pocket_id)` semantics) and a `store` (`InstinctStore`, used to count how many times a given `path` has been corrected in a pocket). It exposes a single public method: `record(correction, action)`.

## record() — The Main Entry Point

Calling `record(correction, action)` after persisting a `Correction` to the store triggers:

1. **Soul lookup** — if no soul is loaded for the pocket, the method returns silently. This is the degradation path: corrections still persist to SQLite and can be read back by the agent tool, but they do not reach the soul. This prevents a soul failure from blocking a human approval.

2. **Episodic observation** — `_observe_correction()` calls `soul.observe()` with an `Interaction` whose text is `correction.context_summary`. Importance is fixed at 5 (episodic tier). This enters the recall index so future proposals in similar contexts surface the correction.

3. **Procedural promotion check** — `_maybe_promote_to_procedural()` calls `store.count_corrections_by_path(pocket_id, path)` for each patch. When a path has been edited `_PROMOTION_THRESHOLD` (3) times, `_synthesize_rule()` generates a short imperative sentence like `"Prefer 'high' over 'medium' for priority in this context"` and calls `soul.remember()` with importance 7 (procedural tier).

## Promotion Threshold Rationale

The threshold of 3 is a deliberate tradeoff. One edit could be a mistake or an unusual situation. Two edits could be coincidence. Three edits on the same field across different actions suggests a stable preference that the agent should internalize. Setting this higher reduces noise but delays learning; setting it lower risks cluttering procedural memory with ephemeral edits.

## _synthesize_rule

`_synthesize_rule(patch, correction)` builds a human-readable rule string from the patch's `before` and `after` values and the action's `action_title`. The `_fmt()` helper coerces enum members to their `.value` string so rules read cleanly. The resulting rule becomes the text stored in soul procedural memory.

## Silent Degradation Design

Every soul interaction is wrapped in try/except with logging at WARNING level. The design choice is explicit: the approval flow (user clicking approve) must never fail because of a soul connectivity issue. The soul is an enhancement, not a gate.

## Known Gaps

- `_PROMOTION_THRESHOLD` is a module-level constant. There is no per-pocket or per-user threshold configuration.
- The bridge does not check whether a rule with the same text already exists in soul procedural memory, so the same rule can be written multiple times if the counter is not properly bounded.
- `soul_manager` is typed as `object` (with `TYPE_CHECKING` import for `InstinctStore`) to avoid a circular import; the lack of a formal protocol makes static analysis weaker.