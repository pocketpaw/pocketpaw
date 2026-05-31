# domain.py — Frozen value objects for the decision-graph entity.
# Created: 2026-05-25 (RFC 07 Slice 1) — the load-bearing types for the
#   whole Decision projection / query pipeline. Every other module hangs
#   off these.
#
#   The Decision is the fold of an N-event journal subgraph into one
#   queryable record. Hash-chained within `correlation_id` so tampering
#   with one Decision invalidates every later one in the chain. Outcome
#   is intentionally NOT in the hash so a late-landing
#   `decision.outcome_attached` event can mutate it without breaking the
#   chain.
#
#   Amendments applied (RFC 07 prototype-derived):
#   - A1 (gap G1): `approvers` is `list[ApproverRef]`, not `list[Actor]`.
#     `ApproverRef` carries `approved_at` + `position` so the SQL schema's
#     `decision_approvers.approved_at` / `.position` columns have a domain
#     home. Without this, every implementer rediscovers it on day 1.
#   - G6 hash composition: see `_hash_material_v1` doc-comment below.
#     The decision id (UUID v4) is the primary collision-defeater; the
#     remaining fields are content-bound for tampering detection.
#     `prev_hash` is included in the material so the chain links within
#     `correlation_id`.
#
#   Multi-tenancy: `scope: list[str]` is required (min_length=1) per
#   ee/cloud Rule 3 (domain enforces multi-tenancy at construction). A
#   Decision cannot exist without at least one scope tag — the journal
#   already requires it on every EventEntry.
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from soul_protocol.spec.journal import Actor

# ---------------------------------------------------------------------------
# Type aliases mirroring the RFC's `ScopeKind`, `OutcomeStatus`,
# `EdgeRelation` enums.
# ---------------------------------------------------------------------------

ScopeKind = Literal["workspace", "org", "pocket", "team"]
OutcomeStatus = Literal["pending", "landed", "rejected", "abandoned"]
EdgeRelation = Literal["precedent", "input", "approval", "outcome", "downstream"]
InputKind = Literal["fabric_object", "dataref", "decision"]
DecisionRelation = Literal["precedent", "downstream", "input"]


# ---------------------------------------------------------------------------
# Value objects on the Decision (RFC 07 "The Decision Object" section)
# ---------------------------------------------------------------------------


class InputRef(BaseModel):
    """A fact considered when this decision was made (RFC lines 86-103).

    Three input shapes:
      - ``fabric_object`` — a Fabric record (Lease, Case, Patient, Vendor).
      - ``dataref`` — a Zero-Copy reference (Salesforce row, Drive doc,
        S3 object). Carries ``point_in_time`` because mutable inputs need
        a snapshot the trace can anchor to.
      - ``decision`` — a prior decision that literally fed data into
        this one. Distinct from ``precedents`` (similar-shape templates).
    """

    model_config = ConfigDict(frozen=True)

    kind: InputKind
    id: str = Field(min_length=1)
    label: str = ""
    point_in_time: datetime | None = None


class DecisionRef(BaseModel):
    """A reference to another decision (RFC lines 105-116).

    Used for ``precedents`` and the inverse ``downstream`` relation. Stored
    as a denormalized edge row so trace traversal is index-driven, not a
    recursive SQL CTE.
    """

    model_config = ConfigDict(frozen=True)

    decision_id: UUID
    relation: DecisionRelation
    weight: float = 1.0  # bigger = stronger precedent (used by explain ranker)


class OutcomeRef(BaseModel):
    """The outcome a decision resolved to (RFC lines 118-130).

    ``None`` on initial Decision construction; populated by a later
    ``decision.outcome_attached`` journal event. The hash chain excludes
    outcome precisely so this mutation is safe.
    """

    model_config = ConfigDict(frozen=True)

    outcome_id: UUID
    status: OutcomeStatus
    landed_at: datetime | None = None
    metered: bool = False


class ApproverRef(BaseModel):
    """An approval recorded on the Decision (RFC 07 amendment A1, gap G1).

    The RFC's draft `Decision.approvers: list[Actor]` left no place to
    land the `approved_at` timestamp that the SQL `decision_approvers`
    table requires. `ApproverRef` lifts that field onto the domain so
    Pydantic catches a missing timestamp at construction.

    ``position`` is the approver's order in the chain — first approver = 0.
    Used so "who approved first" is a query, not a guess.
    """

    model_config = ConfigDict(frozen=True)

    actor: Actor
    approved_at: datetime
    position: int = 0


# ---------------------------------------------------------------------------
# The Decision itself
# ---------------------------------------------------------------------------


