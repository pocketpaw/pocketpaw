# tests/cloud/runs/test_run_core_attachment_instructions.py
# Created: 2026-09-14 (fix/attachment-not-on-disk) — pins that a turn carrying
# attachments states the inlined-not-on-disk rule in the AUTHORITATIVE
# instructions channel, not only in the knowledge block.
#
# Why a second channel for one rule. ``build_behavior_instructions`` exists
# precisely because of this split, and says so in its own docstring: rules put
# in ``instructions`` "read as rules", while the same words buried in
# ``knowledge_context`` "read as reference data and the model often ignores
# them". The ``<uploaded-files>`` block lives in the knowledge channel — the one
# the codebase documents as ignorable — so the file-tool-equipped backend had
# nothing authoritative telling it the upload has no path.
#
# Why the existing attachment-only fix does not already cover this. The reported
# failure is a turn where the user DOES type something ("what does the file I
# uploaded say?"). ``resolve_user_content`` substitutes its guidance ONLY for an
# empty turn and returns typed text byte-identically — a deliberate contract.
# So on exactly the turn that broke, the user channel carries no such rule.
#
# What these prove:
#   * attachments present -> the rule reaches ``AgentPool.run`` as instructions,
#     names the block the text landed in, and forbids the Read/Glob hunt;
#   * no attachments -> nothing is appended, so an ordinary turn's prompt (and
#     its cache digest) is untouched.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

pytestmark = pytest.mark.asyncio

ATTACHMENTS = [{"url": "upload://f1", "filename": "quarterly-report.pdf"}]


class _CapturingPool:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def get(self, _agent_id):
        return type("Inst", (), {"config": {"backend": "claude_agent_sdk"}})()

    def run(self, _agent_id, prompt, _session_key, **kwargs):
        self.kwargs = kwargs

        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()

    async def prewarm(self, *a, **k):  # pragma: no cover - not exercised here
        return None


def _ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def _drive(monkeypatch, *, content: str, attachments) -> _CapturingPool:
    pool = _CapturingPool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _fake_knowledge(_ctx, **kwargs):
        return "KB"

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "INSTR")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None, raising=False)

    async def _is_cancelled():
        return False

    gen = run_core._drive_agent_loop(
        _ctx(),
        user_content=content,
        attachments_in=attachments,
        mentions_in=None,
        history=[{"role": "user", "content": "earlier"}],
        is_cancelled=_is_cancelled,
        emit_stream_start=False,
    )
    async for _ in gen:
        pass
    return pool


async def test_a_typed_turn_with_an_attachment_gets_the_rule_as_an_instruction(monkeypatch):
    """The turn that actually broke: the user typed a question ABOUT the file."""
    pool = await _drive(
        monkeypatch,
        content="what does the file I uploaded say?",
        attachments=ATTACHMENTS,
    )

    instructions = pool.kwargs.get("instructions") or ""
    assert "INSTR" in instructions, "the base instructions must survive"

    lowered = instructions.lower()
    # Points at the block the text was actually inlined into, or the rule is an
    # assertion the model has no way to check.
    assert "<uploaded-files>" in lowered
    assert "not on the filesystem" in lowered
    # Names the tools it would otherwise reach for, and the answer it must stop
    # giving.
    assert "read" in lowered and "glob" in lowered
    assert "no file" in lowered


async def test_a_turn_with_no_attachments_is_left_alone(monkeypatch):
    """Nothing to point at, so nothing is appended — the ordinary turn's prompt
    (and its cache digest) must not move."""
    pool = await _drive(monkeypatch, content="hello there", attachments=None)

    assert (pool.kwargs.get("instructions") or "") == "INSTR"
