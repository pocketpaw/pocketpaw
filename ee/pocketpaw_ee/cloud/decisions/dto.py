# dto.py — Request / response DTOs for the decision-graph REST surface.
# Created: 2026-05-25 (RFC 07 Slice 1) — skeletons only.
# Updated: 2026-05-25 (RFC 07 Slice 2) — wired the real wire shapes the
#   five REST routes return (get / list / trace / downstream / timeline).
#   Adds `DecisionResponse` (the wire mirror of the domain Decision so
#   wire ≠ domain per ee/cloud Rule 4), `EdgeDTO` (relation-tagged trace
#   edge), `JournalEventDTO` + `TimelineResponse` (the flattened journal
#   chain for one correlation_id), and keeps the original request DTOs
#   the router parses query strings into. Pagination is keyset on
#   `(ts DESC, id DESC)` — the cursor is encoded as opaque
#   `before_ts` / `before_id` query params; the response echoes them
#   back as `next_before_ts` / `next_before_id` for the next page.
# Updated: 2026-05-25 (RFC 07 Slice 3a) — added the explain DTOs
#   (`ExplainRequest`, `ExplanationResponse`) for the
#   `POST /api/v1/decisions/explain` route. Wire ≠ domain (Rule 4): the
#   domain `Explanation` lives in `decisions.explain.narrator`; the wire
#   response trims the field list to what the UI consumes.
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw_ee.cloud.decisions.domain import (
    Decision,
    EdgeRelation,
    OutcomeStatus,
    ScopeKind,
)

# ---------------------------------------------------------------------------
# Approver / input / outcome wire shapes — minor flattenings of the domain
# value objects so the wire schema doesn't leak Pydantic internals.
# ---------------------------------------------------------------------------


class ApproverWire(BaseModel):
    """Wire shape for one approver — flattens the embedded Actor so the
    JSON consumer can read `actor_id` / `actor_kind` without unpacking.
    """

    model_config = ConfigDict(frozen=True)

    actor_kind: str
    actor_id: str
    approved_at: datetime
    position: int = 0


class InputWire(BaseModel):
    """Wire shape for one input — same fields as `InputRef`, no nesting."""

    model_config = ConfigDict(frozen=True)

    kind: str
    id: str
    label: str = ""
    point_in_time: datetime | None = None


class OutcomeWire(BaseModel):
    """Wire shape for the outcome attached to a Decision."""

    model_config = ConfigDict(frozen=True)

    outcome_id: UUID
    status: OutcomeStatus
    landed_at: datetime | None = None
    metered: bool = False


class PrecedentWire(BaseModel):
    """Wire shape for a precedent reference — just the target id + weight."""

    model_config = ConfigDict(frozen=True)

    decision_id: UUID
    weight: float = 1.0


