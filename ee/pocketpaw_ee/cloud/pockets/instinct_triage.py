# ee/pocketpaw_ee/cloud/pockets/instinct_triage.py
# Created: 2026-06-18 (feat/instinct-gate-foundation, T1) — the pure,
# synchronous lane classifier for the layered/learning Instinct gate
# (2026-06-18 gate-layered-learning design). Turns a binary
# escalate/execute gate into a 4-lane router (DRY_RUN / AUTO / OPTIMISTIC /
# BATCH / ESCALATE) using three signals: reversibility (CompensateSpec
# presence), blast radius (DELETE + financial keyword heuristic), and the
# per-(pocket, action) trust score + proposed_count from trust_ledger.
#
# PURE: no I/O, no DB, no clock. ``classify_lane`` is a total function of
# its ``TriageProposal`` + the ``dry_run_mode`` flag. This module is on the
# import-linter "Pockets" allowlist (no Beanie writes) — it only imports
# ``CompensateSpec`` from action_executor (itself import-linter-pure).
#
# DEFAULT-SAFE: every uncertain branch returns ESCALATE. The classifier is
# DORMANT until the integration layer (separate gated PR) passes a non-ASK
# ``approval_level`` — under the default ApprovalLevel.ASK rule 2 forces
# ESCALATE for every proposal, so this module changes zero behavior on its
# own. A high-blast action (DELETE or financial path) can reach OPTIMISTIC
# at best, never AUTO — a CompensateSpec is not proof of reversibility for
# money/DELETE.

"""Pure lane classifier for the layered/learning Instinct gate."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pocketpaw_ee.cloud.pockets.action_executor import CompensateSpec

# Financial-action keywords. A write whose path contains any of these is
# treated as high-blast regardless of HTTP method — a CompensateSpec may
# move it to OPTIMISTIC but never to AUTO. Hardcoded here as the moat (see
# design open-question #3 — defaulted, pending captain; a per-workspace
# override list is a possible future change). Matching is case-insensitive
# substring on the path.
_FINANCIAL_KEYWORDS: tuple[str, ...] = (
    "charge",
    "refund",
    "cancel",
    "payment",
    "invoice",
    "withdraw",
    "subscription",
    "billing",
)

# The trust bar mirrors the config default ``instinct_auto_approve_threshold``
# (0.9). It lives here as a module constant so the pure classifier has no
# config dependency; the integration layer asserts the two stay in sync.
_THRESHOLD: float = 0.9


class TriageLane(IntEnum):
    """The lane a proposal routes to.

    Ordering is by escalation severity (0 = most-governed dry run … 4 =
    fully-escalated human queue). ``IntEnum`` so audit rows can store the
    int and dashboards can sort, while the name stays the wire/audit label.
    """

    DRY_RUN = 0
    AUTO = 1
    OPTIMISTIC = 2
    BATCH = 3
    ESCALATE = 4


class ApprovalLevel(StrEnum):
    """Per-workspace triager activation level.

    * ``ASK`` — triager dormant. Every escalate goes to a human. This is
      the global default; under it ``classify_lane`` always returns
      ESCALATE (rule 2), so shipping the classifier changes nothing.
    * ``TRIAGE`` — triager active; lanes 0-4 all live.
    * ``TRUSTED`` — reserved. In this foundation it is treated IDENTICALLY
      to ``TRIAGE`` (no behavior difference yet — design open-question #1,
      defaulted pending captain).
    """

    ASK = "ASK"
    TRIAGE = "TRIAGE"
    TRUSTED = "TRUSTED"


class TriageProposal(BaseModel):
    """The immutable input to ``classify_lane`` for one (action, row).

    Frozen so a caller cannot mutate the signals after construction. The
    classifier reads only these fields plus the ``dry_run_mode`` flag — it
    is a pure function of this object.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    action: str
    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    path: str
    compensate: CompensateSpec | None
    trust_score: float
    proposed_count: int
    instinct_verdict: str
    approval_level: ApprovalLevel


