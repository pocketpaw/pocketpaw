---
{
  "title": "Correction Soul Bridge: Observe Callbacks and Procedural Memory Promotion Tests",
  "summary": "This test suite covers the `CorrectionSoulBridge` — the enterprise component that writes user corrections into the Soul Protocol's memory system and promotes recurring correction patterns into procedural memory. It also validates the `InstinctCorrectionsTool`, the agent-facing interface that exposes captured corrections as readable context.",
  "concepts": [
    "CorrectionSoulBridge",
    "soul.observe",
    "procedural promotion",
    "InstinctCorrectionsTool",
    "correction patches",
    "soul protocol",
    "memory tiers",
    "graceful degradation",
    "3x heuristic",
    "enterprise guard"
  ],
  "categories": [
    "instinct",
    "soul-protocol",
    "testing",
    "enterprise",
    "test"
  ],
  "source_docs": [
    "63cf3610fd0746f4"
  ],
  "backlinks": null,
  "word_count": 600,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `CorrectionSoulBridge` sits at the intersection of PocketPaw's Instinct decision pipeline and the Soul Protocol memory system. When a user edits an AI-proposed action (a "correction"), the bridge both observes the interaction and detects behavioral patterns worthy of long-term retention. These tests lock the exact shape of that integration so neither side can drift silently.

## Why the Bridge Exists

Without the bridge, corrections would live only in the `InstinctStore` — queryable for audit purposes but invisible to the soul. The soul would keep proposing actions in styles the user has already rejected. The bridge solves this by calling `soul.observe()` on every correction, giving the soul runtime a stream of what the user wanted vs. what the agent produced.

## TestObserveCorrection

Four tests cover the observe path:

- **`test_observe_called_once_per_correction`** — ensures the bridge does not batch or debounce; each `record()` call fires exactly one `observe()`. This prevents the soul from missing signal when multiple corrections arrive quickly.
- **`test_observe_payload_includes_summary_and_patches`** — verifies that the `Interaction` object passed to `observe()` carries both the `pocket_id`/`actor` identity context in `user_input` and the field-level diff (patch paths) plus `context_summary` in `agent_output`. The soul needs both halves to learn *who* corrected *what*.
- **`test_no_observe_when_soul_is_absent`** — when `manager.soul` is `None` (soul not loaded, optional dep not installed), `record()` must silently no-op rather than raise. This matters for community-tier deployments where Soul Protocol is not available.
- **`test_observe_exception_is_swallowed`** — if the soul runtime crashes mid-observe, the approval workflow must continue. A user approving an action should never see a 500 because the memory subsystem is flaky.

## TestProceduralPromotion: The 3x Heuristic

The bridge implements a lightweight pattern-detection rule: when the same correction *path* (e.g., `parameters.tone`) appears three or more times across distinct corrections, it calls `soul.remember()` with `type="procedural"` and `importance=7`. This elevates the preference from a series of one-off observations into a durable behavioral rule the soul can apply proactively.

```python
# Third occurrence fires remember(); first two do not
await bridge.record(first, _action())
assert fake_soul.remember.await_count == 0
await bridge.record(second, _action())
assert fake_soul.remember.await_count == 0
await bridge.record(third, _action())
assert fake_soul.remember.await_count == 1
```

Three guard tests prevent regressions:

- **`test_does_not_re_promote_past_threshold`** — a fourth correction on the same path must not fire a second `remember()`. The promotion is idempotent at the threshold boundary.
- **`test_promotes_per_path_independently`** — `title` corrections and `priority` corrections each maintain their own counter. Three `title` edits plus three `priority` edits correctly fire `remember()` twice.

## TestInstinctCorrectionsTool

The `InstinctCorrectionsTool` is the agent-readable surface of the correction store. Four tests cover its contract:

- Empty store returns a "No corrections captured" message so the agent does not interpret silence as an error.
- A seeded store returns a formatted summary including action title, actor, and every patch's before/after values — giving the agent enough context to adjust future proposals.
- When `_get_instinct_store()` returns `None` (enterprise module unavailable), the tool returns a message indicating the feature requires an enterprise license rather than raising.
- The tool's JSON schema must advertise `pocket_id` as the sole required parameter, ensuring channel adapters can discover and call it without hardcoding.

## Fixture: `_stub_soul_protocol`

An `autouse` fixture injects a minimal fake `soul_protocol` module when the real one is absent. This prevents import errors in the base dev environment (where Soul Protocol is optional) while still exercising the bridge's code paths. The stub defines only `Interaction`, which is all the bridge imports.

## Known Gaps

None flagged in the source. The comment notes this covers "Move 1 PR-B" — the soul bridge is part of a phased delivery, so the correction *summarization* logic and advanced memory tier selection may be extended in future PRs.