class DecisionResponse(BaseModel):
    """Wire mirror of `decisions.domain.Decision` (ee/cloud Rule 4).

    Renames `decided_by` to `actor_kind` / `actor_id` for symmetry with
    `ApproverWire`. `correlation_id` and `hash_link` are surfaced as
    strings so the client never has to know the SQLite or hash encoding.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    ts: datetime
    actor_kind: str
    actor_id: str
    scope: list[str]
    scope_kind: ScopeKind
    intent: str
    action: str
    inputs: list[InputWire] = Field(default_factory=list)
    approvers: list[ApproverWire] = Field(default_factory=list)
    instinct_policy: str | None = None
    instinct_policy_passed: bool | None = None
    precedents: list[PrecedentWire] = Field(default_factory=list)
    outcome: OutcomeWire | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    pocket_id: str | None = None
    correlation_id: UUID | None = None
    hash_link: str = ""
    last_seq: int = 0

    @classmethod
    def from_domain(cls, decision: Decision) -> DecisionResponse:
        """Build the wire response from a domain Decision. Centralised so
        the five routes share one mapping path (no drift)."""
        return cls(
            id=decision.id,
            ts=decision.ts,
            actor_kind=decision.decided_by.kind,
            actor_id=decision.decided_by.id,
            scope=list(decision.scope),
            scope_kind=decision.scope_kind,
            intent=decision.intent,
            action=decision.action,
            inputs=[
                InputWire(
                    kind=i.kind,
                    id=i.id,
                    label=i.label,
                    point_in_time=i.point_in_time,
                )
                for i in decision.inputs
            ],
            approvers=[
                ApproverWire(
                    actor_kind=a.actor.kind,
                    actor_id=a.actor.id,
                    approved_at=a.approved_at,
                    position=a.position,
                )
                for a in decision.approvers
            ],
            instinct_policy=decision.instinct_policy,
            instinct_policy_passed=decision.instinct_policy_passed,
            precedents=[
                PrecedentWire(decision_id=p.decision_id, weight=p.weight)
                for p in decision.precedents
            ],
            outcome=(
                OutcomeWire(
                    outcome_id=decision.outcome.outcome_id,
                    status=decision.outcome.status,
                    landed_at=decision.outcome.landed_at,
                    metered=decision.outcome.metered,
                )
                if decision.outcome
                else None
            ),
            payload=dict(decision.payload),
            pocket_id=decision.pocket_id,
            correlation_id=decision.correlation_id,
            hash_link=decision.hash_link,
            last_seq=decision.last_seq,
        )


# ---------------------------------------------------------------------------
# GET /api/v1/decisions  (Slice 2)
# ---------------------------------------------------------------------------


class DecisionsListRequest(BaseModel):
    """Validated query for `GET /api/v1/decisions`.

    `workspace_id` is taken from auth context, never the query.
    Pagination is keyset-style (RFC perf budget — never OFFSET at scale):
    `before_ts` + `before_id` carry the cursor from the previous page.
    """

    model_config = ConfigDict(frozen=True)

    actor: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    scope_kind: ScopeKind | None = None
    pocket_id: str | None = None
    policy: str | None = None
    outcome_status: OutcomeStatus | None = None
    input_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    # keyset pagination cursor (sort key: ts DESC, id DESC).
    before_ts: datetime | None = None
    before_id: str | None = None


class DecisionsListResponse(BaseModel):
    """List response. ``total`` is post-scope-filter — never the
    pre-filter count (RFC 07 § Privacy + audit; matches FabricProjection
    invariant). Pagination cursor echoes the last (ts, id) so the
    client can request the next page without recomputing position.
    """

    model_config = ConfigDict(frozen=True)

    decisions: list[DecisionResponse] = Field(default_factory=list)
    total: int = 0
    next_before_ts: datetime | None = None
    next_before_id: str | None = None


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id/trace  (Slice 2)
# ---------------------------------------------------------------------------


class DecisionTraceRequest(BaseModel):
    """Validated query for `GET /api/v1/decisions/:id/trace?depth=N`."""

    model_config = ConfigDict(frozen=True)

    depth: int = Field(default=3, ge=1, le=10)
    max_fanout: int = Field(default=20, ge=1, le=100)


class EdgeDTO(BaseModel):
    """One edge in the trace / downstream response. Mirrors
    `DecisionEdgeRecord` but renames `src_id` → `src` / `target_id` →
    `target` for wire brevity. The five edge relations cover everything
    the narrator + UI need to render (RFC 07 § Edge kinds).
    """

    model_config = ConfigDict(frozen=True)

    src: str
    target: str
    relation: EdgeRelation
    weight: float = 1.0


class TraceNodeResponse(BaseModel):
    """One node in the trace response.

    ``decision`` is populated when the node IS a Decision (the upstream /
    downstream walk hydrated it from the store). For external inputs and
    actor terminals, ``decision`` is None and ``label`` is the only
    identifier the renderer has.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: Literal["decision", "fabric_object", "dataref", "actor"]
    decision: DecisionResponse | None = None
    label: str = ""