def _is_high_blast(method: str, path: str) -> bool:
    """True for irreversible / money-moving writes.

    A high-blast action cannot reach AUTO even with a CompensateSpec — the
    compensate signal can only lift it to OPTIMISTIC. Two triggers:

    * ``DELETE`` method — a destructive verb. A compensate (undelete) is
      rarely a true inverse, so DELETE never auto-fires.
    * a financial keyword anywhere in the path (case-insensitive). Money
      movement is the canonical "review before it fires" surface.
    """
    if method == "DELETE":
        return True
    low = path.lower()
    return any(kw in low for kw in _FINANCIAL_KEYWORDS)


def classify_lane(proposal: TriageProposal, *, dry_run_mode: bool = False) -> TriageLane:
    """Route one proposal to a lane. Pure; default-safe (ESCALATE on doubt).

    Rules are evaluated in priority order — the first match wins:

    1. ``instinct_verdict == "BLOCK"`` → ESCALATE (hard floor, no override).
    2. ``approval_level == ASK`` → ESCALATE (triager dormant).
    3. ``dry_run_mode`` → DRY_RUN (governance rehearsal).
    4. ``proposed_count == 0`` → ESCALATE (cold-start floor; warmup of >=1
       human-approved execution required before any auto/optimistic route).
    5. high-blast AND no compensate → ESCALATE.
    6. high-blast AND compensate present → OPTIMISTIC if trust clears the
       implicit bar, else ESCALATE (never AUTO — blast-radius floor).
    7. trust >= threshold AND compensate present (low-blast) → AUTO.
    8. trust >= threshold AND no compensate AND low-blast → OPTIMISTIC.
    9. trust < threshold AND compensate present (low-blast) → OPTIMISTIC.
    10. else → ESCALATE.

    "Trusted" means ``trust_score >= _THRESHOLD`` (the module constant that
    mirrors the config ``instinct_auto_approve_threshold`` default of 0.9).
    The bar is a module constant rather than a parameter so the classifier
    stays a pure, config-free total function; the integration layer asserts
    the constant and the config default stay in sync.
    """
    # Rule 1 — BLOCK is the absolute safety floor.
    if proposal.instinct_verdict == "BLOCK":
        return TriageLane.ESCALATE

    # Rule 2 — triager dormant.
    if proposal.approval_level == ApprovalLevel.ASK:
        return TriageLane.ESCALATE

    # Rule 3 — dry-run rehearsal mode (only after BLOCK/ASK floors).
    if dry_run_mode:
        return TriageLane.DRY_RUN

    # Rule 4 — cold-start floor: no prior human-approved data → escalate.
    if proposal.proposed_count == 0:
        return TriageLane.ESCALATE

    trusted = proposal.trust_score >= _THRESHOLD
    has_compensate = proposal.compensate is not None

    # DELETE is the hardest blast-radius floor: a delete is irreversible in
    # a way a compensate (undelete) cannot truthfully repair, so it NEVER
    # reaches AUTO *or* OPTIMISTIC — it always escalates to a human (T-01,
    # T-09). This is stricter than the financial-keyword floor below, which
    # a CompensateSpec CAN lift to OPTIMISTIC.
    if proposal.method == "DELETE":
        return TriageLane.ESCALATE

    # Rules 5-6 — financial-keyword high-blast floor. Compensate can lift to
    # OPTIMISTIC at best; without it, escalate. Never AUTO.
    if _is_high_blast(proposal.method, proposal.path):
        if not has_compensate:
            return TriageLane.ESCALATE
        # compensate present on a high-blast (financial) action → OPTIMISTIC
        # when trusted, else ESCALATE.
        return TriageLane.OPTIMISTIC if trusted else TriageLane.ESCALATE

    # Rule 7 — low-blast, trusted, reversible → AUTO.
    if trusted and has_compensate:
        return TriageLane.AUTO

    # Rule 8 — low-blast, trusted, no compensate → OPTIMISTIC.
    if trusted and not has_compensate:
        return TriageLane.OPTIMISTIC

    # Rule 9 — low-blast, below threshold, reversible → OPTIMISTIC.
    if not trusted and has_compensate:
        return TriageLane.OPTIMISTIC

    # Rule 10 — everything else escalates.
    return TriageLane.ESCALATE


__all__ = [
    "ApprovalLevel",
    "TriageLane",
    "TriageProposal",
    "classify_lane",
]
