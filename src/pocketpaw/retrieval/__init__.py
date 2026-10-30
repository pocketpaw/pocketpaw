# retrieval/__init__.py — Retrieval orchestration + the journal projection.
# Updated: 2026-06-02 (feat/retrieval-rehome, #1327) — re-homed the retrieval
# *orchestration* classes that soul-protocol 0.4.0 (#179) deleted from
# ``soul_protocol.engine.retrieval``: ``RetrievalRouter`` (the dispatcher),
# ``InMemoryCredentialBroker`` (the reference broker), and ``ProjectionAdapter``
# (the callable-wrapping adapter). soul-protocol now ships only the retrieval
# *vocabulary* (models + protocols + exceptions) under spec.retrieval; the
# concrete orchestration is application-layer and belongs here in the consuming
# runtime. They live alongside the existing journal-projection layer below — two
# concerns, one package. Note: ``RetrievalRouter`` here is the dispatcher, NOT
# the FastAPI ``APIRouter`` exported as the ``router`` object from router.py.
#
# Created: 2026-04-16 (feat/retrieval-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Supersedes the side-channel design in held PRs
# #936 (JSONL retrieval sink) and #937 (graduation policy over that JSONL).
# Both targeted the same problem — an observable retrieval trail + access-
# count graduation — with a separate `~/.pocketpaw/retrieval.jsonl` file
# and its own mutex. The org journal is now the source of truth, so the
# JSONL sink is retired and the domain logic re-lands here as a projection
# over the journal's ``retrieval.query`` + ``graduation.applied`` events.
#
# What we re-export: the orchestration (router + broker + projection adapter),
# the store (write path), the projection (read path), the policy (graduation
# decisions), plus the canonical action names and payload builders for callers
# that want to emit events out of band.

from pocketpaw.retrieval.adapters import ProjectionAdapter
from pocketpaw.retrieval.broker import Credential, InMemoryCredentialBroker
from pocketpaw.retrieval.events import (
    ACTION_GRADUATION_APPLIED,
    ACTION_RETRIEVAL_QUERY,
    ALL_RETRIEVAL_ACTIONS,
    graduation_applied_payload,
    retrieval_query_payload,
)
from pocketpaw.retrieval.orchestrator import RetrievalRouter
from pocketpaw.retrieval.policy import (
    DEFAULT_EPISODIC_THRESHOLD,
    DEFAULT_SEMANTIC_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    GraduationDecision,
    GraduationKind,
    GraduationReport,
    apply_decisions,
    scan_for_graduations,
)
from pocketpaw.retrieval.projection import (
    GraduationStateRow,
    RetrievalProjection,
    RetrievalView,
)
from pocketpaw.retrieval.store import RetrievalJournalStore

__all__ = [
    # Retrieval orchestration (re-homed from soul-protocol 0.4.0, #1327).
    "RetrievalRouter",
    "InMemoryCredentialBroker",
    "ProjectionAdapter",
    "Credential",
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