class Decision(BaseModel):
    """A first-class queryable decision (RFC 07 lines 132-168).

    The fold of an N-event subgraph of the journal into one record.
    Construction happens inside ``DecisionProjection._close_chain`` when a
    terminal event (per A2: only ``decision.completed``) lands on a
    correlation_id that previously received ``agent.proposed``.

    Multi-tenancy: ``scope`` is required (min_length=1). The journal
    requires a non-empty scope on every EventEntry; the Decision inherits
    that invariant at construction time.

    Mutability: ``model_config = ConfigDict(frozen=False)`` is deliberate.
    ``outcome`` mutates after first emit when ``decision.outcome_attached``
    lands — the *only* post-emit mutation. The hash_link is computed
    *without* outcome so the chain stays valid across that mutation.
    """

    model_config = ConfigDict(frozen=False)

    id: UUID
    ts: datetime  # tz-aware UTC; matches EventEntry.ts semantics
    decided_by: Actor  # agent | user | system | root — reuses journal's Actor
    scope: list[str] = Field(min_length=1)
    scope_kind: ScopeKind = "pocket"
    intent: str = Field(min_length=1)
    action: str = Field(min_length=1)
    inputs: list[InputRef] = Field(default_factory=list)
    approvers: list[ApproverRef] = Field(default_factory=list)  # A1 (gap G1)
    instinct_policy: str | None = None
    instinct_policy_passed: bool | None = None
    precedents: list[DecisionRef] = Field(default_factory=list)
    outcome: OutcomeRef | None = None
    payload: dict = Field(default_factory=dict)
    pocket_id: str | None = None
    correlation_id: UUID | None = None
    hash_link: str = ""
    last_seq: int = 0


# ---------------------------------------------------------------------------
# Edge records (mirror the `decision_edges` SQLite schema in RFC § The
# materialized store).
# ---------------------------------------------------------------------------


class DecisionEdgeRecord(BaseModel):
    """One row in the `decision_edges` table — five edge kinds, all explicit.

    The forward edge for ``downstream`` is **never written**; the inverse
    of a ``precedent`` edge answers downstream queries via the
    ``(target_id, relation='precedent')`` index. This keeps writes O(1)
    per Decision while reads stay index-driven.
    """

    model_config = ConfigDict(frozen=True)

    src_id: UUID
    target_id: str  # string because targets can be UUID *or* fabric_object id
    relation: EdgeRelation
    weight: float = 1.0


# ---------------------------------------------------------------------------
# Hash chain composition (RFC 07 amendment, gap G6)
# ---------------------------------------------------------------------------


def compute_hash_link(decision: Decision, prev_hash: str) -> str:
    """Compute a decision's hash link per RFC 07 (amended for gap G6).

    Composition:
      sha256(id || ts || decided_by.id || action || sorted(input_ids) || prev_hash)

    Why each part:
      - ``id`` — UUID v4, primary collision-defeater. Two decisions with
        identical content but different ids hash to different values.
      - ``ts`` (ISO-8601) — content-bound; tampering with the timestamp
        invalidates the hash.
      - ``decided_by.id`` — content-bound; rewriting the actor breaks
        the chain.
      - ``action`` — content-bound; renaming the action breaks the chain.
      - ``sorted(input_ids)`` — content-bound; swapping inputs breaks
        the chain. Sort so ordering of `inputs[]` is irrelevant to the
        hash.
      - ``prev_hash`` — chain link within ``correlation_id``. Tampering
        with one Decision invalidates every later Decision in the same
        correlation chain.

    Why some fields are **excluded** from the hash:
      - ``outcome`` — mutates after first emit (RFC 07 line 144). If
        outcome were in the hash, the late-landing
        ``decision.outcome_attached`` event would invalidate the chain.
      - ``approvers`` — the RFC excludes; in practice approvers land
        before the terminal event so they could be in the hash, but
        keeping them out keeps the chain symmetric with the
        outcome-late-attach case.
      - ``payload`` — opaque to the graph; not indexed; not hashed.
      - ``precedents`` — supplied by the proposer; A3 fallback can add
        more after the fact (same-pocket / same-action / nearest-ts).
        Excluding from the hash keeps the precedent-fallback path safe.
    """

    h = hashlib.sha256()
    h.update(str(decision.id).encode())
    h.update(decision.ts.isoformat().encode())
    h.update(decision.decided_by.id.encode())
    h.update(decision.action.encode())
    for input_id in sorted(i.id for i in decision.inputs):
        h.update(input_id.encode())
    if prev_hash:
        h.update(prev_hash.encode())
    return h.hexdigest()


__all__ = [
    "ApproverRef",
    "Decision",
    "DecisionEdgeRecord",
    "DecisionRef",
    "DecisionRelation",
    "EdgeRelation",
    "InputKind",
    "InputRef",
    "OutcomeRef",
    "OutcomeStatus",
    "ScopeKind",
    "compute_hash_link",
]
