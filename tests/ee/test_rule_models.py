# tests/ee/test_rule_models.py — unit tests for the discovery RuleDraft model (S2-R1).
#
# Pure-logic coverage of ``pocketpaw_ee.discovery.rule_models.RuleDraft`` — the
# digester output AND the editable ``rule_spec`` blob shape. No DB, no bus.
# Asserts: model_validate accepts a well-formed plain dict and round-trips,
# rejects a bad action (not in InstinctRuleActionT), clamps an out-of-range
# confidence into [0, 1] (silent clamp, mirroring discovery.models._clamp —
# does NOT raise), validates the ``when`` CEL (malformed CEL → ValidationError),
# round-trips scope, and preserves provenance.

from __future__ import annotations

import pytest
from pocketpaw_ee.discovery.rule_models import RuleDraft, RuleScope
from pydantic import ValidationError


def _well_formed_blob() -> dict:
    return {
        "name": "Escalate large refunds",
        "description": "Refunds over $1000 need a human.",
        "when": "amount > 1000",
        "action": "require_approval",
        "scope": {
            "workspace_id": "ws_alpha",
            "pocket_id": "pk_refunds",
            "object_type": "refund",
        },
        "confidence": 0.82,
        "provenance": ["audit:row1", "correction:c2", "audit:row3"],
    }


def test_model_validate_accepts_well_formed_dict() -> None:
    draft = RuleDraft.model_validate(_well_formed_blob())
    assert draft.name == "Escalate large refunds"
    assert draft.when == "amount > 1000"
    assert draft.action == "require_approval"
    assert draft.scope.workspace_id == "ws_alpha"
    assert draft.confidence == pytest.approx(0.82)


def test_round_trips_from_plain_dict() -> None:
    """The blob → RuleDraft → blob round-trip is lossless (the R3 executor
    re-validates the stored ``rule_spec`` dict at the chokepoint)."""
    blob = _well_formed_blob()
    draft = RuleDraft.model_validate(blob)
    dumped = draft.model_dump()
    reloaded = RuleDraft.model_validate(dumped)
    assert reloaded == draft
    # Scope survives the round-trip intact.
    assert reloaded.scope.pocket_id == "pk_refunds"
    assert reloaded.scope.object_type == "refund"


def test_rejects_bad_action() -> None:
    blob = _well_formed_blob()
    blob["action"] = "delete_everything"  # not in InstinctRuleActionT
    with pytest.raises(ValidationError):
        RuleDraft.model_validate(blob)


@pytest.mark.parametrize("action", ["require_approval", "notify", "block"])
def test_accepts_each_valid_action(action: str) -> None:
    blob = _well_formed_blob()
    blob["action"] = action
    draft = RuleDraft.model_validate(blob)
    assert draft.action == action


def test_confidence_out_of_range_clamps_not_raises() -> None:
    """Out-of-range confidence is CLAMPED into [0, 1], not rejected — mirrors
    discovery.models._clamp so a noisy inference score never fails validation."""
    high = RuleDraft.model_validate({**_well_formed_blob(), "confidence": 1.7})
    assert high.confidence == 1.0

    low = RuleDraft.model_validate({**_well_formed_blob(), "confidence": -0.4})
    assert low.confidence == 0.0

    exact = RuleDraft.model_validate({**_well_formed_blob(), "confidence": 0.5})
    assert exact.confidence == pytest.approx(0.5)


def test_when_validates_cel() -> None:
    """A malformed CEL ``when`` raises (CelExpression parses at validation)."""
    blob = _well_formed_blob()
    blob["when"] = "amount >>> "  # not valid CEL
    with pytest.raises(ValidationError):
        RuleDraft.model_validate(blob)


def test_when_rejects_empty_string() -> None:
    blob = _well_formed_blob()
    blob["when"] = ""
    with pytest.raises(ValidationError):
        RuleDraft.model_validate(blob)


def test_scope_round_trips() -> None:
    scope = RuleScope(workspace_id="ws_x", pocket_id=None, object_type=None)
    assert scope.workspace_id == "ws_x"
    assert scope.pocket_id is None
    assert scope.object_type is None
    # Nested round-trip through the draft.
    draft = RuleDraft.model_validate(
        {
            "name": "n",
            "when": "true",
            "action": "notify",
            "scope": {"workspace_id": "ws_x"},
            "confidence": 0.1,
        }
    )
    assert draft.scope.workspace_id == "ws_x"
    assert draft.scope.pocket_id is None


def test_scope_requires_workspace_id() -> None:
    """Tenancy lives in scope.workspace_id — a draft without it is invalid."""
    blob = _well_formed_blob()
    blob["scope"] = {"pocket_id": "pk_x"}  # missing workspace_id
    with pytest.raises(ValidationError):
        RuleDraft.model_validate(blob)


def test_provenance_preserved() -> None:
    blob = _well_formed_blob()
    blob["provenance"] = ["audit:a", "audit:b", "correction:c"]
    draft = RuleDraft.model_validate(blob)
    assert draft.provenance == ["audit:a", "audit:b", "correction:c"]


def test_provenance_defaults_empty() -> None:
    blob = _well_formed_blob()
    del blob["provenance"]
    draft = RuleDraft.model_validate(blob)
    assert draft.provenance == []


def test_description_optional() -> None:
    blob = _well_formed_blob()
    del blob["description"]
    draft = RuleDraft.model_validate(blob)
    assert draft.description is None
