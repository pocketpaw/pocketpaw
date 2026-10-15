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
#   * A BYOK turn whose private backend cannot be built is refused with an
#     ``error`` event; the shared backend (our credentials) never runs it.
#   * ``spends_byok_key`` says which backends really spend the caller's key.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.pool import AgentPool

pytestmark = pytest.mark.asyncio


class _Backend:
    def __init__(self, name: str = "shared", agent_backend: str = "claude_agent_sdk") -> None:
        self.name = name
        self.settings = SimpleNamespace(agent_backend=agent_backend)
        self.policy = object()
        self.cleaned = 0
        self.stopped = 0
        self.ran = 0

    def get_tool_policy(self):
        return self.policy

    async def run(self, message, **kwargs):
        self.ran += 1
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


@pytest.mark.parametrize("exclusive_tools", [False, True])
async def test_a_failed_byok_build_refuses_the_turn(monkeypatch, exclusive_tools):
    """The cloud let this turn skip the wallet because the caller's key pays.
    Running it on the shared backend instead would be unbilled platform spend,
    so the pool yields an error and runs nothing. Covers the plain BYOK branch
    and the deny-by-default one.

    Mutation: fall back to ``instance.backend`` (or the platform pydantic_ai)
    when the isolated build raises.
    """
    from pocketpaw.agents import pool as pool_mod
    from pocketpaw.agents import registry
    from pocketpaw.agents.router import AgentRouter

    shared = _Backend(agent_backend="pydantic_ai")

    def _boom(*_a, **_k):
        raise RuntimeError("bad settings key")

    monkeypatch.setattr(AgentRouter, "create_isolated_backend", staticmethod(_boom))
    monkeypatch.setattr(registry, "get_backend_class", lambda _n: _Backend)
    pool = AgentPool()
    inst = _instance(shared)

    async def _get(_aid):
        return inst

    monkeypatch.setattr(pool, "get", _get)
    events = [
        ev
        async for ev in pool.run(
            "a1", "hi", "session:s1", byok_api_key="sk-user", exclusive_tools=exclusive_tools
        )
    ]

    assert [ev.type for ev in events] == ["error"]
    assert events[0].content == pool_mod.BYOK_BACKEND_FAILED_MESSAGE
    assert shared.ran == 0, "the turn ran on the platform backend"
    assert inst.active_runs == 0


@pytest.mark.parametrize(
    ("backend", "exclusive", "spends"),
    [
        ("pydantic_ai", False, True),
        ("claude_agent_sdk", False, False),
        ("claude_code", False, False),
        (None, False, False),
        ("openai_agents", False, False),
        ("claude_agent_sdk", True, True),
    ],
)
async def test_spends_byok_key(backend, exclusive, spends):
    """Mutation: answer True for every backend, or default a missing backend to
    pydantic_ai (the pool builds a missing one as claude_agent_sdk)."""
    from pocketpaw.agents.pool import spends_byok_key

    assert spends_byok_key(backend, exclusive_tools=exclusive) is spends


async def test_a_failed_byok_build_on_a_backend_that_ignores_the_key_falls_back(monkeypatch):
    """claude_agent_sdk never reads the override, so the cloud gated and billed
    this turn as platform already. A failed private build then runs it on the
    shared backend instead of telling the user their key failed.

    Mutation: refuse on every failed build.
    """
    from pocketpaw.agents.router import AgentRouter

    shared = _Backend(agent_backend="claude_agent_sdk")

    def _boom(*_a, **_k):
        raise RuntimeError("bad settings key")

    monkeypatch.setattr(AgentRouter, "create_isolated_backend", staticmethod(_boom))
    pool = AgentPool()
    inst = _instance(shared)

    async def _get(_aid):
        return inst

    monkeypatch.setattr(pool, "get", _get)
    events = [ev async for ev in pool.run("a1", "hi", "session:s1", byok_api_key="sk-user")]

    assert [ev.type for ev in events] == ["done"]
    assert shared.ran == 1


