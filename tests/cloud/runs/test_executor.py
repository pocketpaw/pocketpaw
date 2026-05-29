import pytest
from pocketpaw_ee.cloud.chat.runs import executor as ex
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

pytestmark = pytest.mark.asyncio


def _spec():
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


async def test_in_process_executor_runs_execute_run(monkeypatch):
    seen = []

    async def fake_execute_run(spec):
        seen.append(spec.run_id)

    monkeypatch.setattr(ex, "execute_run", fake_execute_run)
    inproc = ex.InProcessExecutor()
    await inproc.submit(_spec())
    await inproc.drain()  # await all tracked tasks
    assert seen == ["r1"]


async def test_executor_selection_defaults_to_inprocess(monkeypatch):
    monkeypatch.delenv("POCKETPAW_CLOUD_RUN_EXECUTOR", raising=False)
    ex._reset_for_tests()
    assert isinstance(ex.get_executor(), ex.InProcessExecutor)
