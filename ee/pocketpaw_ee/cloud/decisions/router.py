# router.py — FastAPI router for the decision-graph entity (RFC 07).
# Created: 2026-05-25 (RFC 07 Slice 1) — skeleton ping route only.
# Updated: 2026-05-25 (RFC 07 Slice 2 — post-filter total) — list endpoint
#   now sources ``total`` from ``DecisionGraph.count`` (post-scope-filter)
#   instead of ``len(decisions)`` (page size), and only echoes the
#   keyset cursor when the returned page is full. Page-size totals
#   defeat the anti-probe property RFC 07 § Privacy requires; partial-
#   page cursor echoes give clients a phantom "next page" that always
#   returns empty.
# Updated: 2026-05-25 (RFC 07 Slice 2) — wires the five real read routes
#   that the RFC pins:
#
#     GET  /api/v1/decisions/:id            → DecisionResponse
#     GET  /api/v1/decisions                 → DecisionsListResponse
#     GET  /api/v1/decisions/:id/trace      → DecisionTraceResponse
#     GET  /api/v1/decisions/:id/downstream → DecisionTraceResponse
#     GET  /api/v1/decisions/:id/timeline   → TimelineResponse
#
#   The `_ping` smoke route stays — operators rely on it as a cheap
#   liveness check on the projection cursor + row count.
# Updated: 2026-05-25 (RFC 07 Slice 3a) — added the natural-language
#   explain route:
#
#     POST /api/v1/decisions/explain        → ExplanationResponse
#
#   Pipeline: extractor → find → trace → narrator → cache. Scope filter
#   is the same load-bearing invariant the read routes enforce; a
#   workspace caller cannot probe another workspace's decisions through
#   the question.
#
# Router contract (mirrors `outcomes/router.py` and `pockets/router.py`):
#   - Thin one-line bodies that delegate to `DecisionGraph` or read the
#     journal directly (timeline only).
#   - `Depends(request_context)` for `RequestContext` — `workspace_id`
#     becomes the scope tag the service filters on.
#   - Never `raise HTTPException`; `_core.http` maps `CloudError` to
#     JSON. The Decision graph's scope filter returns None for both
#     "missing" and "scope-hidden" — the router maps None to 404 with
#     the same code so a caller cannot distinguish the two states (RFC
#     07 § Privacy + audit).
from __future__ import annotations

import base64
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from soul_protocol.engine.journal import Journal

