# tests/cloud/runs/test_run_core_surface_threading.py
# Created: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — pins that
# ``_drive_agent_loop`` READS the resolved surface off ``ctx.surface_context``
# and forwards BOTH halves to ``AgentPool.run`` as plain data:
# ``surface_preamble`` (a ``str``) and ``surface_cache_key`` (a ``str | None``).
# No ``pocketpaw_ee`` symbol crosses the boundary, the same contract
# ``deny_mcp_tool_ids`` rides on.
#
# This is the seam with no other cover. The preamble used to reach the prompt
# folded into ``knowledge_context``; now it travels on its own, and if this
# threading were dropped the surface would silently vanish from every cloud
# prompt — no error, no failing handler test, just an agent that no longer
# knows which pocket the user is looking at. The key half matters just as much
# and is quieter still: drop it and the preamble is right while the digest goes
# blind, so a cached agent survives a navigation it should not have.
#
# The "and it is not ALSO in the knowledge context" half deliberately does not
# live here. It was written here first and it was worthless: this file fakes
# ``build_knowledge_context``, so the assertion could never see the real one
# come back. Re-prepending the preamble in ``build_dynamic_context`` left it
# green. It lives in ``tests/cloud/chat/test_agent_service_surface_injection.py``
# instead, against the function that would actually do it.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.surface import SurfaceContext, SurfaceKind, SurfaceMeta

pytestmark = pytest.mark.asyncio

_PREAMBLE = '<surface kind="pocket" route="/pockets/p1" />\n<current-pocket id="p1" />'
_KEY = "pocket:s:0123456789abcdef"


class _CapturingPool:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] | None = None

    async def get(self, _agent_id):
        return type("Inst", (), {"config": {"backend": "claude_agent_sdk"}})()

    def run(self, *args, **kwargs):
        self.run_kwargs = kwargs

        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


def _ctx(surface_context: SurfaceContext | None) -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        surface_context=surface_context,
    )


def _surface(preamble: str, key: str | None) -> SurfaceContext:
    return SurfaceContext(
        workspace_id="w1",
        user_id="u1",
        kind=SurfaceKind.POCKET,
        meta=SurfaceMeta(pocket_id="p1"),
        preamble=preamble,
        preamble_cache_key=key,
    )


async def _drive(monkeypatch, ctx) -> _CapturingPool:
    pool = _CapturingPool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _fake_knowledge(*a, **k):
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
        ctx,
        user_content="hi",
        attachments_in=None,
        mentions_in=None,
        history=[],
        is_cancelled=_is_cancelled,
        emit_stream_start=False,
    )
    async for _ in gen:
        pass
    return pool


async def test_threads_the_preamble_and_its_key(monkeypatch):
    pool = await _drive(monkeypatch, _ctx(_surface(_PREAMBLE, _KEY)))

    assert pool.run_kwargs is not None
    assert pool.run_kwargs.get("surface_preamble") == _PREAMBLE
    assert pool.run_kwargs.get("surface_cache_key") == _KEY


async def test_an_unkeyed_surface_threads_a_none_key(monkeypatch):
    """A handler that would not claim a key must arrive as ``None`` rather than
    as some placeholder string, which the layer would treat as a stability
    claim nobody made."""
    pool = await _drive(monkeypatch, _ctx(_surface(_PREAMBLE, None)))

    assert pool.run_kwargs is not None
    assert pool.run_kwargs.get("surface_preamble") == _PREAMBLE
    assert pool.run_kwargs.get("surface_cache_key") is None


async def test_no_surface_context_threads_the_no_surface_answer(monkeypatch):
    """Older clients and non-surface paths. NOT withhold-when-empty: these two
    never reach a backend, so there is no narrow signature to protect, and
    ``""`` / ``None`` is itself the meaningful answer."""
    pool = await _drive(monkeypatch, _ctx(None))

    assert pool.run_kwargs is not None
    assert pool.run_kwargs.get("surface_preamble") == ""
    assert pool.run_kwargs.get("surface_cache_key") is None
