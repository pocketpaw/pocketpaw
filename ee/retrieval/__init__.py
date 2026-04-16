# ee/retrieval/__init__.py — Retrieval log + graduation as a journal projection.
# Created: 2026-04-16 (feat/retrieval-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Supersedes the side-channel design in held PRs
# #936 (JSONL retrieval sink) and #937 (graduation policy over that JSONL).
# Both targeted the same problem — an observable retrieval trail + access-
# count graduation — with a separate `~/.pocketpaw/retrieval.jsonl` file
# and its own mutex. The org journal is now the source of truth, so the
# JSONL sink is retired and the domain logic re-lands here as a projection
# over the journal's ``retrieval.query`` + ``graduation.applied`` events.
#
# What we re-export: the store (write path), the projection (read path),
# the policy (graduation decisions), plus the canonical action names and
# payload builders for callers that want to emit events out of band.

from ee.retrieval.events import (
    ACTION_GRADUATION_APPLIED,
    ACTION_RETRIEVAL_QUERY,
    ALL_RETRIEVAL_ACTIONS,
    graduation_applied_payload,
    retrieval_query_payload,
)
from ee.retrieval.policy import (
    DEFAULT_EPISODIC_THRESHOLD,
    DEFAULT_SEMANTIC_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    GraduationDecision,
    GraduationKind,
    GraduationReport,
    apply_decisions,
    scan_for_graduations,
)
from ee.retrieval.projection import (
    GraduationStateRow,
    RetrievalProjection,
    RetrievalView,
)
from ee.retrieval.store import RetrievalJournalStore

__all__ = [
    # Actions + payload builders.
    "ACTION_RETRIEVAL_QUERY",
    "ACTION_GRADUATION_APPLIED",
    "ALL_RETRIEVAL_ACTIONS",
    "retrieval_query_payload",
    "graduation_applied_payload",
    # Write path.
    "RetrievalJournalStore",
    # Read path.
    "RetrievalProjection",
    "RetrievalView",
    "GraduationStateRow",
    # Graduation policy.
    "GraduationDecision",
    "GraduationKind",
    "GraduationReport",
    "DEFAULT_WINDOW_DAYS",
    "DEFAULT_EPISODIC_THRESHOLD",
    "DEFAULT_SEMANTIC_THRESHOLD",
    "scan_for_graduations",
    "apply_decisions",
]
