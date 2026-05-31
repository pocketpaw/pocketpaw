# tests/ee/foresight/test_instinct_bridge.py — RFC 08 PR 8.
# Created: 2026-05-25 (feat/foresight-v08-approval-loop)
# — pure-conversion tests for the Foresight → Instinct bridge module.
# The bridge is a pure function (no I/O, no Beanie, no Instinct store
# call), so these tests exercise it directly with duck-typed projection
# fixtures rather than spinning up Mongo or SQLite.
"""Unit tests for the Foresight → Instinct bridge (RFC 08 §8 conversion)."""

from __future__ import annotations

from types import SimpleNamespace

from pocketpaw_ee.foresight.instinct_bridge import (
    InstinctProposal,
    build_dedupe_key,
    projected_decision_to_instinct_proposal,
)


def _pd(
    *,
    workspace_id: str = "w1",
    run_id: str = "r1",
    anchor_id: str = "decision:renewal",
    persona_id: str = "p1",
    tick_id: int = 0,
    decision_text: str = "accept",
    confidence: float = 0.5,
    sub_type: str = "decision_forecast",
    forward_precedent_decision_id: str | None = None,
) -> SimpleNamespace:
    """Duck-typed ProjectedDecision stand-in.

    The bridge stays attribute-typed so test doubles don't have to
    import the cloud domain value object — a ``SimpleNamespace`` with
    the same field names is enough.
    """
    return SimpleNamespace(
        workspace_id=workspace_id,
        run_id=run_id,
        anchor_id=anchor_id,
        persona_id=persona_id,
        tick_id=tick_id,
        decision_text=decision_text,
        confidence=confidence,
        sub_type=sub_type,
        forward_precedent_decision_id=forward_precedent_decision_id,
    )


# ---------------------------------------------------------------------------
# build_dedupe_key — deterministic, joined-string key
# ---------------------------------------------------------------------------


def test_dedupe_key_is_stable_for_same_inputs() -> None:
    a = build_dedupe_key(
        workspace_id="w1", run_id="r1", tick_id=2, anchor_id="decision:lease", persona_id="p7"
    )
    b = build_dedupe_key(
        workspace_id="w1", run_id="r1", tick_id=2, anchor_id="decision:lease", persona_id="p7"
    )
    assert a == b
    assert a == "w1|r1|2|decision:lease|p7"


def test_dedupe_key_includes_empty_persona_segment() -> None:
    # Engine emits one record per anchor even when no persona acted;
    # the empty persona_id must be preserved as an empty segment so
    # the key shape is deterministic across that branch.
    key = build_dedupe_key(
        workspace_id="w1", run_id="r1", tick_id=0, anchor_id="rollout:announce", persona_id=""
    )
    assert key == "w1|r1|0|rollout:announce|"


def test_dedupe_key_differs_when_any_axis_changes() -> None:
    base = build_dedupe_key(
        workspace_id="w1", run_id="r1", tick_id=0, anchor_id="a", persona_id="p"
    )
    assert base != build_dedupe_key(
        workspace_id="w2", run_id="r1", tick_id=0, anchor_id="a", persona_id="p"
    )
    assert base != build_dedupe_key(
        workspace_id="w1", run_id="r2", tick_id=0, anchor_id="a", persona_id="p"
    )
    assert base != build_dedupe_key(
        workspace_id="w1", run_id="r1", tick_id=1, anchor_id="a", persona_id="p"
    )
    assert base != build_dedupe_key(
        workspace_id="w1", run_id="r1", tick_id=0, anchor_id="b", persona_id="p"
    )
    assert base != build_dedupe_key(
        workspace_id="w1", run_id="r1", tick_id=0, anchor_id="a", persona_id="q"
    )


# ---------------------------------------------------------------------------
# projected_decision_to_instinct_proposal — shape + labels
# ---------------------------------------------------------------------------


