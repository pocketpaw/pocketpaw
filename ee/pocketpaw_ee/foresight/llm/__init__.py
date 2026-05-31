# ee/pocketpaw_ee/foresight/llm/__init__.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
# Foresight backend adapters. v0.1 ships:
#   - ClaudeCodeBackend: the thin Claude Code SDK ↔ CAMEL BaseModelBackend
#     adapter described in RFC 08 §6.4.
#   - DeterministicFakeBackend: tests + smoke runs without network.
# v1.0 adds the tier-pool builder (premium/mid/tail round-robin) and
# wires LiteLLM as the documented fallback path.

from __future__ import annotations

from pocketpaw_ee.foresight.llm.adapter import (
    ClaudeCodeBackend,
    DeterministicFakeBackend,
)

__all__ = ["ClaudeCodeBackend", "DeterministicFakeBackend"]
