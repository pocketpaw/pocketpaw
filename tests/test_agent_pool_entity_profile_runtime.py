# tests/test_agent_pool_entity_profile_runtime.py
# Created: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A1/A2) —
# pins the OSS-side runtime consumption of two formerly-inert SurfaceProfile
# fields threaded through ``AgentPool.run``:
#   * ``system_message_override`` — SWAPS the base persona/soul identity portion
#     of the assembled system prompt while KEEPING the downstream layers
#     (authoritative instructions incl. ripple LAW, soul memory, knowledge
#     wrapper). Net = override + instructions + knowledge.
#   * ``skill_names`` — forwarded to the backend's ``run`` ONLY when non-empty
#     (withhold-when-empty, so the 6 non-Claude backends keep their signature).

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.pool import AgentPool

pytestmark = pytest.mark.asyncio


class _CapturingBackend:
    """Records the (message, kwargs) of the last ``run`` call. Yields nothing."""

    def __init__(self) -> None:
        self.last_message: str | None = None
        self.last_kwargs: dict | None = None

    async def run(self, message: str, **kwargs) -> AsyncIterator[object]:
        self.last_message = message
        self.last_kwargs = kwargs
        return
        yield  # pragma: no cover — makes this an async generator


def _instance_with(backend, persona: str = "BASE PERSONA"):
    """A minimal AgentInstance stand-in: no soul, persona via config fallback."""
    return SimpleNamespace(
        backend=backend,
        soul_manager=None,
        config={"soul_persona": persona, "system_prompt": ""},
        last_active=datetime.now(UTC),
        active_runs=0,
    )


async def _drain(pool, **run_kwargs):
    async for _ in pool.run("a1", "hello", "session:s1", **run_kwargs):
        pass


async def _run_with(monkeypatch, **run_kwargs) -> _CapturingBackend:
    backend = _CapturingBackend()
    inst = _instance_with(backend)
    pool = AgentPool()

    async def _fake_get(agent_id):
        return inst

    monkeypatch.setattr(pool, "get", _fake_get)
    await _drain(pool, **run_kwargs)
    return backend


# ---------------------------------------------------------------------------
# A1 — system_message_override (SWAP BASE, KEEP LAYERS)
# ---------------------------------------------------------------------------


async def test_override_swaps_base_keeps_layers(monkeypatch):
    backend = await _run_with(
        monkeypatch,
        instructions="RIPPLE LAW: do the thing",
        knowledge_context="KB FACT",
        system_message_override="ENTITY BASE OVERRIDE",
    )
    prompt = backend.last_kwargs["system_prompt"]
    # Base swapped:
    assert "ENTITY BASE OVERRIDE" in prompt
    assert "BASE PERSONA" not in prompt
    # Layers kept (instructions + knowledge wrapper still append):
    assert "RIPPLE LAW: do the thing" in prompt
    assert "KB FACT" in prompt
    # Order: override comes before instructions before knowledge.
    assert prompt.index("ENTITY BASE OVERRIDE") < prompt.index("RIPPLE LAW")
    assert prompt.index("RIPPLE LAW") < prompt.index("KB FACT")


async def test_no_override_keeps_base(monkeypatch):
    backend = await _run_with(
        monkeypatch,
        instructions="RIPPLE LAW",
        knowledge_context="KB FACT",
    )
    prompt = backend.last_kwargs["system_prompt"]
    assert "BASE PERSONA" in prompt
    assert "RIPPLE LAW" in prompt


async def test_empty_string_override_is_applied(monkeypatch):
    """An explicit empty-string override (entity wants NO base) swaps the base
    to empty but still keeps the layers — only ``None`` means 'no opinion'."""
    backend = await _run_with(
        monkeypatch,
        instructions="RIPPLE LAW",
        knowledge_context="KB FACT",
        system_message_override="",
    )
    prompt = backend.last_kwargs["system_prompt"]
    assert "BASE PERSONA" not in prompt
    assert "RIPPLE LAW" in prompt
    assert "KB FACT" in prompt


# ---------------------------------------------------------------------------
# A2 — skill_names forwarding (withhold-when-empty)
# ---------------------------------------------------------------------------


async def test_skill_names_forwarded_when_nonempty(monkeypatch):
    backend = await _run_with(monkeypatch, skill_names=frozenset({"github"}))
    assert backend.last_kwargs.get("skill_names") == frozenset({"github"})


async def test_skill_names_withheld_when_empty(monkeypatch):
    backend = await _run_with(monkeypatch)
    assert "skill_names" not in backend.last_kwargs, (
        "empty skill_names must be withheld so non-Claude backends keep their signature"
    )


# ---------------------------------------------------------------------------
# A2b — agent's OWN skill_refs materialize on every run path
# ---------------------------------------------------------------------------


def _instance_with_skill_refs(backend, refs):
    inst = _instance_with(backend)
    inst.config = {**inst.config, "skill_refs": refs}
    return inst


async def _run_with_instance(monkeypatch, inst, **run_kwargs):
    pool = AgentPool()

    async def _fake_get(agent_id):
        return inst

    monkeypatch.setattr(pool, "get", _fake_get)
    await _drain(pool, **run_kwargs)


async def test_agent_skill_refs_forwarded_without_explicit_skill_names(monkeypatch):
    backend = _CapturingBackend()
    inst = _instance_with_skill_refs(backend, ["snctm-vetting"])
    await _run_with_instance(monkeypatch, inst)
    # No skill_names passed by the caller (the DM/bridge path) — the agent's own
    # skill_refs must still reach the backend.
    assert backend.last_kwargs.get("skill_names") == frozenset({"snctm-vetting"})


async def test_agent_skill_refs_union_with_explicit_skill_names(monkeypatch):
    backend = _CapturingBackend()
    inst = _instance_with_skill_refs(backend, ["snctm-vetting"])
    await _run_with_instance(monkeypatch, inst, skill_names=frozenset({"github"}))
    assert backend.last_kwargs.get("skill_names") == frozenset({"snctm-vetting", "github"})


async def test_no_skill_refs_keeps_caller_skill_names(monkeypatch):
    backend = _CapturingBackend()
    inst = _instance_with(backend)  # no skill_refs
    await _run_with_instance(monkeypatch, inst, skill_names=frozenset({"github"}))
    assert backend.last_kwargs.get("skill_names") == frozenset({"github"})


async def test_no_skill_refs_no_skill_names_withheld(monkeypatch):
    backend = _CapturingBackend()
    inst = _instance_with(backend)  # no skill_refs, no caller skill_names
    await _run_with_instance(monkeypatch, inst)
    assert "skill_names" not in backend.last_kwargs
