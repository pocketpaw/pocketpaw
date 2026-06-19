# tests/cloud/test_instinct_triage.py
# Created: 2026-06-18 (feat/instinct-gate-foundation, T1) — pure unit tests
# for the layered/learning Instinct gate's lane classifier. No DB, no I/O.
# Pins the 10 priority-ordered lane rules + the _is_high_blast floor from
# the 2026-06-18 gate-layered-learning design's Contracts section. Cases
# T-01..T-09 from the design's Test plan. The classifier is DORMANT by
# default: ApprovalLevel.ASK forces ESCALATE so nothing auto-approves until
# the integration layer (separate PR) sets a non-ASK level.

from __future__ import annotations

from pocketpaw_ee.cloud.pockets.action_executor import CompensateSpec
from pocketpaw_ee.cloud.pockets.instinct_triage import (
    ApprovalLevel,
    TriageLane,
    TriageProposal,
    classify_lane,
)


def _compensate() -> CompensateSpec:
    return CompensateSpec(method="POST", path="/refund", params={})


def _proposal(**overrides) -> TriageProposal:
    """Build a TriageProposal with sensible TRIAGE-mode defaults.

    Defaults describe a low-blast, reversible, warm (proposed_count>=1),
    high-trust POST that an EXECUTE-floor verdict escalated — i.e. the
    happy AUTO case — so each test overrides only the signal it pins.
    """
    base: dict = {
        "action": "do_thing",
        "method": "POST",
        "path": "/items",
        "compensate": _compensate(),
        "trust_score": 0.95,
        "proposed_count": 5,
        "instinct_verdict": "ESCALATE_APPROVAL",
        "approval_level": ApprovalLevel.TRIAGE,
    }
    base.update(overrides)
    return TriageProposal(**base)


# T-01: irreversible high-blast always escalates regardless of trust.
def test_t01_delete_no_compensate_high_trust_escalates() -> None:
    p = _proposal(
        method="DELETE",
        path="/items/42",
        compensate=None,
        trust_score=1.0,
        proposed_count=99,
    )
    assert classify_lane(p) == TriageLane.ESCALATE


# T-02: reversible POST, high trust, warm, TRIAGE → AUTO.
def test_t02_post_compensate_high_trust_warm_auto() -> None:
    p = _proposal(
        method="POST",
        path="/items",
        trust_score=0.95,
        proposed_count=5,
    )
    assert classify_lane(p) == TriageLane.AUTO


# T-03: reversible POST, below threshold, warm → OPTIMISTIC.
def test_t03_post_compensate_low_trust_optimistic() -> None:
    p = _proposal(
        method="POST",
        path="/items",
        trust_score=0.4,
        proposed_count=3,
    )
    assert classify_lane(p) == TriageLane.OPTIMISTIC


# T-04: ASK level forces ESCALATE for any proposal (any trust, any method).
def test_t04_ask_level_always_escalates() -> None:
    p = _proposal(
        approval_level=ApprovalLevel.ASK,
        method="POST",
        trust_score=1.0,
        proposed_count=99,
    )
    assert classify_lane(p) == TriageLane.ESCALATE


# T-05: BLOCK verdict → ESCALATE regardless of trust, level, compensate.
def test_t05_block_verdict_escalates_even_at_trusted() -> None:
    p = _proposal(
        instinct_verdict="BLOCK",
        approval_level=ApprovalLevel.TRUSTED,
        trust_score=1.0,
        proposed_count=99,
    )
    assert classify_lane(p) == TriageLane.ESCALATE


# T-06: cold-start floor — proposed_count=0 forces ESCALATE even with high
# trust + compensate at TRIAGE.
def test_t06_cold_start_zero_count_escalates() -> None:
    p = _proposal(
        proposed_count=0,
        trust_score=0.99,
    )
    assert classify_lane(p) == TriageLane.ESCALATE


# T-07: financial path + PATCH + compensate + high trust → OPTIMISTIC (not
# AUTO — blast-radius floor for financial paths).
def test_t07_financial_path_high_trust_optimistic_not_auto() -> None:
    p = _proposal(
        method="PATCH",
        path="/subscriptions/cancel",
        trust_score=0.95,
        proposed_count=5,
    )
    assert classify_lane(p) == TriageLane.OPTIMISTIC


