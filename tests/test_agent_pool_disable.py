# tests/test_agent_pool_disable.py
# Created: 2026-06-28 (feat/aiam-agent-revoke, AW-4) — pins the OSS run-pool
# enforcement of the agent soft-disable / revoke-everywhere flow:
#   * ``AgentPool.get`` raises ``AgentDisabled`` when the resolved agent doc has
#     ``disabled=True`` — on BOTH the cold-build path and the cached-instance
#     path (the must-fix: a cached instance is NOT handed back for a disabled
#     agent, and the disabled check is not swallowed by the DB-error guard).
#   * ``invalidate`` drops the cached instance so the NEXT ``get`` rebuilds /
#     re-checks (immediate revoke, no stale-instance window).
#   * re-enabling lets ``get`` return a working instance again.
#   * ``invalidate`` does not tear down the backend, so an already-resolved
#     in-flight instance keeps running (disable blocks NEW resolves only).

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from pocketpaw.agents.errors import AgentDisabled, AgentNotFound
from pocketpaw.agents.pool import AgentInstance, AgentPool

pytestmark = pytest.mark.asyncio


# --- Fakes -----------------------------------------------------------------


class _FakeDoc:
    """Stand-in for the cloud Agent Beanie doc the pool fetches."""

    def __init__(self, *, disabled: bool = False, updated_at: datetime | None = None) -> None:
        self.disabled = disabled
        self.updatedAt = updated_at or datetime.now(UTC)


class _FakeAgentModel:
    """Stand-in for the resolved Agent document class; ``get`` returns the doc
    registered for the id (or ``None`` for 'no such agent')."""

    def __init__(self, docs: dict[str, _FakeDoc | None]) -> None:
        self._docs = docs

    async def get(self, oid):  # noqa: ANN001 — PydanticObjectId-ish, str() it
        return self._docs.get(str(oid))


# A valid-looking ObjectId hex so ``PydanticObjectId(agent_id)`` parses.
AID = "0123456789abcdef01234567"


def _patch_model(monkeypatch, docs: dict[str, _FakeDoc | None]) -> None:
    monkeypatch.setattr(
        "pocketpaw.agents.pool._resolve_agent_model",
        lambda: _FakeAgentModel(docs),
    )


def _stub_instance(agent_id: str, *, updated_at: datetime | None = None) -> AgentInstance:
    return AgentInstance(
        agent_id=agent_id,
        agent_name="Buddy",
        config={},
        backend=SimpleNamespace(),  # never run in these tests
        soul_manager=None,
        memory_namespace="ns",
        created_from_updated_at=updated_at,
    )


def _patch_build(monkeypatch, pool: AgentPool) -> dict[str, int]:
    """Make ``_build`` return a stub instance (no real backend) and count calls."""
    calls = {"n": 0}

    async def _fake_build(agent_doc):  # noqa: ANN001
        calls["n"] += 1
        inst = _stub_instance(AID, updated_at=getattr(agent_doc, "updatedAt", None))
        pool._instances[AID] = inst
        return inst

    monkeypatch.setattr(pool, "_build", _fake_build)
    return calls


# --- Cold-build path -------------------------------------------------------


async def test_get_raises_agent_disabled_on_cold_build(monkeypatch):
    """A disabled agent with NO cached instance fails closed at the build path."""
    _patch_model(monkeypatch, {AID: _FakeDoc(disabled=True)})
    pool = AgentPool()
    _patch_build(monkeypatch, pool)

    with pytest.raises(AgentDisabled):
        await pool.get(AID)


async def test_get_raises_agent_not_found_when_missing(monkeypatch):
    """Sanity: missing doc still raises AgentNotFound (not AgentDisabled)."""
    _patch_model(monkeypatch, {AID: None})
    pool = AgentPool()
    _patch_build(monkeypatch, pool)

    with pytest.raises(AgentNotFound):
        await pool.get(AID)


async def test_get_builds_when_enabled(monkeypatch):
    """An enabled agent builds and returns a working instance."""
    _patch_model(monkeypatch, {AID: _FakeDoc(disabled=False)})
    pool = AgentPool()
    calls = _patch_build(monkeypatch, pool)

    inst = await pool.get(AID)
    assert inst.agent_id == AID
    assert calls["n"] == 1


# --- Cached-instance path (the must-fix) -----------------------------------


