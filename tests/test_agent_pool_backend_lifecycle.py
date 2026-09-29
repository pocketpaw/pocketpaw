# tests/test_agent_pool_backend_lifecycle.py
# Pins how AgentPool releases the backends it builds.
#
#   * A BYOK turn runs on a private backend built for that turn alone. It must
#     be released when the turn ends (a Claude SDK backend otherwise keeps its
#     CLI subprocess forever), and it must carry the agent's own ToolPolicy
#     rather than the process-wide one.
#   * AgentRouter.create_isolated_backend hands a policy to a backend that
#     declares one.
#   * AgentPool.invalidate releases the dropped instance's backend: at once when
#     it is idle, after its last run when it is busy.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.pool import AgentPool

pytestmark = pytest.mark.asyncio


class _Backend:
    def __init__(self, name: str = "shared") -> None:
        self.name = name
        self.settings = SimpleNamespace(agent_backend="claude_agent_sdk")
        self.policy = object()
        self.cleaned = 0
        self.stopped = 0

    def get_tool_policy(self):
        return self.policy

    async def run(self, message, **kwargs):
        yield SimpleNamespace(type="done", content="")

    async def cleanup(self):
        self.cleaned += 1

    async def stop(self):
        self.stopped += 1


def _instance(backend):
    return SimpleNamespace(
        agent_id="a1",
        agent_name="Paw",
        backend=backend,
        soul_manager=None,
        config={"soul_persona": "P", "system_prompt": ""},
        last_active=datetime.now(UTC),
        active_runs=0,
        deny_by_default_backend=None,
    )


async def test_byok_backend_is_released_and_carries_the_agent_policy(monkeypatch):
    from pocketpaw.agents.router import AgentRouter

    shared = _Backend()
    isolated = _Backend("byok")
    calls: list[dict] = []

    def _create(name, settings, *, settings_override=None, policy=None):
        calls.append({"name": name, "policy": policy})
        return isolated

    monkeypatch.setattr(AgentRouter, "create_isolated_backend", staticmethod(_create))
    pool = AgentPool()
    inst = _instance(shared)

    async def _get(_aid):
        return inst

    monkeypatch.setattr(pool, "get", _get)
    async for _ in pool.run("a1", "hi", "session:s1", byok_api_key="sk-user"):
        pass

    assert calls and calls[0]["policy"] is shared.policy, "BYOK dropped the agent's ToolPolicy"
    assert isolated.cleaned + isolated.stopped >= 1, "the per-turn BYOK backend was leaked"
    assert shared.cleaned == 0 and shared.stopped == 0, "the shared backend was torn down"


def test_create_isolated_backend_passes_policy(monkeypatch):
    from pocketpaw.agents import router as router_mod

    class _WithPolicy:
        def __init__(self, settings, policy=None):
            self.policy = policy

    monkeypatch.setattr(router_mod, "get_backend_class", lambda _n: _WithPolicy)
    policy = object()
    backend = router_mod.AgentRouter.create_isolated_backend(
        "claude_agent_sdk", SimpleNamespace(), policy=policy
    )
    assert backend.policy is policy


async def test_invalidate_releases_an_idle_instance():
    pool = AgentPool()
    backend = _Backend()
    pool._instances["a1"] = _instance(backend)
    await pool.invalidate("a1")
    assert backend.stopped == 1, "invalidate dropped an idle instance without teardown"


async def test_invalidate_defers_teardown_of_a_busy_instance(monkeypatch):
    pool = AgentPool()
    backend = _Backend()
    inst = _instance(backend)
    pool._instances["a1"] = inst

    async def _get(_aid):
        return inst

    monkeypatch.setattr(pool, "get", _get)
    agen = pool.run("a1", "hi", "session:s1")
    await agen.__anext__()  # the run is now active
    await pool.invalidate("a1")
    assert backend.stopped == 0, "invalidate tore down a backend mid-run"
    async for _ in agen:
        pass
    assert backend.stopped == 1, "the invalidated instance was never torn down"
