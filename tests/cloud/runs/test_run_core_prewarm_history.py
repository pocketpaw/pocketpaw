# tests/cloud/runs/test_run_core_prewarm_history.py
# Pins that ``_prewarm_session`` hands the run's conversation history to
# ``AgentPool.prewarm``. The Claude SDK bakes history into the system prompt
# only at ``connect()``, and the prewarm is what connects turn 1's client, so a
# prewarm without history builds a client that has forgotten the session.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

pytestmark = pytest.mark.asyncio

_HISTORY = [
    {"role": "user", "content": "My codeword is PELICAN-42"},
    {"role": "assistant", "content": "Noted."},
]


class _CapturingPool:
    def __init__(self) -> None:
        self.prewarm_kwargs: dict[str, Any] | None = None

    async def get(self, _agent_id):
        return type("Inst", (), {"config": {"backend": "claude_agent_sdk"}})()

    async def prewarm(self, *args, **kwargs):
        self.prewarm_kwargs = kwargs


async def test_prewarm_session_forwards_history(monkeypatch):
    pool = _CapturingPool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "INSTR")
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(run_core, "bind_pawbar_run", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(run_core, "unbind_pawbar_run", lambda *a, **k: None, raising=False)

    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )
    await run_core._prewarm_session(ctx, history=_HISTORY)

    assert pool.prewarm_kwargs is not None, "prewarm never ran — check the guards"
    assert pool.prewarm_kwargs.get("history") == _HISTORY