# T-08: dry_run_mode=True → DRY_RUN regardless of lane (but BLOCK/ASK still
# take priority — see T-29; here verdict is the escalate floor).
def test_t08_dry_run_mode_returns_dry_run() -> None:
    p = _proposal()
    assert classify_lane(p, dry_run_mode=True) == TriageLane.DRY_RUN


# T-09: DELETE is high-blast; a CompensateSpec cannot promote DELETE to AUTO
# or OPTIMISTIC.
def test_t09_delete_with_compensate_high_trust_still_escalates() -> None:
    p = _proposal(
        method="DELETE",
        path="/items/42",
        compensate=_compensate(),
        trust_score=1.0,
        proposed_count=20,
    )
    assert classify_lane(p) == TriageLane.ESCALATE


# --- extra coverage for the remaining rules (rule 8: optimistic when
# reversible+nothing else fires; AUTO requires compensate) ---


def test_high_trust_no_compensate_low_blast_optimistic() -> None:
    # rule 8: trust>=threshold AND compensate is None AND not high-blast
    p = _proposal(
        method="POST",
        path="/items",
        compensate=None,
        trust_score=0.95,
        proposed_count=5,
    )
    assert classify_lane(p) == TriageLane.OPTIMISTIC


def test_low_trust_no_compensate_low_blast_escalates() -> None:
    # rule 10 (else): low trust, no compensate, not high-blast → ESCALATE
    p = _proposal(
        method="POST",
        path="/items",
        compensate=None,
        trust_score=0.3,
        proposed_count=5,
    )
    assert classify_lane(p) == TriageLane.ESCALATE


def test_dry_run_mode_does_not_override_block() -> None:
    # BLOCK is the hard floor (rule 1) — even dry_run_mode cannot move it.
    p = _proposal(instinct_verdict="BLOCK")
    assert classify_lane(p, dry_run_mode=True) == TriageLane.ESCALATE


def test_dry_run_mode_does_not_override_ask() -> None:
    # ASK (rule 2) takes priority over dry_run_mode (rule 3).
    p = _proposal(approval_level=ApprovalLevel.ASK)
    assert classify_lane(p, dry_run_mode=True) == TriageLane.ESCALATE


def test_proposal_is_frozen() -> None:
    import pytest
    from pydantic import ValidationError

    p = _proposal()
    with pytest.raises(ValidationError):
        p.trust_score = 0.1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DELETE-floor guard (security-review FIX 2). The explicit DELETE check in
# classify_lane is the hardest blast-radius floor: a DELETE must NEVER reach
# OPTIMISTIC or AUTO — not even with a CompensateSpec AND perfect trust AND a
# warm history. A future refactor that "simplifies" the DELETE check into
# _is_high_blast would silently let a compensate lift a DELETE to OPTIMISTIC
# (the financial-keyword floor permits that). This test pins the floor so that
# refactor fails loudly. Do NOT delete or weaken it.
# ---------------------------------------------------------------------------


def test_delete_floor_compensate_perfect_trust_triage_still_escalates() -> None:
    """DELETE + CompensateSpec + trust_score=1.0 + TRIAGE + proposed_count=5
    → ESCALATE. The delete floor overrides every promotion signal: an
    "undelete" compensate is not a truthful inverse, so a DELETE always goes
    to a human regardless of trust or warmup. Pins design rule that the
    DELETE branch stays SEPARATE from _is_high_blast (the financial floor)."""
    p = _proposal(
        method="DELETE",
        path="/items/42",
        compensate=_compensate(),
        trust_score=1.0,
        proposed_count=5,
        approval_level=ApprovalLevel.TRIAGE,
    )
    assert classify_lane(p) == TriageLane.ESCALATE


def test_delete_floor_holds_at_trusted_level() -> None:
    """Even at the (reserved) TRUSTED level, DELETE + compensate + max trust
    escalates — the floor is independent of activation level."""
    p = _proposal(
        method="DELETE",
        path="/items/42",
        compensate=_compensate(),
        trust_score=1.0,
        proposed_count=99,
        approval_level=ApprovalLevel.TRUSTED,
    )
    assert classify_lane(p) == TriageLane.ESCALATE
