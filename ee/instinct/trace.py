# ee/instinct/trace.py — Decision trace types for the Instinct pipeline.
# Created: 2026-04-13 (Move 2 PR-A) — Captures the reasoning inputs behind each
# proposed action so the audit log explains *why*, not just *what*. Paired with
# TraceCollector (the bus-subscriber context manager) and FabricObjectSnapshot
# (immutable rows that preserve referenced objects at decision time).

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ee.fabric.models import _gen_id


class ToolCallRef(BaseModel):
    """One tool call captured during proposal reasoning.

    Stored inside `ReasoningTrace.tool_calls`. `args_hash` is a stable fingerprint
    of the invocation so repeated calls dedupe cleanly; `result_preview` is the
    first 200 chars of the result string so a human can inspect the trace without
    re-running the tool.
    """

    tool: str
    args_hash: str
    result_preview: str = ""
    duration_ms: int = 0


class ReasoningTrace(BaseModel):
    """Full reasoning context that produced a proposed action.

    Every decision that lands in `AuditEntry.context` under the key
    `reasoning_trace` follows this schema. Reference fields hold IDs only —
    hydrated content is resolved at read time via the `?hydrate=1` endpoint
    (Move 2 PR-B).
    """

    fabric_queries: list[str] = Field(default_factory=list)
    soul_memories: list[str] = Field(default_factory=list)
    kb_articles: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallRef] = Field(default_factory=list)
    prompt_version: str = ""
    backend: str = ""
    model: str = ""
    token_counts: dict[str, int] = Field(default_factory=dict)


class FabricObjectSnapshot(BaseModel):
    """An immutable snapshot of a Fabric object at the time a decision was made.

    When the live object later changes (ownership transfer, status update,
    anything), the trace still reproduces what the agent saw. Keyed by
    (object_id, audit_id) so a single query can be referenced by many
    decisions without duplication.
    """

    id: str = Field(default_factory=lambda: _gen_id("fos"))
    object_id: str
    audit_id: str
    object_type: str = ""
    snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
