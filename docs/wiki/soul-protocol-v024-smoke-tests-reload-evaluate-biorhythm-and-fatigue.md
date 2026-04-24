---
{
  "title": "Soul Protocol v0.2.4 Smoke Tests: Reload, Evaluate, Biorhythm, and Fatigue",
  "summary": "Smoke tests for soul-protocol v0.2.4 features including hot-reload, the evaluate endpoint, biorhythm config round-trips, dirty tracking for autosave optimization, external change detection, and the fatigue hint in bootstrap context. Designed to catch regressions when upgrading the soul-protocol dependency.",
  "concepts": [
    "soul-protocol v0.2.4",
    "hot-reload",
    "evaluate endpoint",
    "biorhythm",
    "dirty tracking",
    "autosave",
    "external change detection",
    "fatigue hint",
    "bootstrap context"
  ],
  "categories": [
    "testing",
    "soul management",
    "version compatibility",
    "test"
  ],
  "source_docs": [
    "af1b5b51ea00dfea"
  ],
  "backlinks": null,
  "word_count": 477,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When PocketPaw upgrades its `soul-protocol` dependency, new API surface appears and existing behaviour may shift. This smoke-test file targets v0.2.4 specifically, pinning the features introduced in that release. It is structured around six concern areas, each in its own test class.

## Reload

`TestReload` validates three aspects of hot-reload:

- `test_reload_returns_updated_name`: Writing a new soul file to disk and calling `reload()` makes the manager load the updated soul. Without this, the running agent would serve stale identity data indefinitely.
- `test_reload_invalidates_tools_cache`: Reload must clear the tool cache so the next `get_tools()` call reflects the reloaded soul's capabilities.
- `test_reload_updates_bridge_and_provider`: The bootstrap provider and tool bridge must be refreshed, not just the internal soul object, so downstream consumers see the updated state.

## Evaluate

`TestEvaluate` tests the `evaluate()` endpoint, which returns a scores dict in v0.2.4+ or `None` for older versions. The dual-outcome test (`test_evaluate_returns_dict_or_none`) accommodates both, making the test forward and backward compatible. `test_evaluate_returns_none_without_soul` guards against calling `evaluate()` before the soul is initialised.

## Biorhythm

`TestBiorhythm` checks that biorhythm settings survive round-trips through `SoulSettings`. Three cases: explicit values are stored, missing values fall back to defaults, and a dashboard validation step succeeds. Biorhythm drives the soul's energy and mood model — incorrect defaults would cause personality drift from the first session.

## Dirty Tracking

`TestDirtyTracking` is the most granular suite in the file, with five tests:

- Clean after init, dirty after observe, clean after save — the basic state machine.
- `test_auto_save_skips_when_clean`: The autosave loop must not write to disk when nothing has changed. Unnecessary writes cause disk churn and, on constrained hardware, measurable performance degradation.
- `test_auto_save_writes_when_dirty`: Conversely, a dirty soul must be persisted by the autosave loop or the session's memories are lost on unexpected shutdown.

## Auto-Sync / External Change Detection

`TestAutoSync` verifies the manager's response to external file modifications:

- No spurious change detection when the file is unmodified.
- Detection fires when the file timestamp/content changes.
- `test_reload_clears_external_change`: After a reload, the change-detection flag is cleared so the next polling cycle starts fresh.

This feature enables the `soul` CLI and the PocketPaw agent to operate concurrently on the same soul file without corrupting each other's state.

## Fatigue Hint

`TestFatigueHint` checks that when the soul's energy is at or below the `tired_threshold`, the bootstrap context includes a fatigue hint — a signal to the LLM to adjust the soul's tone and response length. When energy is above the threshold, no hint is injected. This is a soft personality cue, not a hard capability restriction.

## Known Gaps

The module doc lists the covered features explicitly, suggesting the author intended this as a comprehensive v0.2.4 contract test rather than a partial implementation.

```python
# Dirty tracking state machine
# init -> clean
# observe() -> dirty
# save() -> clean
# autosave loop: skip if clean, write if dirty
```
