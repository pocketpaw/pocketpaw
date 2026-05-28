# tests/unit/test_temporal_sweeper.py
# Created: 2026-05-28 (feat/rfc-03-v2-temporal) — unit tests for the
# RFC 03 v2 temporal trigger sweeper (``sweep_temporal_triggers``).
# Rising-edge semantics (false → true), state passthrough, per-row
# error isolation, multi-trigger fan-out, deterministic ``now``,
# default vs explicit ``row_id_field`` resolution.
"""Tests for the temporal trigger sweeper.

The sweeper is a pure decision function — given a template, the
current row set, and the prior-sweep state, it reports which CEL
``when`` predicates flipped from false to true and returns the
updated state. The EE-side sweeper (``ee/cloud/pockets/``) owns
cadence, persistence, and action dispatch; PR 2f is the OSS-side
brain it calls.

Covered cases (per the PR 2f brief):

* Rising edge fires (false → true).
* No re-fire when already true (idempotent re-evaluation).
* No fire when predicate stays false.
* Falling edge does NOT fire but DOES update state to false.
* First sweep (empty state) — currently-true rows count as rising.
* Non-temporal triggers (cron / source_change / manual) skipped.
* Per-row CEL eval failure isolates: error collected, other rows
  still process.
* Multiple temporal triggers on the same template are both run.
* ``now`` injection is deterministic.
* ``row_id_field`` override is honored.
* Default ``row_id_field`` falls back to ``template.state.id_field``.
* Empty inputs (no rows, no temporal triggers) are no-ops.
* ``SweepResult`` is frozen / immutable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from pocketpaw.bundled_templates import (
    PocketTemplate,
    SweepResult,
    TemporalRisingEdge,
    TemporalSweepError,
    sweep_temporal_triggers,
)

# ---------------------------------------------------------------------------
# Template fixtures
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "templates"


def _lease_renewal_template() -> PocketTemplate:
    """Load the bundled lease-renewal v2 fixture. Has three triggers:
    cron, source_change, temporal, manual — only the temporal one
    should be processed by the sweeper."""
    raw = (_FIXTURE_DIR / "lease-renewal-v2.yaml").read_text()
    return PocketTemplate.model_validate(yaml.safe_load(raw))


def _multi_temporal_template() -> PocketTemplate:
    """A template with TWO temporal triggers — proves the sweeper
    fans out over every temporal trigger, not just the first one."""
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "multi-temporal-test",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "general",
            "description": "Two temporal triggers; one CEL predicate each.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Thing",
                "id_field": "id",
                "columns": [
                    {"field": "id", "widget": "text"},
                    {"field": "score", "widget": "trend"},
                    {"field": "stage", "widget": "status_dot"},
                ],
            },
            "triggers": [
                {
                    "type": "temporal",
                    "when": "score >= 10",
                    "action": "celebrate",
                },
                {
                    "type": "temporal",
                    "when": "stage == 'red'",
                    "action": "escalate",
                },
            ],
        }
    )


def _no_temporal_template() -> PocketTemplate:
    """A template with cron + manual but NO temporal triggers."""
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "no-temporal-test",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "general",
            "description": "Cron-only template — sweeper should ignore.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Thing",
                "columns": [{"field": "id", "widget": "text"}],
            },
            "triggers": [
                {"type": "cron", "schedule": "0 8 * * MON", "action": "noop"},
                {"type": "manual"},
            ],
        }
    )


def _shared_action_template() -> PocketTemplate:
    """Two temporal triggers sharing the same ``action`` name — the
    sweeper must disambiguate the state key so they don't collide."""
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "shared-action-test",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "general",
            "description": "Two temporal triggers with the same action.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Thing",
                "columns": [
                    {"field": "id", "widget": "text"},
                    {"field": "left", "widget": "trend"},
                    {"field": "right", "widget": "trend"},
                ],
            },
            "triggers": [
                {"type": "temporal", "when": "left > 0", "action": "alert"},
                {"type": "temporal", "when": "right > 0", "action": "alert"},
            ],
        }
    )


