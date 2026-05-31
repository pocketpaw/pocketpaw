# ee/pocketpaw_ee/cloud/decisions/explain/__init__.py
# Created: 2026-05-25 (RFC 07 Slice 3a) — package marker for the
#   natural-language explain pipeline that sits on top of the deterministic
#   DecisionGraph. Four submodules:
#     - extractor.py — small LLM call that turns a question into structured
#       filters (templated fallback when no API key).
#     - narrator.py  — grounded prose generation. Two backends: a Sonnet
#       LLM path (default) and a deterministic templated path used when
#       the LLM call fails or the pocket config opts in.
#     - cache.py     — 24h cache keyed on (question_norm, root_id, depth,
#       scope_hash) backed by the same SQLite store as the projection.
#     - service.py   — the ``explain(question, scope, max_decisions,
#       backend)`` orchestrator the router and MCP wrapper both call.
#
#   The split keeps each piece narrow enough to unit-test in isolation
#   and lines up with the four-file ee/cloud entity convention even
#   though this is a sub-package (the parent `decisions/` already owns
#   domain/dto/service/router; this nests under it).

from pocketpaw_ee.cloud.decisions.explain.extractor import (
    ExtractedEntities,
    extract_entities,
)
from pocketpaw_ee.cloud.decisions.explain.narrator import (
    Explanation,
    narrate_decision,
)
from pocketpaw_ee.cloud.decisions.explain.service import (
    ExplainRequestInput,
    explain,
)

__all__ = [
    "ExplainRequestInput",
    "Explanation",
    "ExtractedEntities",
    "explain",
    "extract_entities",
    "narrate_decision",
]
