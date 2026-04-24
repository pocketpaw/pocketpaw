---
{
  "title": "Fleet Installer Journal Emission Tests: Correlated Events, Backward Compatibility, and Partial Install",
  "summary": "This suite verifies that the fleet installer emits a correlated sequence of journal events (`fleet.install.started`, `agent.spawned` per soul, `fleet.installed` summary) when given a journal, remains silent when the journal is omitted, suppresses the terminal event on soul-creation failure, and falls back to a fleet-scoped tag when the fleet declares no scopes.",
  "concepts": [
    "install_fleet",
    "FleetTemplate",
    "fleet.install.started",
    "agent.spawned",
    "fleet.installed",
    "correlation ID",
    "journal emission",
    "backward compatibility",
    "partial install",
    "scope fallback"
  ],
  "categories": [
    "testing",
    "enterprise edition",
    "fleet",
    "journal",
    "test"
  ],
  "source_docs": [
    "d4683290d29ae1ab"
  ],
  "backlinks": null,
  "word_count": 525,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's fleet installer (`install_fleet`) provisions a set of AI agents from a `FleetTemplate`. Starting with the `feat/fleet-journal-emission` feature, it optionally writes correlated journal events so that dashboards, audit trails, and projections can track fleet installation progress and outcomes. The journal is optional — callers that do not pass one get the same installation behavior, silently.

## The Three-Event Contract

A successful fleet installation with a journal emits exactly three events in order:

1. `fleet.install.started` — marks the beginning of the install attempt.
2. `agent.spawned` — one per soul created from the soul factory.
3. `fleet.installed` — terminal event summarizing the outcome.

```python
async def test_emits_started_spawned_installed_in_order(journal, ...):
    ...
    events = journal.query(limit=100)
    actions = [e.action for e in events]
    assert actions == ["fleet.install.started", "agent.spawned", "fleet.installed"]
```

The order matters because projections tail the journal stream and expect to see the start event before any agent events, and the terminal event last. A projection that sees `fleet.installed` before `agent.spawned` could mark the fleet as complete before all agents are recorded.

## Correlation ID

All events from a single `install_fleet` call share one correlation ID:

```python
corr_ids = {e.correlation_id for e in events}
assert len(corr_ids) == 1
```

This allows operators to filter the journal to "all events from fleet install run X" across any number of agents and steps, even when multiple fleet installs are running concurrently and their events interleave.

## Backward Compatibility

`TestBackwardCompat` covers two scenarios:

1. `journal` parameter omitted entirely — `install_fleet` succeeds normally, no events, no errors.
2. `journal=None` passed explicitly — same behavior. An unrelated journal opened before the call remains empty afterward.

This is critical for callers that were written before journal support was added. The journal is opt-in; making it required would be a breaking change.

## Partial Install: No Terminal Event on Soul Failure

```python
async def test_soul_failure_emits_started_only_no_terminal(journal):
    factory.load_bundled = MagicMock(side_effect=FileNotFoundError("template missing"))
    ...
    actions = [e.action for e in events]
    assert actions == ["fleet.install.started"]
    assert "fleet.installed" not in actions
```

If the soul factory fails (template not found, network error), the install never produces a soul. Emitting `fleet.installed` in this case would tell projections that the fleet is "done" when in fact no agent was ever created. The test enforces that the terminal event is suppressed, leaving the projection in a "started but not completed" state that operators can identify and retry.

Conversely, if the soul was created but a downstream connector fails, the terminal event IS emitted (with `succeeded=False`) because the agent exists and the connector failure is a recoverable partial outcome.

## Scope Fallback

```python
async def test_empty_scopes_fall_back_to_fleet_tag(journal, ...):
    fleet = _basic_fleet(scopes=[])
    ...
    for event in journal.query(limit=100):
        assert event.scope == ["fleet:sales-fleet"]
```

The journal's `EventEntry` invariant requires non-empty scope. When a fleet template declares no scopes, the installer synthesizes `fleet:<fleet-name>` as a fallback. This ensures events are always routable and queryable, even for fleets that predated the scope system.

## Known Gaps

There is no test for concurrent fleet installs sharing the same journal. The correlation ID is generated per call, but if two installs run simultaneously, the test suite does not verify that their events do not interleave in a way that confuses the projection.