def _custom_id_template() -> PocketTemplate:
    """Template whose ``state.id_field`` is ``"lease_id"`` — proves the
    default ``row_id_field`` resolution path picks the right key."""
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "custom-id-test",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "general",
            "description": "Custom id_field declared on state.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Lease",
                "id_field": "lease_id",
                "columns": [
                    {"field": "lease_id", "widget": "text"},
                    {"field": "score", "widget": "trend"},
                ],
            },
            "triggers": [
                {"type": "temporal", "when": "score >= 10", "action": "alert"},
            ],
        }
    )


# A wall-clock fixed point that places the lease-renewal-v2 trigger's
# ``within(expires_at, duration('60d'))`` window exactly at 60 days
# before / after 2026-05-28.
NOW = datetime(2026, 5, 28, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Rising-edge core semantics
# ---------------------------------------------------------------------------


def test_rising_edge_fires_on_lease_renewal_predicate() -> None:
    """A row inside the 60-day expiry window with no renewal_stage
    set should trigger ``draft_renewal`` on the first sweep."""
    template = _lease_renewal_template()
    rows = [
        # Inside 60d, stage null → predicate true → rising edge
        {
            "id": "lease-1",
            "expires_at": NOW + timedelta(days=30),
            "renewal_stage": None,
        },
    ]
    result = sweep_temporal_triggers(
        template,
        rows,
        last_seen_state={},
        now=NOW,
    )

    assert len(result.rising_edges) == 1
    edge = result.rising_edges[0]
    assert edge.row_id == "lease-1"
    assert edge.action == "draft_renewal"
    assert edge.row["renewal_stage"] is None
    assert result.errors == []
    # State must always be updated, even when no edge fires elsewhere.
    assert result.new_state[("draft_renewal", "lease-1")] is True


def test_no_refire_when_already_true() -> None:
    """If the predicate was already true on the prior sweep, the
    same row must NOT emit a rising edge on this sweep."""
    template = _lease_renewal_template()
    rows = [
        {
            "id": "lease-1",
            "expires_at": NOW + timedelta(days=30),
            "renewal_stage": None,
        },
    ]
    result = sweep_temporal_triggers(
        template,
        rows,
        last_seen_state={("draft_renewal", "lease-1"): True},
        now=NOW,
    )
    assert result.rising_edges == []
    # State stays true (idempotent re-evaluation).
    assert result.new_state[("draft_renewal", "lease-1")] is True


def test_no_fire_when_predicate_stays_false() -> None:
    """Row outside the 60-day window: predicate false, state stays
    false, no rising edge."""
    template = _lease_renewal_template()
    rows = [
        # 365d out — far outside the 60d temporal window.
        {
            "id": "lease-2",
            "expires_at": NOW + timedelta(days=365),
            "renewal_stage": None,
        },
    ]
    result = sweep_temporal_triggers(
        template,
        rows,
        last_seen_state={},
        now=NOW,
    )
    assert result.rising_edges == []
    assert result.new_state[("draft_renewal", "lease-2")] is False


def test_falling_edge_does_not_fire_but_updates_state() -> None:
    """Prior True, current False → no rising edge, but state must
    update to False so the next true-transition fires again."""
    template = _lease_renewal_template()
    rows = [
        # The tenant returned the renewal — stage no longer null,
        # so the temporal predicate is now false.
        {
            "id": "lease-1",
            "expires_at": NOW + timedelta(days=30),
            "renewal_stage": "sent",
        },
    ]
    result = sweep_temporal_triggers(
        template,
        rows,
        last_seen_state={("draft_renewal", "lease-1"): True},
        now=NOW,
    )
    assert result.rising_edges == []
    assert result.new_state[("draft_renewal", "lease-1")] is False


def test_first_sweep_empty_state_currently_true_fires() -> None:
    """When last_seen_state is empty (very first sweep), a currently
    true predicate IS a rising edge: default-false → current-true."""
    template = _lease_renewal_template()
    rows = [
        {
            "id": "lease-1",
            "expires_at": NOW + timedelta(days=30),
            "renewal_stage": None,
        },
    ]
    result = sweep_temporal_triggers(template, rows, last_seen_state=None, now=NOW)
    assert len(result.rising_edges) == 1
    assert result.rising_edges[0].row_id == "lease-1"


# ---------------------------------------------------------------------------
# Non-temporal trigger isolation
# ---------------------------------------------------------------------------


def test_non_temporal_triggers_silently_skipped() -> None:
    """lease-renewal-v2 has cron + source_change + temporal + manual.
    The sweeper must only produce results for the temporal one — no
    errors, no rising edges from the other three."""
    template = _lease_renewal_template()
    rows = [
        {
            "id": "lease-1",
            "expires_at": NOW + timedelta(days=30),
            "renewal_stage": None,
        },
    ]
    result = sweep_temporal_triggers(template, rows, last_seen_state={}, now=NOW)

    # All rising edges must be tied to the temporal trigger's action.
    for edge in result.rising_edges:
        assert edge.action == "draft_renewal"
    # State must never carry keys for non-temporal trigger actions.
    for key in result.new_state:
        action_part = key[0]
        # bulk_draft, mark_renewed are the cron/source_change actions.
        assert action_part not in {"bulk_draft", "mark_renewed"}


def test_template_with_no_temporal_triggers_returns_empty() -> None:
    template = _no_temporal_template()
    rows = [{"id": "thing-1"}, {"id": "thing-2"}]
    result = sweep_temporal_triggers(template, rows, last_seen_state={}, now=NOW)
    assert result.rising_edges == []
    assert result.new_state == {}
    assert result.errors == []


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


def test_cel_failure_on_one_row_doesnt_abort_sweep() -> None:
    """A row missing the field referenced by the temporal predicate
    should produce a TemporalSweepError but not break other rows."""
    template = _lease_renewal_template()
    rows = [
        # Good row → fires.
        {
            "id": "lease-good",
            "expires_at": NOW + timedelta(days=30),
            "renewal_stage": None,
        },
        # Bad row → missing ``expires_at`` makes the CEL evaluator
        # raise. The sweeper must capture it and keep going.
        {
            "id": "lease-bad",
            "renewal_stage": None,
        },
    ]
    result = sweep_temporal_triggers(template, rows, last_seen_state={}, now=NOW)

    # Good row still fires.
    assert len(result.rising_edges) == 1
    assert result.rising_edges[0].row_id == "lease-good"

    # Bad row contributes an error.
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.row_id == "lease-bad"
    assert err.action == "draft_renewal"
    assert "expires_at" in err.message

    # Bad-row state should NOT be written — preserve prior so a future
    # successful eval can still detect rising edge.
    assert ("draft_renewal", "lease-bad") not in result.new_state


# ---------------------------------------------------------------------------
# Multi-trigger fan-out
# ---------------------------------------------------------------------------


def test_multiple_temporal_triggers_both_evaluated() -> None:
    """A template with two temporal triggers should produce per-row
    results keyed by each trigger's action."""
    template = _multi_temporal_template()
    rows = [
        {"id": "t1", "score": 15, "stage": "green"},  # only ``celebrate`` fires
        {"id": "t2", "score": 3, "stage": "red"},  # only ``escalate`` fires
        {"id": "t3", "score": 20, "stage": "red"},  # both fire
        {"id": "t4", "score": 1, "stage": "green"},  # neither fires
    ]
    result = sweep_temporal_triggers(template, rows, last_seen_state={}, now=NOW)

    edges = {(e.action, e.row_id) for e in result.rising_edges}
    assert edges == {
        ("celebrate", "t1"),
        ("escalate", "t2"),
        ("celebrate", "t3"),
        ("escalate", "t3"),
    }

    # Every row × trigger pair appears in new_state.
    for row_id in ("t1", "t2", "t3", "t4"):
        assert ("celebrate", row_id) in result.new_state
        assert ("escalate", row_id) in result.new_state


def test_shared_action_triggers_get_disambiguated_keys() -> None:
    """Two temporal triggers sharing the same ``action`` must end up
    with distinct ``trigger_key`` values in new_state so their states
    don't collide."""
    template = _shared_action_template()
    rows = [{"id": "row-1", "left": 5, "right": 0}]
    result = sweep_temporal_triggers(template, rows, last_seen_state={}, now=NOW)

    keys_for_row = [k for k in result.new_state if k[1] == "row-1"]
    # Two distinct keys for the two ``alert`` triggers.
    assert len(keys_for_row) == 2
    assert len({k[0] for k in keys_for_row}) == 2


# ---------------------------------------------------------------------------
# Determinism (``now`` injection)
# ---------------------------------------------------------------------------


def test_now_is_honored_for_within_predicate() -> None:
    """The lease-renewal predicate hinges on ``within(expires_at,
    duration('60d'))``. Two different ``now`` values for the same row
    must produce different rising-edge sets."""
    template = _lease_renewal_template()
    expires_at = datetime(2026, 7, 15, tzinfo=UTC)
    rows = [
        {"id": "lease-1", "expires_at": expires_at, "renewal_stage": None},
    ]

    # ``now`` ~48 days before expiry → inside 60d window → rising edge.
    close_now = datetime(2026, 5, 28, tzinfo=UTC)
    inside = sweep_temporal_triggers(template, rows, last_seen_state={}, now=close_now)
    assert len(inside.rising_edges) == 1

    # ``now`` ~12 months before expiry → outside 60d window → no edge.
    far_now = datetime(2025, 7, 15, tzinfo=UTC)
    outside = sweep_temporal_triggers(template, rows, last_seen_state={}, now=far_now)
    assert outside.rising_edges == []


# ---------------------------------------------------------------------------
# row_id_field resolution
# ---------------------------------------------------------------------------


def test_explicit_row_id_field_override() -> None:
    """An explicit ``row_id_field`` argument wins over the template
    default."""
    template = _custom_id_template()
    rows = [
        # Use a different field name than the template's id_field to
        # prove the override flows through.
        {"lease_id": "ignored", "external_key": "EXT-42", "score": 15},
    ]
    result = sweep_temporal_triggers(
        template,
        rows,
        last_seen_state={},
        now=NOW,
        row_id_field="external_key",
    )
    assert len(result.rising_edges) == 1
    assert result.rising_edges[0].row_id == "EXT-42"
    assert ("alert", "EXT-42") in result.new_state


def test_default_row_id_field_uses_template_state_id_field() -> None:
    """When ``row_id_field`` is not supplied, the sweeper falls back
    to ``template.state.id_field``."""
    template = _custom_id_template()
    rows = [{"lease_id": "L-1", "score": 15}]
    result = sweep_temporal_triggers(template, rows, last_seen_state={}, now=NOW)
    assert len(result.rising_edges) == 1
    assert result.rising_edges[0].row_id == "L-1"


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


def test_no_rows_returns_empty_result() -> None:
    template = _lease_renewal_template()
    result = sweep_temporal_triggers(template, [], last_seen_state={}, now=NOW)
    assert result.rising_edges == []
    assert result.new_state == {}
    assert result.errors == []


def test_no_temporal_no_rows_returns_empty() -> None:
    template = _no_temporal_template()
    result = sweep_temporal_triggers(template, [], last_seen_state={}, now=NOW)
    assert result.rising_edges == []
    assert result.new_state == {}
    assert result.errors == []


# ---------------------------------------------------------------------------
# Frozen model
# ---------------------------------------------------------------------------


def test_sweep_result_is_frozen() -> None:
    template = _lease_renewal_template()
    rows = [
        {
            "id": "lease-1",
            "expires_at": NOW + timedelta(days=30),
            "renewal_stage": None,
        },
    ]
    result = sweep_temporal_triggers(template, rows, last_seen_state={}, now=NOW)
    # Pydantic v2 frozen → ValidationError on attribute mutation.
    from pydantic import ValidationError

    with pytest.raises((ValidationError, TypeError, AttributeError)):
        result.rising_edges = []  # type: ignore[misc]


def test_temporal_rising_edge_round_trip() -> None:
    """TemporalRisingEdge / TemporalSweepError are dataclass-like
    structured records — they must be re-importable from the package
    root."""
    assert TemporalRisingEdge is not None
    assert TemporalSweepError is not None
    assert SweepResult is not None
