# tests/test_pool_turn_context.py
# Pins how ``AgentPool.run`` splits the assembled prompt for a backend that
# takes per-turn context separately (``turn_context``).
#
# The volatile layers (``legacy_tail`` = the knowledge-base wrapper, whose
# content carries KB hits, <scope>/<participants>/<current-pocket>, the member
# briefing and <uploaded-files>; ``retrieval`` = the soul recall) change every
# turn. A backend that applies its system prompt only at connect (the Claude
# SDK) must get them as ``turn_context`` so they can ride the user message, and
# get a system prompt holding only the stable, keyed layers. A backend that does
# not declare ``turn_context`` keeps the one assembled prompt, byte for byte.

from __future__ import annotations

from types import SimpleNamespace

from pocketpaw.agents.pool import AgentPool


def _instance(backend):
    return SimpleNamespace(
        agent_id="agent-1",
        agent_name="Paw",
        backend=backend,
        soul_manager=None,
        config={"soul_persona": "WHO I AM", "system_prompt": ""},
        memory_namespace="ns",
        created_from_updated_at=None,
        active_runs=0,
        last_active=None,
    )


class _SplitBackend:
    def __init__(self):
        self.calls: list[dict] = []

    async def run(
        self,
        message,
        *,
        system_prompt=None,
        history=None,
        session_key=None,
        system_prompt_digest="",
        turn_context="",
    ):
        self.calls.append(
            dict(
                system_prompt=system_prompt, turn_context=turn_context, digest=system_prompt_digest
            )
        )
        yield SimpleNamespace(type="done", content="")


class _PlainBackend:
    def __init__(self):
        self.calls: list[dict] = []

    async def run(
        self,
        message,
        *,
        system_prompt=None,
        history=None,
        session_key=None,
        system_prompt_digest="",
    ):
        self.calls.append(dict(system_prompt=system_prompt, digest=system_prompt_digest))
        yield SimpleNamespace(type="done", content="")


async def _drive(monkeypatch, backend, knowledge):
    pool = AgentPool()
    instance = _instance(backend)

    async def _fake_get(agent_id):  # noqa: ARG001
        return instance

    monkeypatch.setattr(pool, "get", _fake_get)
    async for _ in pool.run(
        "agent-1",
        "hello",
        "cloud:session:s1:agent-1",
        knowledge_context=knowledge,
        instructions="LAW.",
    ):
        pass


async def test_volatile_layers_ride_turn_context_for_a_backend_that_takes_it(monkeypatch):
    backend = _SplitBackend()
    await _drive(monkeypatch, backend, "<scope>pocket P1</scope> codeword PELICAN")
    await _drive(monkeypatch, backend, "<scope>pocket P2</scope> codeword WALRUS")

    first, second = backend.calls
    assert "PELICAN" in first["turn_context"] and "WALRUS" in second["turn_context"]
    assert "PELICAN" not in first["system_prompt"] and "WALRUS" not in second["system_prompt"]
    assert "LAW." in first["system_prompt"], "the keyed layers stay in the system prompt"
    assert first["system_prompt"] == second["system_prompt"]
    assert first["digest"] == second["digest"]


async def test_a_backend_without_turn_context_keeps_the_whole_prompt(monkeypatch):
    split, plain = _SplitBackend(), _PlainBackend()
    await _drive(monkeypatch, split, "codeword PELICAN")
    await _drive(monkeypatch, plain, "codeword PELICAN")

    assert "PELICAN" in plain.calls[0]["system_prompt"]
    assert plain.calls[0]["system_prompt"].startswith(split.calls[0]["system_prompt"])
    assert plain.calls[0]["digest"] == split.calls[0]["digest"], (
        "the split must not move the digest"
    )
