# ee/retrieval/events.py — Canonical event payloads for the retrieval projection.
# Created: 2026-04-16 (feat/retrieval-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Carries #936's intent (capture retrieval traces
# in an append-only log) and #937's intent (graduation decisions written
# durably) onto the org journal. The action names come from soul-protocol's
# v0.3.1 catalog — ``retrieval.query`` is the event soul-protocol's own
# RetrievalRouter already emits, ``graduation.applied`` is listed there too
# even though no upstream writer exists yet.
#
# Payload shapes:
#   - ``retrieval.query`` — we keep the v0.3.1 base keys (``request_id``,
#     ``query``, ``strategy``, ``sources_queried``, ``sources_failed``,
#     ``candidate_count``) and extend with pocketpaw-specific context the
#     graduation projection needs (full candidate list with tier + score,
#     picked IDs, pocket, latency). Downstream readers that only know the
#     base keys still work — additive fields, no breaking rename.
#   - ``graduation.applied`` — no upstream writer shipped with v0.3.1, so
#     this is the first concrete shape. Mirrors #937's GraduationDecision
#     so the projection can reconstruct decisions without a second table.
#
# Scope lives on ``EventEntry.scope`` (the journal column), NOT inside the
# payload. Same rule as ee/fabric/events.py — scope is the journal's
# canonical filter and duplicating it in the payload invites drift.

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Action names — from soul-protocol v0.3.1 ACTION_CATALOG. Pinned as
# constants so the projection + policy can both reach them without another
# module path.
# ---------------------------------------------------------------------------

ACTION_RETRIEVAL_QUERY = "retrieval.query"
ACTION_GRADUATION_APPLIED = "graduation.applied"

ALL_RETRIEVAL_ACTIONS = (
    ACTION_RETRIEVAL_QUERY,
    ACTION_GRADUATION_APPLIED,
)


# ---------------------------------------------------------------------------
# Payload builders — tiny module-level functions, same pattern as
# ee/fabric/events.py. Kept boring on purpose so migrations and out-of-band
# emitters (soul-protocol's own router, for instance) produce identical
# dicts to what the projection expects.
# ---------------------------------------------------------------------------


def retrieval_query_payload(
    *,
    request_id: str,
    query: str,
    strategy: str = "parallel",
    sources_queried: list[str] | None = None,
    sources_failed: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    picked: list[str] | None = None,
    latency_ms: int = 0,
    pocket_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Payload for ``retrieval.query`` events.

    Base keys (``request_id``, ``query``, ``strategy``, ``sources_queried``,
    ``sources_failed``, ``candidate_count``) match what soul-protocol's own
    RetrievalRouter emits — so callers replaying a journal written by the
    engine get the same shape regardless of who wrote the entry.

    The extra keys (``candidates``, ``picked``, ``latency_ms``, ``pocket_id``,
    ``trace_id``) are additive — pocketpaw's graduation projection needs the
    per-candidate tier + score to count accesses, and the operator UI wants
    to see what was actually picked. Consumers that only know the base
    shape simply ignore the extra keys.

    Each entry in ``candidates`` is a small dict — ``{"id", "source",
    "score", "tier"?}`` — not a Pydantic model so the projection survives
    soul-protocol refactors without a migration.
    """

    candidates = list(candidates or [])
    return {
        "request_id": request_id,
        "query": query,
        "strategy": strategy,
        "sources_queried": list(sources_queried or []),
        "sources_failed": list(sources_failed or []),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "picked": list(picked or []),
        "latency_ms": latency_ms,
        "pocket_id": pocket_id,
        "trace_id": trace_id,
    }


def graduation_applied_payload(
    *,
    memory_id: str,
    kind: str,
    access_count: int,
    window_days: int,
    from_tier: str | None,
    to_tier: str,
    pocket_id: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Payload for ``graduation.applied`` events.

    One event per decision. The projection walks these to reconstruct a
    per-memory graduation history — which memories graduated, to what
    tier, how many accesses triggered the promotion. Apply-or-propose is
    recorded on the EventEntry's ``actor`` (``system:graduation`` for the
    scheduler, a real actor when a human pushes the button) rather than
    inside the payload.
    """

    return {
        "memory_id": memory_id,
        "kind": kind,
        "access_count": access_count,
        "window_days": window_days,
        "from_tier": from_tier,
        "to_tier": to_tier,
        "pocket_id": pocket_id,
        "reason": reason,
    }
