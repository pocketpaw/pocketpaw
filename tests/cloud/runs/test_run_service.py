"""CRUD tests for the chat-run service."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

pytestmark = pytest.mark.asyncio


def _spec(run_id: str = "r1", scope_id: str = "s1") -> RunSpec:
    return RunSpec(
        run_id=run_id,
        workspace_id="w1",
        context_type="session",
        scope_id=scope_id,
        session_key=f"session:{scope_id}",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id=f"c-{run_id}",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def test_create_and_get(mongo_db):
    await run_service.create_run(_spec())
    doc = await run_service.get_run("r1")
    assert doc.status == "queued"


async def test_mark_running_then_completed(mongo_db):
    await run_service.create_run(_spec())
    await run_service.mark_running("r1")
    await run_service.mark_completed("r1", assistant_message_id="m2", partial_text="done")
    doc = await run_service.get_run("r1")
    assert doc.status == "completed"
    assert doc.assistant_message_id == "m2"
    assert doc.ended_at is not None


async def test_find_active_run_for_scope_returns_newest_nonterminal(mongo_db):
    await run_service.create_run(_spec(run_id="old"))
    await run_service.mark_completed("old", assistant_message_id=None, partial_text="")
    await run_service.create_run(_spec(run_id="live"))
    await run_service.mark_running("live")
    active = await run_service.find_active_run_for_scope(
        workspace_id="w1", context_type="session", scope_id="s1"
    )
    assert active is not None and active.run_id == "live"


async def test_create_run_is_idempotent_on_client_message_id(mongo_db):
    spec = _spec()
    first = await run_service.create_run(spec)
    second = await run_service.create_run(spec)  # same client_message_id
    assert first.run_id == second.run_id


async def test_create_run_concurrent_same_client_message_id_returns_one(mongo_db):
    import asyncio

    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    spec_a = _spec(run_id="ra")
    spec_b = _spec(run_id="rb").model_copy(update={"client_message_id": spec_a.client_message_id})

    a, b = await asyncio.gather(
        run_service.create_run(spec_a),
        run_service.create_run(spec_b),
    )
    assert a.run_id == b.run_id
    rows = await ChatRunDoc.find(
        ChatRunDoc.workspace == spec_a.workspace_id,
        ChatRunDoc.client_message_id == spec_a.client_message_id,
    ).to_list()
    assert len(rows) == 1