async def test_disable_invalidates_cache_then_get_raises(monkeypatch):
    """MUST-FIX: an agent cached as an instance, then disabled + invalidated,
    raises AgentDisabled on the NEXT get() — no stale instance reused."""
    docs: dict[str, _FakeDoc | None] = {AID: _FakeDoc(disabled=False)}
    _patch_model(monkeypatch, docs)
    pool = AgentPool()
    _patch_build(monkeypatch, pool)

    # First get builds + caches.
    inst = await pool.get(AID)
    assert pool._instances.get(AID) is inst

    # Admin disables: flip the doc + explicit cache invalidation (what the
    # service's disable() does).
    docs[AID] = _FakeDoc(disabled=True)
    await pool.invalidate(AID)
    assert AID not in pool._instances

    # Next resolve fails closed.
    with pytest.raises(AgentDisabled):
        await pool.get(AID)


async def test_cached_instance_not_reused_for_disabled_even_without_invalidate(monkeypatch):
    """Defense in depth: even if invalidate were skipped, a cached instance is
    NOT handed back once the doc reads disabled — the check runs before the
    staleness branch and is re-raised past the broad DB-error guard."""
    docs: dict[str, _FakeDoc | None] = {AID: _FakeDoc(disabled=False)}
    _patch_model(monkeypatch, docs)
    pool = AgentPool()
    _patch_build(monkeypatch, pool)

    inst = await pool.get(AID)
    assert pool._instances.get(AID) is inst

    # Flip disabled WITHOUT invalidating the cache.
    docs[AID] = _FakeDoc(disabled=True)
    with pytest.raises(AgentDisabled):
        await pool.get(AID)


async def test_enable_restores_working_instance(monkeypatch):
    """After re-enable + invalidate, the next get() returns a working instance."""
    docs: dict[str, _FakeDoc | None] = {AID: _FakeDoc(disabled=True)}
    _patch_model(monkeypatch, docs)
    pool = AgentPool()
    _patch_build(monkeypatch, pool)

    with pytest.raises(AgentDisabled):
        await pool.get(AID)

    # Re-enable + invalidate (what enable() does).
    docs[AID] = _FakeDoc(disabled=False)
    await pool.invalidate(AID)

    inst = await pool.get(AID)
    assert inst.agent_id == AID


# --- In-flight runs are unaffected -----------------------------------------


async def test_invalidate_does_not_teardown_running_instance(monkeypatch):
    """``invalidate`` only drops the cache entry; an already-resolved instance
    (e.g. with an in-flight run) keeps its backend — disable blocks NEW
    resolves, it does not abort runs mid-stream."""
    teardown_calls = {"n": 0}

    docs: dict[str, _FakeDoc | None] = {AID: _FakeDoc(disabled=False)}
    _patch_model(monkeypatch, docs)
    pool = AgentPool()
    _patch_build(monkeypatch, pool)

    async def _spy_teardown(instance):  # noqa: ANN001
        teardown_calls["n"] += 1

    monkeypatch.setattr(pool, "_teardown", _spy_teardown)

    inst = await pool.get(AID)
    inst.active_runs = 1  # simulate an in-flight run holding this instance

    # An admin disables mid-run: the in-flight caller still holds ``inst``.
    docs[AID] = _FakeDoc(disabled=True)
    await pool.invalidate(AID)

    # invalidate must NOT have torn down the backend.
    assert teardown_calls["n"] == 0
    # The caller's own reference is intact and still usable for its run.
    assert inst.active_runs == 1
    assert inst.backend is not None


async def test_staleness_rebuild_still_works_for_enabled(monkeypatch):
    """Regression: the existing updatedAt-staleness rebuild path is unbroken for
    an ENABLED agent (the new disabled check sits before it, no interference)."""
    t0 = datetime.now(UTC)
    docs: dict[str, _FakeDoc | None] = {AID: _FakeDoc(disabled=False, updated_at=t0)}
    _patch_model(monkeypatch, docs)
    pool = AgentPool()
    calls = _patch_build(monkeypatch, pool)

    await pool.get(AID)  # build #1
    assert calls["n"] == 1

    # Bump updatedAt → next get rebuilds.
    docs[AID] = _FakeDoc(disabled=False, updated_at=t0 + timedelta(seconds=5))
    await pool.get(AID)  # build #2
    assert calls["n"] == 2