class _PaiBackend(_Backend):
    """A pydantic_ai stand-in that declares the per-run limit kwargs."""

    def __init__(self, name: str = "pai") -> None:
        super().__init__(name, agent_backend="pydantic_ai")
        self.kwargs: dict = {}

    async def run(
        self,
        message,
        *,
        max_requests: int | None = None,
        total_tokens_limit: int | None = None,
        model_override: str | None = None,
        **kwargs,
    ):
        self.ran += 1
        self.kwargs = {
            "max_requests": max_requests,
            "total_tokens_limit": total_tokens_limit,
            "model_override": model_override,
        }
        yield SimpleNamespace(type="done", content="")


def _forcing_pool(monkeypatch, shared, built):
    from pocketpaw.agents import registry
    from pocketpaw.agents.router import AgentRouter

    def _build(name, settings, settings_override=None, policy=None, **_k):
        built.append(settings_override)
        pai.policy = policy
        return pai

    pai = _PaiBackend()
    monkeypatch.setattr(AgentRouter, "create_isolated_backend", staticmethod(_build))
    monkeypatch.setattr(registry, "get_backend_class", lambda _n: _PaiBackend)
    pool = AgentPool()
    inst = _instance(shared)

    async def _get(_aid):
        return inst

    monkeypatch.setattr(pool, "get", _get)
    return pool, inst, pai


async def test_force_pydantic_ai_runs_a_claude_sdk_agent_on_pydantic_ai(monkeypatch):
    """An unpaid platform-funded run never touches the agent's claude_agent_sdk
    backend (its own turn cap, the server's CLI credentials). It runs on
    pydantic_ai with the per-run limits, and the per-send model chosen for the
    other backend is dropped.

    Mutation: ignore ``force_pydantic_ai``, or skip forwarding the limits.
    """
    shared = _Backend()
    built: list = []
    pool, inst, pai = _forcing_pool(monkeypatch, shared, built)

    events = [
        ev
        async for ev in pool.run(
            "a1",
            "hi",
            "session:s1",
            force_pydantic_ai=True,
            max_requests=20,
            total_tokens_limit=5_000,
            model_override="claude-opus-4-6",
        )
    ]

    assert [ev.type for ev in events] == ["done"]
    assert shared.ran == 0, "the turn ran on the agent's own backend"
    assert pai.ran == 1
    assert built == [{"agent_backend": "pydantic_ai"}]
    assert pai.kwargs == {"max_requests": 20, "total_tokens_limit": 5_000, "model_override": None}
    assert inst.active_runs == 0


async def test_force_pydantic_ai_keeps_the_agent_tool_policy(monkeypatch):
    """A forced free run is built with the agent's own ToolPolicy, so a
    claude_agent_sdk agent that opted into the planner keeps it on pydantic_ai.

    Mutation: build the forced backend without ``policy=``.
    """
    shared = _Backend()
    pool, _inst, pai = _forcing_pool(monkeypatch, shared, [])

    _ = [ev async for ev in pool.run("a1", "hi", "session:s1", force_pydantic_ai=True)]

    assert pai.ran == 1
    assert pai.policy is shared.policy, "the forced build dropped the agent's ToolPolicy"


async def test_limits_a_backend_cannot_take_refuse_the_turn(monkeypatch):
    """Limits that would be silently dropped are not limits. A backend whose
    ``run`` cannot take them gets no turn.

    Mutation: forward the limits only when accepted and run anyway.
    """
    from pocketpaw.agents import pool as pool_mod

    shared = _Backend()
    pool = AgentPool()
    inst = _instance(shared)

    async def _get(_aid):
        return inst

    monkeypatch.setattr(pool, "get", _get)
    events = [ev async for ev in pool.run("a1", "hi", "session:s1", max_requests=20)]

    assert [ev.type for ev in events] == ["error"]
    assert events[0].content == pool_mod.RUN_LIMITS_UNSUPPORTED_MESSAGE
    assert shared.ran == 0