class DecisionTraceResponse(BaseModel):
    """BFS trace response (Slice 2). ``truncated`` is set when any node
    exceeded the fanout cap; ``truncated_count`` reports how many edges
    were dropped (RFC 07 amendment for gap G7). Shape is `{root, nodes,
    edges, truncated, truncated_count}` per the RFC spec.
    """

    model_config = ConfigDict(frozen=True)

    root: UUID
    nodes: dict[str, TraceNodeResponse] = Field(default_factory=dict)
    edges: list[EdgeDTO] = Field(default_factory=list)
    truncated: bool = False
    truncated_count: int = 0
    depth_reached: int = 0


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id/timeline  (Slice 2)
# ---------------------------------------------------------------------------


class JournalEventDTO(BaseModel):
    """One event from the journal in the timeline wire shape.

    Slim mirror of `EventEntry` — only the fields the narrator + UI
    timeline view need. `seq` is the journal's monotonic per-org sequence,
    surfaced so the client can sort even when timestamps tie.
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    id: UUID
    ts: datetime
    actor_kind: str
    actor_id: str
    action: str
    scope: list[str] = Field(default_factory=list)
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TimelineResponse(BaseModel):
    """Wire shape for `GET /api/v1/decisions/:id/timeline`.

    Bypasses the projection — reads the journal directly for events
    sharing the Decision's `correlation_id`, returning them in seq
    order. A Decision with no correlation_id (degenerate write) gets
    an empty events list and the response's `correlation_id` is None.
    """

    model_config = ConfigDict(frozen=True)

    decision_id: UUID
    correlation_id: UUID | None = None
    events: list[JournalEventDTO] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# POST /api/v1/decisions/explain  (Slice 3a)
# ---------------------------------------------------------------------------


class ExplainRequest(BaseModel):
    """Validated body for `POST /api/v1/decisions/explain`.

    The natural-language question is required; everything else is
    optional. `backend` lets a per-pocket overlay opt out of the LLM
    narrator (per RFC 07 line 621 — `narrator.backend_pref`); the
    default `None` keeps the orchestrator's "llm with templated
    fallback" behavior.

    `max_decisions` caps how many candidates the extractor's find()
    step pulls. The narrator only walks the top candidate; the cap
    bounds the find() cost not the narration cost.
    """

    model_config = ConfigDict(frozen=False)

    question: str = Field(min_length=1, max_length=2000)
    scope: dict[str, Any] | None = None
    max_decisions: int = Field(default=5, ge=1, le=20)
    depth: int = Field(default=3, ge=1, le=10)
    backend: Literal["llm", "templated"] | None = None


class ExplanationResponse(BaseModel):
    """Wire mirror of `decisions.explain.narrator.Explanation`.

    `ungrounded_sentences` is included so the UI can surface them
    when the verifier strips a hallucinated citation — telemetry
    point for the operator to see how often the narrator
    over-reaches. Empty by default.
    """

    model_config = ConfigDict(frozen=False)

    narrative: str
    decisions_walked: list[UUID] = Field(default_factory=list)
    depth_reached: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    ungrounded_sentences: list[str] = Field(default_factory=list)
    backend_used: Literal["llm", "templated"] = "templated"

    @classmethod
    def from_domain(cls, explanation: Any) -> ExplanationResponse:
        """Build the wire response from a domain Explanation. Single
        mapping path so the REST + MCP surfaces never drift."""
        return cls(
            narrative=explanation.narrative,
            decisions_walked=list(explanation.decisions_walked),
            depth_reached=explanation.depth_reached,
            tokens_in=explanation.tokens_in,
            tokens_out=explanation.tokens_out,
            ungrounded_sentences=list(explanation.ungrounded_sentences),
            backend_used=explanation.backend_used,
        )


__all__ = [
    "ApproverWire",
    "DecisionResponse",
    "DecisionTraceRequest",
    "DecisionTraceResponse",
    "DecisionsListRequest",
    "DecisionsListResponse",
    "EdgeDTO",
    "ExplainRequest",
    "ExplanationResponse",
    "InputWire",
    "JournalEventDTO",
    "OutcomeWire",
    "PrecedentWire",
    "TimelineResponse",
    "TraceNodeResponse",
]
