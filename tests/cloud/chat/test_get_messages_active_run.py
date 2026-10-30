"""``get_messages`` surfaces the newest non-terminal run for the scope."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat import message_service
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("mongo_db")]


async def _make_group(
    *,
    workspace: str = "w1",
    owner: str = "u1",
    members: list[str] | None = None,
    type: str = "private",
) -> _GroupDoc:
    if members is None:
        members = [owner]
    doc = _GroupDoc(
        workspace=workspace,
        name="G",
        slug="g",
        type=type,
        members=members,
        owner=owner,
    )
    await doc.insert()
    return doc


def _spec(
    *,
    run_id: str,
    context_type: str,
    scope_id: str,
    workspace: str = "w1",
) -> RunSpec:
    return RunSpec(
        run_id=run_id,
        workspace_id=workspace,
        context_type=context_type,
        scope_id=scope_id,
        session_key=f"{context_type}:{scope_id}",
        group=scope_id,
        user_id="u1",
        agent_id="agent-x",
        client_message_id=f"c-{run_id}",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def test_get_messages_includes_active_run_for_group() -> None:
    group = await _make_group()

    await run_service.create_run(_spec(run_id="grp", context_type="group", scope_id=str(group.id)))
    await run_service.mark_running("grp")

    result = await message_service.get_messages(str(group.id), "u1")

    assert result["active_run"] == {"run_id": "grp", "status": "running"}


async def test_get_messages_includes_active_run_for_dm() -> None:
    """DMs share the group document but ``post_agent_chat`` writes
    ``context_type="dm"`` on the run — both must surface here."""
    group = await _make_group(type="dm")

    await run_service.create_run(_spec(run_id="dm", context_type="dm", scope_id=str(group.id)))
    # Leave status as ``queued`` — both queued and running must surface.

    result = await message_service.get_messages(str(group.id), "u1")

    assert result["active_run"] == {"run_id": "dm", "status": "queued"}


async def test_get_messages_active_run_null_when_no_run() -> None:
    group = await _make_group()
    result = await message_service.get_messages(str(group.id), "u1")
    assert result["active_run"] is None


async def test_get_messages_active_run_null_when_completed() -> None:
    group = await _make_group()

    await run_service.create_run(_spec(run_id="done", context_type="group", scope_id=str(group.id)))
    await run_service.mark_completed("done", assistant_message_id="m2", partial_text="ok")

    result = await message_service.get_messages(str(group.id), "u1")
    assert result["active_run"] is None


async def test_get_messages_active_run_isolated_by_workspace() -> None:
    """A run in a different workspace for the same scope_id must NOT
    surface — guards the tenancy filter."""
    group = await _make_group()
    await run_service.create_run(
        _spec(
            run_id="other",
            context_type="group",
            scope_id=str(group.id),
            workspace="other-ws",
        )
    )
    await run_service.mark_running("other")

    result = await message_service.get_messages(str(group.id), "u1")
    assert result["active_run"] is None
