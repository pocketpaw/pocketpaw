# __init__.py — pocketpaw.retrieval: the runtime infrastructure for fanning
# out retrieval across registered SourceAdapters.
# Created: feat/receive-retrieval-infra (2026-04-19) — soul-protocol 0.3.2
# pruned engine/retrieval/ and kept only the vocabulary (Protocols + types +
# exceptions) in soul_protocol.spec.retrieval. This module is where the
# concrete orchestration (Router, broker impl, callable-wrapping adapter,
# test helpers) now lives. Spec imports come from soul_protocol.spec —
# this module implements against them.

from __future__ import annotations

from .adapters import MockAdapter, ProjectionAdapter
from .broker import InMemoryCredentialBroker
from .router import RetrievalRouter

__all__ = [
    "InMemoryCredentialBroker",
    "MockAdapter",
    "ProjectionAdapter",
    "RetrievalRouter",
]