from pocketpaw.journal_dep import get_journal
from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
from pocketpaw_ee.cloud.decisions.dto import (
    DecisionResponse,
    DecisionsListResponse,
    DecisionTraceResponse,
    EdgeDTO,
    ExplainRequest,
    ExplanationResponse,
    JournalEventDTO,
    TimelineResponse,
    TraceNodeResponse,
)
from pocketpaw_ee.cloud.decisions.explain import ExplainRequestInput, explain
from pocketpaw_ee.cloud.decisions.service import (
    DecisionGraph,
    TraceResult,
    get_decision_graph,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/decisions",
    tags=["Decisions"],
    dependencies=[Depends(require_license)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _requester_scopes(ctx: RequestContext) -> list[str] | None:
    """Build the scope-tag list from the request context.

    The decision graph stores `scope` tags on every Decision (e.g.
    `workspace:abc`, `pocket:p1`, `org:nerve`). A caller with
    `active_workspace == "abc"` is allowed to see decisions tagged
    `workspace:abc` — that's the only tag the auth context can
    contribute today.

    A caller with no active workspace gets an empty list (treated as
    "no scope" by the service — sees nothing) rather than `None`
    (admin / unscoped). This is the conservative default; future
    work will widen scope inference once cross-pocket / cross-team
    membership lands.
    """
    if ctx.workspace_id:
        return [f"workspace:{ctx.workspace_id}"]
    return []


def _encode_cursor(ts: datetime, decision_id: UUID) -> str:
    """Encode `(ts, id)` as an opaque base64 cursor for the next-page link.

    The cursor format is `<iso_ts>|<uuid>` base64-encoded so the wire
    string is URL-safe and the client doesn't need to know the shape.
    Decoding lives in `_decode_cursor`.
    """
    raw = f"{ts.isoformat()}|{decision_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _maybe_uuid(value: str) -> UUID:
    """Parse a UUID query / path param or raise a CloudError (400).

    Routers must never raise HTTPException; this helper keeps the path
    handlers thin while giving a clean error envelope on bad input.
    """
    try:
        return UUID(value)
    except (ValueError, TypeError) as exc:
        raise CloudError(
            400, "decisions.invalid_id", f"'{value}' is not a valid UUID"
        ) from exc


def _trace_to_response(result: TraceResult) -> DecisionTraceResponse:
    """Map the service-layer TraceResult into the wire trace DTO."""
    return DecisionTraceResponse(
        root=result.root,
        nodes={
            node_id: TraceNodeResponse(
                id=node.id,
                kind=node.kind,
                decision=DecisionResponse.from_domain(node.decision) if node.decision else None,
                label=node.label,
            )
            for node_id, node in result.nodes.items()
        },
        edges=[
            EdgeDTO(
                src=str(e.src_id),
                target=e.target_id,
                relation=e.relation,
                weight=e.weight,
            )
            for e in result.edges
        ],
        truncated=result.truncated,
        truncated_count=result.truncated_count,
        depth_reached=result.depth_reached,
    )


# ---------------------------------------------------------------------------
# Smoke / liveness — kept from Slice 1
# ---------------------------------------------------------------------------


@router.get("/_ping")
async def decisions_ping() -> dict[str, Any]:
    """Smoke endpoint — projection cursor + total row count.

    Kept from Slice 1 for operator liveness checks. Unauthenticated
    beyond the license gate so an operator can curl it.
    """
    graph = get_decision_graph()
    return {
        "status": "ok",
        "cursor": graph.projection.cursor,
        "decisions": graph.store.count(),
    }


# ---------------------------------------------------------------------------
# GET /api/v1/decisions — list (filtered, keyset-paginated)
# ---------------------------------------------------------------------------


@router.get("", response_model=DecisionsListResponse)
async def list_decisions(
    request: Request,
    actor: str | None = Query(default=None, description="Decided-by actor id"),
    since: datetime | None = Query(default=None, description="ISO-8601 lower bound on ts"),
    until: datetime | None = Query(default=None, description="ISO-8601 upper bound on ts"),
    scope_kind: str | None = Query(default=None, description="workspace|org|pocket|team"),
    pocket_id: str | None = Query(default=None),
    policy: str | None = Query(default=None, description="Instinct policy name"),
    outcome_status: str | None = Query(
        default=None, description="pending|landed|rejected|abandoned"
    ),
    input_id: str | None = Query(default=None, description="Filter to decisions citing this input"),
    limit: int = Query(default=50, ge=1, le=200),
    before_ts: datetime | None = Query(default=None, description="Keyset cursor (ts)"),
    before_id: str | None = Query(default=None, description="Keyset cursor (id)"),
    ctx: RequestContext = Depends(request_context),
) -> DecisionsListResponse:
    """List recent decisions for the caller's scope.

    Workspace tenancy is derived from the auth context — the route
    rejects a `workspace_id` query param so a caller cannot read another
    workspace's decisions. Pagination is keyset on `(ts DESC, id DESC)`
    — `next_before_ts` / `next_before_id` echo the last row's sort
    key so the client can request the next page without OFFSET.
    """
    if "workspace_id" in request.query_params:
        raise CloudError(
            400,
            "decisions.workspace_id_forbidden",
            "workspace_id is taken from auth context, not query",
        )

    graph: DecisionGraph = get_decision_graph()
    scopes = _requester_scopes(ctx)
    decisions = await graph.find(
        actor=actor,
        since=since,
        until=until,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        pocket_id=pocket_id,
        policy=policy,
        outcome_status=outcome_status,  # type: ignore[arg-type]
        input_id=input_id,
        limit=limit,
        before_ts=before_ts,
        before_id=before_id,
        requester_scopes=scopes,
    )
    total = await graph.count(
        actor=actor,
        since=since,
        until=until,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        pocket_id=pocket_id,
        policy=policy,
        outcome_status=outcome_status,  # type: ignore[arg-type]
        input_id=input_id,
        requester_scopes=scopes,
    )

    # Only echo the cursor when the page was full. A short page is the
    # last page — echoing a cursor here would give the client a phantom
    # next page that always returns empty.
    next_ts: datetime | None = None
    next_id: str | None = None
    if decisions and len(decisions) == limit:
        last = decisions[-1]
        next_ts = last.ts
        next_id = str(last.id)

    return DecisionsListResponse(
        decisions=[DecisionResponse.from_domain(d) for d in decisions],
        total=total,
        next_before_ts=next_ts,
        next_before_id=next_id,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id — single lookup
# ---------------------------------------------------------------------------


@router.get("/{decision_id}", response_model=DecisionResponse)
async def get_decision(
    decision_id: str,
    ctx: RequestContext = Depends(request_context),
) -> DecisionResponse:
    """Return one Decision.

    Returns 404 with `decisions.not_found` for both genuinely missing
    Decisions and Decisions outside the caller's scope — the two states
    are deliberately indistinguishable so a caller cannot probe for
    hidden rows (RFC 07 § Privacy + audit).
    """
    decision_uuid = _maybe_uuid(decision_id)
    graph: DecisionGraph = get_decision_graph()
    decision = await graph.get(decision_uuid, requester_scopes=_requester_scopes(ctx))
    if decision is None:
        raise NotFound("decisions", decision_id)
    return DecisionResponse.from_domain(decision)


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id/trace — upstream walk (precedents + inputs)
# ---------------------------------------------------------------------------


@router.get("/{decision_id}/trace", response_model=DecisionTraceResponse)
async def trace_decision(
    decision_id: str,
    depth: int = Query(default=3, ge=1, le=10),
    max_fanout: int = Query(default=20, ge=1, le=100),
    ctx: RequestContext = Depends(request_context),
) -> DecisionTraceResponse:
    """Depth-bounded BFS upstream from this Decision.

    Walks `precedent` and `input` edges; `approval` and `outcome` edges
    are surfaced as terminal labels (not walked further) so the narrator
    can render them without exploding the trace. `truncated` /
    `truncated_count` are set when a node's outgoing edges exceeded
    `max_fanout`.
    """
    decision_uuid = _maybe_uuid(decision_id)
    graph: DecisionGraph = get_decision_graph()
    result = await graph.trace(
        decision_uuid,
        depth=depth,
        max_fanout=max_fanout,
        requester_scopes=_requester_scopes(ctx),
    )
    if not result.nodes:
        # Root either missing or scope-hidden — collapse to 404 so the
        # caller cannot probe.
        raise NotFound("decisions", decision_id)
    return _trace_to_response(result)


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id/downstream — inverse precedent walk
# ---------------------------------------------------------------------------


@router.get("/{decision_id}/downstream", response_model=DecisionTraceResponse)
async def downstream_decision(
    decision_id: str,
    depth: int = Query(default=3, ge=1, le=10),
    ctx: RequestContext = Depends(request_context),
) -> DecisionTraceResponse:
    """Decisions that later cited this one as a precedent.

    Reads the inverse of the precedent index — the result's edges are
    stamped with `relation='downstream'` so the renderer reads the
    arrow direction correctly without re-deriving inverse semantics.
    """
    decision_uuid = _maybe_uuid(decision_id)
    graph: DecisionGraph = get_decision_graph()
    result = await graph.downstream(
        decision_uuid,
        depth=depth,
        requester_scopes=_requester_scopes(ctx),
    )
    if not result.nodes:
        raise NotFound("decisions", decision_id)
    return _trace_to_response(result)


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id/timeline — flattened journal events
# ---------------------------------------------------------------------------


@router.get("/{decision_id}/timeline", response_model=TimelineResponse)
async def decision_timeline(
    decision_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: RequestContext = Depends(request_context),
    journal: Journal = Depends(get_journal),
) -> TimelineResponse:
    """Return the journal events that produced this Decision, in seq order.

    Bypasses the projection — reads the journal directly for events
    sharing the Decision's `correlation_id`. The projection folds the
    same events into one row; the timeline view is what the narrator
    and the audit UI use to render the per-event history with the
    real ts / actor / payload.

    Scope filter: the Decision is looked up through the graph first
    so a caller outside scope sees a 404 just like the other routes
    (no "decision is hidden but its events are visible" probe).
    """
    decision_uuid = _maybe_uuid(decision_id)
    graph: DecisionGraph = get_decision_graph()
    decision = await graph.get(decision_uuid, requester_scopes=_requester_scopes(ctx))
    if decision is None:
        raise NotFound("decisions", decision_id)

    events: list[JournalEventDTO] = []

    if decision.correlation_id is not None:
        # Reach into the backend so we can capture the seq alongside the
        # entry — the public ``Journal.query`` API flattens seq away.
        # Same pattern the pocket journal stream router uses.
        backend = journal._backend  # type: ignore[attr-defined]
        rows = backend._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM events WHERE correlation_id = ? ORDER BY seq ASC LIMIT ?",
            (str(decision.correlation_id), limit),
        )
        for row in rows:
            entry, seq = backend._row_to_entry(row)  # type: ignore[attr-defined]
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            events.append(
                JournalEventDTO(
                    seq=seq,
                    id=entry.id,
                    ts=entry.ts,
                    actor_kind=entry.actor.kind,
                    actor_id=entry.actor.id,
                    action=entry.action,
                    scope=list(entry.scope),
                    correlation_id=entry.correlation_id,
                    causation_id=entry.causation_id,
                    payload=dict(payload),
                )
            )

    return TimelineResponse(
        decision_id=decision_uuid,
        correlation_id=decision.correlation_id,
        events=events,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/decisions/explain — natural-language Q&A (Slice 3a)
# ---------------------------------------------------------------------------


@router.post("/explain", response_model=ExplanationResponse)
async def explain_decision(
    body: ExplainRequest,
    ctx: RequestContext = Depends(request_context),
) -> ExplanationResponse:
    """Answer a natural-language question about the decision graph.

    The pipeline (per RFC 07 § "LLM-grounded query"):

      1. extractor: small LLM call (Haiku) distills the question to
         structured filters. Falls back to a deterministic regex
         extractor when the SDK / key are missing.
      2. find: index-driven multi-axis filter over Decisions.
      3. trace: depth-bounded BFS upstream from the top candidate.
      4. narrator: Sonnet call (or templated fallback) produces a
         grounded paragraph. Every claim must cite a decision id.
      5. verifier: re-scans the narrative for ungrounded sentences.
      6. cache: 24h TTL keyed on (question_norm, root_id, depth,
         scope_hash). The projection's post-apply hook invalidates
         entries whose `decisions_walked` set contains a newly-emitted
         decision.

    Scope filter: identical to the read routes — the request's scope
    tag list is derived from auth, never from the body. A question
    that resolves to a decision outside scope produces the empty
    "no matching decision" response (the privacy invariant).
    """
    input_body = ExplainRequestInput(
        question=body.question,
        scope=body.scope,
        max_decisions=body.max_decisions,
        depth=body.depth,
        backend=body.backend,
    )
    explanation = await explain(
        input_body,
        requester_scopes=_requester_scopes(ctx),
    )
    return ExplanationResponse.from_domain(explanation)


# Export the cursor encoder for the test suite's keyset assertions.
__all__ = ["_encode_cursor", "router"]
