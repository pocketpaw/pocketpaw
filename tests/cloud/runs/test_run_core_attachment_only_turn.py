# tests/cloud/runs/test_run_core_attachment_only_turn.py
# Created: 2026-09-08 (fix/attachment-only-turns) — pins that a turn carrying
# ONLY attachments reaches the model as a real request instead of as an empty
# one.
#
# The bug this file exists for: the composer sends a zero-width space (U+200B)
# as the message body when the user attaches files and types nothing — the row
# has to be non-empty to persist, and every renderer strips it so no
# placeholder shows. The backend then handed that sentinel to ``AgentPool.run``
# verbatim. The attached file's text DOES reach the prompt, but it lands in the
# knowledge/system channel, so the model saw a system block of reference
# material and a user turn that said nothing, and answered the only way that
# leaves: by asking what the user wanted. Pasting a long brief (which the
# composer converts to a .txt attachment) and pressing send therefore produced
# "I don't have a brief yet — what's the site for?" while the brief sat in the
# prompt. Typing any character alongside the attachment made it work, which is
# exactly the tell.
#
# What these prove:
#   * an attachment-only turn is given a user message that says the files ARE
#     the message, so the model acts on the inlined text;
#   * the substitute reaches BOTH ``AgentPool.run`` and
#     ``build_knowledge_context`` (the latter uses it as the KB query — a
#     zero-width char is a useless one);
#   * an ordinary typed turn is passed through BYTE-IDENTICALLY, sentinel or
#     not — this must not start rewriting messages people actually wrote;
#   * an empty turn with NO attachments invents nothing.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

pytestmark = pytest.mark.asyncio

# What the composer actually sends for a files-only message.
SENTINEL = "\u200b"

ATTACHMENTS = [{"url": "upload://f1", "filename": "pasted-20260908-101500.txt"}]


class _CapturingPool:
    def __init__(self) -> None:
        self.prompt: str | None = None

    async def get(self, _agent_id):
        return type("Inst", (), {"config": {"backend": "claude_agent_sdk"}})()

    def run(self, _agent_id, prompt, _session_key, **_kwargs):
        self.prompt = prompt

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


async def _drive(
    monkeypatch,
    *,
    content: str,
    attachments: list[dict[str, Any]] | None,
) -> tuple[_CapturingPool, dict[str, Any]]:
    pool = _CapturingPool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    seen: dict[str, Any] = {}

    async def _fake_knowledge(_ctx, **kwargs):
        seen.update(kwargs)
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
    return pool, seen


async def test_an_attachment_only_turn_reaches_the_model_as_a_request(monkeypatch):
    """The whole bug in one assertion: the model must not be handed a message
    that says nothing while the user's brief sits in the system prompt."""
    pool, seen = await _drive(monkeypatch, content=SENTINEL, attachments=ATTACHMENTS)

    assert pool.prompt is not None
    assert SENTINEL not in pool.prompt
    assert pool.prompt.strip() != ""
    # It has to name the block the text was inlined into, or the instruction is
    # just an assertion the model cannot check.
    assert "<uploaded-files>" in pool.prompt
    # And the KB query gets the same substitute, not a zero-width char.
    assert seen["user_message"] == pool.prompt


async def test_whitespace_only_content_with_attachments_is_treated_the_same(monkeypatch):
    """A client that sends spaces (or nothing at all) instead of the sentinel is
    the same situation and must not fall through to the empty-prompt path."""
    for content in ("", "   ", "\n\t", "\ufeff"):
        pool, _ = await _drive(monkeypatch, content=content, attachments=ATTACHMENTS)
        assert pool.prompt and "<uploaded-files>" in pool.prompt


async def test_a_typed_message_is_passed_through_byte_for_byte(monkeypatch):
    """The substitution is only for turns with nothing to say. A real message —
    including one that happens to carry the sentinel next to real text — must
    arrive exactly as the user wrote it."""
    pool, _ = await _drive(monkeypatch, content="build me a landing page", attachments=ATTACHMENTS)
    assert pool.prompt == "build me a landing page"

    pool, _ = await _drive(
        monkeypatch, content=f"{SENTINEL}build me a landing page", attachments=ATTACHMENTS
    )
    assert pool.prompt == f"{SENTINEL}build me a landing page"


async def test_an_empty_turn_with_no_attachments_invents_nothing(monkeypatch):
    """Nothing was attached, so there is no file to point the model at. Say
    nothing rather than describing files that do not exist."""
    pool, _ = await _drive(monkeypatch, content=SENTINEL, attachments=None)
    assert pool.prompt == SENTINEL
