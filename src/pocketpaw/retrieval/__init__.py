# __init__.py — pocketpaw.retrieval: the runtime infrastructure for fanning
# out retrieval across registered SourceAdapters.
# Updated: feat/receive-retrieval-infra (review pass) — MockAdapter demoted
# out of the public __all__ surface. It is a test helper and remains
# importable from pocketpaw.retrieval.adapters for suites that want it,
# but should not appear in hover / IDE discovery for production callers.
# Created: feat/receive-retrieval-infra (2026-04-19) — soul-protocol 0.3.2
# pruned engine/retrieval/ and kept only the vocabulary (Protocols + types +
# exceptions) in soul_protocol.spec.retrieval. This module is where the
# concrete orchestration (Router, broker impl, callable-wrapping adapter,
# test helpers) now lives. Spec imports come from soul_protocol.spec —
# this module implements against them.

from __future__ import annotations

from .adapters import ProjectionAdapter
from .broker import InMemoryCredentialBroker
from .router import RetrievalRouter

__all__ = [
    "InMemoryCredentialBroker",
    "ProjectionAdapter",
    "RetrievalRouter",
]