def test_conversion_returns_proposal_with_expected_fields() -> None:
    proposal = projected_decision_to_instinct_proposal(_pd())
    assert isinstance(proposal, InstinctProposal)
    assert proposal.pocket_id == "foresight:run:r1"
    assert proposal.category == "data"  # RFC §8 — evidence, not executing
    assert proposal.trigger_type == "foresight"
    assert proposal.trigger_source == "run:r1"
    # The dedupe key is stamped under parameters._foresight so the
    # cloud-side idempotence check can read it back.
    block = proposal.parameters["_foresight"]
    assert block["dedupe_key"] == "w1|r1|0|decision:renewal|p1"
    assert block["run_id"] == "r1"
    assert block["sub_type"] == "decision_forecast"
    assert block["confidence"] == 0.5


def test_decision_forecast_label_uses_forecast_prefix() -> None:
    proposal = projected_decision_to_instinct_proposal(
        _pd(sub_type="decision_forecast", anchor_id="decision:lease-renewal")
    )
    assert proposal.title.startswith("Forecast:")


def test_market_sim_label_uses_segment_forecast_prefix() -> None:
    proposal = projected_decision_to_instinct_proposal(
        _pd(sub_type="market_sim", anchor_id="segment:enterprise", decision_text="churn")
    )
    assert proposal.title.startswith("Segment forecast:")
    # The anchor "kind:" prefix is stripped for the human label.
    assert "enterprise" in proposal.title


def test_org_change_label_uses_rollout_forecast_prefix() -> None:
    proposal = projected_decision_to_instinct_proposal(
        _pd(
            sub_type="org_change_rehearsal",
            anchor_id="rollout:training",
            decision_text="resist",
        )
    )
    assert proposal.title.startswith("Rollout forecast:")
    assert "training" in proposal.title


def test_unknown_sub_type_falls_back_to_neutral_label() -> None:
    proposal = projected_decision_to_instinct_proposal(_pd(sub_type="future_sub_type_v1"))
    assert proposal.title.startswith("Forecast:")


def test_priority_scales_with_confidence() -> None:
    assert projected_decision_to_instinct_proposal(_pd(confidence=0.95)).priority == "critical"
    assert projected_decision_to_instinct_proposal(_pd(confidence=0.75)).priority == "high"
    assert projected_decision_to_instinct_proposal(_pd(confidence=0.5)).priority == "medium"
    assert projected_decision_to_instinct_proposal(_pd(confidence=0.1)).priority == "low"


def test_scenario_name_appears_in_description_when_supplied() -> None:
    proposal = projected_decision_to_instinct_proposal(
        _pd(),
        scenario_config={"name": "Q3-renewal-forecast"},
    )
    assert "Q3-renewal-forecast" in proposal.description


def test_scenario_config_is_optional() -> None:
    # The bridge stays happy with no scenario_config — the proposal
    # still has a valid description that doesn't mention a scenario.
    proposal = projected_decision_to_instinct_proposal(_pd())
    assert proposal.description  # non-empty


def test_assignee_threads_through() -> None:
    proposal = projected_decision_to_instinct_proposal(_pd(), assignee="user:anne")
    assert proposal.assignee == "user:anne"


def test_assignee_defaults_to_none() -> None:
    proposal = projected_decision_to_instinct_proposal(_pd())
    assert proposal.assignee is None


def test_unknown_run_id_falls_back_to_unknown_pocket_id() -> None:
    proposal = projected_decision_to_instinct_proposal(_pd(run_id=""))
    assert proposal.pocket_id == "foresight:run:unknown"
    assert proposal.trigger_source == "run:unknown"


def test_anchor_without_prefix_passes_through_unchanged() -> None:
    # An anchor that doesn't carry the "kind:" convention should still
    # produce a readable title; the bridge falls back to the raw id.
    proposal = projected_decision_to_instinct_proposal(_pd(anchor_id="raw-anchor"))
    assert "raw-anchor" in proposal.title


def test_forward_precedent_is_preserved_in_provenance_block() -> None:
    proposal = projected_decision_to_instinct_proposal(_pd(forward_precedent_decision_id="dec-123"))
    block = proposal.parameters["_foresight"]
    assert block["forward_precedent_decision_id"] == "dec-123"
