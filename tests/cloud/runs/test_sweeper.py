"""Stale-run sweeper marks queued/running ChatRunDocs as interrupted when
they've outlived the threshold — the backend process died, the executor task
is gone, but Mongo still says ``running``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.chat.runs import sweeper
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

pytestmark = pytest.mark.asyncio


def _make_run(*, status: str, created_minutes_ago: int) -> ChatRunDoc:
    created = datetime.now(UTC) - timedelta(minutes=created_minutes_ago)
    return ChatRunDoc(
        run_id=f"r-{status}-{created_minutes_ago}",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id=f"c-{status}-{created_minutes_ago}",
        user_message_id="um1",
        status=status,  # type: ignore[arg-type]
        createdAt=created,
    )


async def test_sweep_marks_stale_running_as_interrupted(mongo_db):  # noqa: ARG001
    stale = _make_run(status="running", created_minutes_ago=30)
    fresh = _make_run(status="running", created_minutes_ago=2)
    queued_stale = _make_run(status="queued", created_minutes_ago=30)
    completed = _make_run(status="completed", created_minutes_ago=30)
    await stale.insert()
    await fresh.insert()
    await queued_stale.insert()
    await completed.insert()

    n = await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert n == 2  # stale + queued_stale
    refreshed = await ChatRunDoc.find_one(ChatRunDoc.run_id == stale.run_id)
    assert refreshed is not None and refreshed.status == "interrupted"
    refreshed_fresh = await ChatRunDoc.find_one(ChatRunDoc.run_id == fresh.run_id)
    assert refreshed_fresh is not None and refreshed_fresh.status == "running"
    refreshed_completed = await ChatRunDoc.find_one(ChatRunDoc.run_id == completed.run_id)
    assert refreshed_completed is not None and refreshed_completed.status == "completed"


async def test_sweep_with_no_stale_runs_returns_zero(mongo_db):  # noqa: ARG001
    fresh = _make_run(status="running", created_minutes_ago=2)
    await fresh.insert()

    n = await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert n == 0
