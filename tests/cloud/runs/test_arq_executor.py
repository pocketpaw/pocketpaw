"""Tier 2: ArqExecutor enqueues a job for the worker pool instead of
running the agent inline."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat.runs import arq_executor
from pocketpaw_ee.cloud.chat.runs.arq_executor import ArqExecutor
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

pytestmark = pytest.mark.asyncio


def _spec(run_id: str = "r1") -> RunSpec:
    return RunSpec(
        run_id=run_id,
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id=f"c-{run_id}",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def test_arq_executor_enqueues_execute_run_job(monkeypatch):
    enqueued: list[tuple[str, dict]] = []

    class _FakePool:
        async def enqueue_job(self, name: str, payload: dict) -> None:
            enqueued.append((name, payload))

    async def _fake_pool():
        return _FakePool()

    monkeypatch.setattr(arq_executor, "_get_pool", _fake_pool)
    arq_executor._reset_for_tests()

    ex = ArqExecutor()
    await ex.submit(_spec())

    assert len(enqueued) == 1
    name, payload = enqueued[0]
    assert name == "execute_run_job"
    assert payload["run_id"] == "r1"
    assert payload["workspace_id"] == "w1"
    # Round-trips through Pydantic — proves the payload is plain JSON primitives.
    restored = RunSpec.model_validate(payload)
    assert restored.run_id == "r1"


async def test_arq_executor_reuses_pool(monkeypatch):
    calls = 0

    class _FakePool:
        async def enqueue_job(self, name: str, payload: dict) -> None:
            pass

    async def _fake_pool():
        nonlocal calls
        calls += 1
        return _FakePool()

    monkeypatch.setattr(arq_executor, "create_pool", _make_create_pool(_fake_pool))
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    arq_executor._reset_for_tests()

    ex = ArqExecutor()
    await ex.submit(_spec("a"))
    await ex.submit(_spec("b"))

    # _get_pool is memoised — the underlying pool factory runs exactly once.
    assert calls == 1


def _make_create_pool(factory):
    """Wrap a zero-arg async factory so it matches arq.create_pool(settings)."""

    async def _create_pool(_settings):
        return await factory()

    return _create_pool
