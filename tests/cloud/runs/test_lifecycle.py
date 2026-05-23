"""Pin ``InProcessExecutor.drain()`` behaviour."""

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud.chat.runs import executor as ex

pytestmark = pytest.mark.asyncio


def _spec():
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

    return RunSpec(
        run_id="r1",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def test_shutdown_drains_in_process_executor(monkeypatch):
    ex._reset_for_tests()
    monkeypatch.delenv("POCKETPAW_CLOUD_RUN_EXECUTOR", raising=False)
    inproc = ex.get_executor()
    assert isinstance(inproc, ex.InProcessExecutor)

    done: list[str] = []

    async def slow(spec):
        await asyncio.sleep(0.05)
        done.append(spec.run_id)

    monkeypatch.setattr(ex, "execute_run", slow)
    await inproc.submit(_spec())
    await inproc.drain()
    assert done == ["r1"]


async def test_drain_is_no_op_when_no_tasks(monkeypatch):
    ex._reset_for_tests()
    monkeypatch.delenv("POCKETPAW_CLOUD_RUN_EXECUTOR", raising=False)
    inproc = ex.get_executor()
    # nothing submitted
    await inproc.drain()  # must not hang or error
