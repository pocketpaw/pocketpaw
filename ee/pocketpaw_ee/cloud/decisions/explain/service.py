# ee/pocketpaw_ee/cloud/decisions/explain/service.py
# Created: 2026-05-25 (RFC 07 Slice 3a) — the orchestrator the REST
#   route and the MCP wrapper both call. End-to-end:
#     1. extract_entities(question, scope) → ExtractedEntities
#     2. DecisionGraph.find(...) with the extracted filters → candidate list
#     3. DecisionGraph.trace(root, depth=3) for the top candidate
#     4. compress trace + pass to narrator
#     5. verify grounding; strip / flag ungrounded sentences
#     6. cache result keyed on (question_norm, root_id, depth, scope_hash)
#     7. return Explanation
#
#   The pocket-template config `narrator.backend_pref` (per RFC 07 line
#   621) flows through as the `backend` arg on the request. Defaults to
#   "llm" — falls back to "templated" automatically on any LLM failure.
"""End-to-end explain orchestrator."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw_ee.cloud.decisions.domain import Decision
from pocketpaw_ee.cloud.decisions.explain.cache import (
    _hash_scope as hash_scope,
)
from pocketpaw_ee.cloud.decisions.explain.cache import (
    _normalize_question as normalize_question,
)
from pocketpaw_ee.cloud.decisions.explain.cache import (
    build_cache_key,
    get_explain_cache,
)
from pocketpaw_ee.cloud.decisions.explain.extractor import (
    ExtractedEntities,
    extract_entities,
)
from pocketpaw_ee.cloud.decisions.explain.narrator import (
    Explanation,
    narrate_decision,
)
from pocketpaw_ee.cloud.decisions.service import (
    DecisionGraph,
    get_decision_graph,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input shape — what the router validates a POST body into
# ---------------------------------------------------------------------------


class ExplainRequestInput(BaseModel):
    """Validated input the orchestrator consumes. Distinct from the
    `ExplainRequest` wire DTO (which lives in `decisions.dto`) so the
    orchestrator can be called from MCP / CLI / tests without a FastAPI
    request body."""

    model_config = ConfigDict(frozen=False)

    question: str = Field(min_length=1, max_length=2000)
    scope: dict[str, Any] | None = None
    max_decisions: int = Field(default=5, ge=1, le=20)
    depth: int = Field(default=3, ge=1, le=10)
    backend: Literal["llm", "templated"] | None = None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def explain(
    body: ExplainRequestInput | dict[str, Any],
    *,
    requester_scopes: list[str] | None = None,
    graph: DecisionGraph | None = None,
    api_key: str | None = None,
    now: datetime | None = None,
) -> Explanation:
    """Answer one natural-language question with a grounded paragraph.

    Args:
        body: The validated request. Accepts a dict so non-FastAPI
            callers (MCP wrappers, tests) can pass plain kwargs.
        requester_scopes: Scope tags the find / trace passes through to
            the graph. None means "admin / unscoped".
        graph: Override the singleton DecisionGraph (test seam).
        api_key: Anthropic key override (test seam — primarily for the
            mock-LLM path).
        now: Override "now" for cache TTL math (test seam).

    Returns:
        An Explanation. Never raises — every failure path falls through
        to the templated narrator and an empty-trace canned response.
    """
    body = ExplainRequestInput.model_validate(body)
    graph = graph or get_decision_graph()
    cache = get_explain_cache()

    # ----- 1. Extract -------------------------------------------------------
    entities = await extract_entities(
        body.question,
        scope=body.scope,
        backend=body.backend,
        api_key=api_key,
    )

    # ----- 2. Find ----------------------------------------------------------
    candidates = await _find_candidates(
        graph,
        entities,
        max_decisions=body.max_decisions,
        requester_scopes=requester_scopes,
    )

    if not candidates:
        # No matching decisions — still return a coherent response so
        # the UI never sees an empty body.
        empty = Explanation(
            narrative=(
                "No matching decision was found in the supplied trace. "
                "Try widening the date range, naming the fabric object "
                "(for example `lease:LR-2026-117`), or asking about a "
                "specific actor."
            ),
            decisions_walked=[],
            depth_reached=0,
            backend_used=body.backend or "templated",
        )
        return empty

    root = _pick_root(candidates, entities)

    # ----- 3. Cache lookup --------------------------------------------------
    question_norm = normalize_question(body.question)
    scope_hash = hash_scope(body.scope)
    cache_key = build_cache_key(
        question_normalized=question_norm,
        root_decision_id=root.id,
        depth=body.depth,
        scope_hash=scope_hash,
    )
    cached = cache.get(cache_key, now=now)
    if cached is not None:
        return cached

    # ----- 4. Trace ---------------------------------------------------------
    trace = await graph.trace(
        root.id,
        depth=body.depth,
        requester_scopes=requester_scopes,
    )

    # ----- 5. Narrate -------------------------------------------------------
    backend = body.backend or "llm"
    explanation = await narrate_decision(
        root,
        trace,
        backend=backend,
        api_key=api_key,
    )

    # ----- 6. Cache the result ---------------------------------------------
    try:
        cache.put(
            cache_key,
            explanation,
            question_normalized=question_norm,
            root_decision_id=root.id,
            depth=body.depth,
            scope_hash=scope_hash,
            now=now,
        )
    except Exception:  # noqa: BLE001
        logger.warning("explain cache.put failed — returning uncached", exc_info=True)

    return explanation


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


async def _find_candidates(
    graph: DecisionGraph,
    entities: ExtractedEntities,
    *,
    max_decisions: int,
    requester_scopes: list[str] | None,
) -> list[Decision]:
    """Resolve ExtractedEntities into Decision candidates.

    Priority order:
      1. Explicit decision_ids — fetch each one directly.
      2. fabric_object_refs — narrow by input id.
      3. actor / time / policy / pocket filters.
      4. Pure fallback — most-recent N in scope (so the narrator always
         has something to walk).
    """
    # 1. explicit decision ids
    if entities.decision_ids:
        out: list[Decision] = []
        for d_id in entities.decision_ids[:max_decisions]:
            d = await graph.get(d_id, requester_scopes=requester_scopes)
            if d is not None:
                out.append(d)
        if out:
            return out

    # 2. fabric-object refs — first ref wins; widen the filter to the others
    # via find() per-ref then dedup.
    if entities.fabric_object_refs:
        accumulated: dict[UUID, Decision] = {}
        for obj_ref in entities.fabric_object_refs:
            since = until = None
            if entities.time_range:
                since, until = entities.time_range
            found = await graph.find(
                input_id=obj_ref,
                since=since,
                until=until,
                pocket_id=entities.pocket_id,
                policy=entities.policy_ref,
                limit=max_decisions,
                requester_scopes=requester_scopes,
            )
            for d in found:
                accumulated.setdefault(d.id, d)
            if len(accumulated) >= max_decisions:
                break
        ranked = _rank_by_outcome_hint(list(accumulated.values()), entities.outcome_hint)
        if ranked:
            return ranked[:max_decisions]

    # 3. axis-driven find
    actor = entities.actor_refs[0] if entities.actor_refs else None
    since = until = None
    if entities.time_range:
        since, until = entities.time_range
    outcome_status = _outcome_hint_to_status(entities.outcome_hint)
    found = await graph.find(
        actor=actor,
        since=since,
        until=until,
        pocket_id=entities.pocket_id,
        policy=entities.policy_ref,
        outcome_status=outcome_status,
        limit=max_decisions,
        requester_scopes=requester_scopes,
    )
    if found:
        return _rank_by_outcome_hint(found, entities.outcome_hint)[:max_decisions]

    # 4. Pure fallback — most-recent N in scope. Widen time too in case
    # the time range was over-narrow.
    if entities.time_range:
        # Retry without time bound to recover the close-but-not-exact case.
        found = await graph.find(
            actor=actor,
            pocket_id=entities.pocket_id,
            policy=entities.policy_ref,
            outcome_status=outcome_status,
            limit=max_decisions,
            requester_scopes=requester_scopes,
        )
        if found:
            return _rank_by_outcome_hint(found, entities.outcome_hint)[:max_decisions]

    return []


def _pick_root(candidates: list[Decision], entities: ExtractedEntities) -> Decision:
    """Pick the candidate the narrator centers on. Outcome hint nudges
    the choice when the caller asked about an approval or rejection."""
    ranked = _rank_by_outcome_hint(candidates, entities.outcome_hint)
    return ranked[0] if ranked else candidates[0]


def _rank_by_outcome_hint(
    candidates: list[Decision],
    hint: str | None,
) -> list[Decision]:
    """Stable sort that floats decisions matching the outcome hint to
    the front, preserving the original ts-DESC ordering elsewhere."""
    if not hint or not candidates:
        return candidates

    def match(d: Decision) -> int:
        if hint == "approved":
            # "approved" is shorthand for "passed instinct and no rejection."
            if d.outcome and d.outcome.status == "rejected":
                return 1
            if d.instinct_policy_passed is True:
                return 0
            return 1
        if hint == "rejected":
            if d.outcome and d.outcome.status == "rejected":
                return 0
            if d.instinct_policy_passed is False:
                return 0
            return 1
        if hint == "landed":
            if d.outcome and d.outcome.status == "landed":
                return 0
            return 1
        if hint == "pending":
            if d.outcome is None:
                return 0
            return 1
        return 0

    return sorted(candidates, key=match)


def _outcome_hint_to_status(hint: str | None) -> str | None:
    if hint == "rejected":
        return "rejected"
    if hint == "landed":
        return "landed"
    if hint == "pending":
        return "pending"
    # "approved" maps to "all non-rejected" — we filter post-fetch instead
    # so the rank step is the only place that filters.
    return None


__all__ = [
    "ExplainRequestInput",
    "explain",
]


# Re-export some helpers so tests don't reach for the underscore-prefixed
# variants. Keeping the leading-underscore names in cache.py prevents
# accidental cross-module reach (the helpers are still cache-internal
# in intent — these are test-only re-exports).
__all__ += ["hash_scope", "normalize_question"]


# Backwards compatibility — older callers might import this with a stale
# parameter. ``timedelta`` import kept so future TTL tuning lands without
# another import edit.
_ = timedelta  # silence unused-import lint when TTL tuning lands later
_ = timezone  # same — kept ready for explicit now-with-tz construction
