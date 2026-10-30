# tests/cloud/credits/test_early_access_unbilled_paths.py: the agent runs that
# bypass ``execute_run`` refuse an unpaid early-access workspace.
#
# The group/DM agent bridge, the three /files AI-edit routes and the planner call
# ``pool.run`` directly. None of them bills the ledger, so a positive wallet never
# runs down and the credit gate alone would let a free workspace run them without
# limit. Each refuses with ``early_access.not_available`` before any model call,
# while a paid workspace still runs.
#
# Real ledger, real guard, real entitlements on the in-memory Mongo; the
# early-access harness (``ea_user``, ``early_access_posture``) comes from
# test_early_access_cap.py through this package's conftest. Only the model run
# itself and transport side effects are stubbed.
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


async def _make_paid(workspace_id: str) -> None:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = await Workspace.get(workspace_id)
    ws.plan = "pro"
    await ws.save()


# ---------------------------------------------------------------------------
# Group/DM agent bridge
# ---------------------------------------------------------------------------


async def _dispatch(workspace_id: str):
    from pocketpaw_ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["u1"], agents=[SimpleNamespace(agent_id="agent-a", respond_mode="smart")]
    )
    should = AsyncMock(return_value=True)
    run = AsyncMock(return_value=None)
    emit = AsyncMock()
    with (
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=group),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond", new=should),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=run),
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=emit),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "g1",
                "sender_id": "u1",
                "content": "hey team",
                "mentions": [],
                "workspace_id": workspace_id,
            }
        )
    return should, run, emit


async def test_bridge_refuses_unpaid_workspace_before_any_model_call(ea_user, early_access_posture):
    """Mutation: drop the early-access check from the bridge gate."""
    should, run, emit = await _dispatch(ea_user.workspace_id)

    should.assert_not_awaited()
    run.assert_not_awaited()
    assert emit.await_count == 1
    assert emit.await_args.args[0].data["code"] == "early_access.not_available"


async def test_bridge_runs_for_paid_workspace(ea_user, early_access_posture):
    await _make_paid(ea_user.workspace_id)

    should, run, emit = await _dispatch(ea_user.workspace_id)

    run.assert_awaited()
    emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# /files AI edits
# ---------------------------------------------------------------------------


def _edit_calls():
    from pocketpaw_ee.cloud.file_versions import router
    from pocketpaw_ee.cloud.file_versions.dto import AiEditRequest
    from pocketpaw_ee.cloud.file_versions.slides_dto import SlidesEditRequest
    from pocketpaw_ee.cloud.file_versions.spreadsheet_dto import SpreadsheetEditRequest

    return {
        "ai-edit": lambda ws: router.ai_edit_file(
            "f1", AiEditRequest(content="", prompt="tidy"), user_id="u1", workspace_id=ws
        ),
        "spreadsheet-edit": lambda ws: router.spreadsheet_ai_edit(
            "f1", SpreadsheetEditRequest(content="", prompt="tidy"), user_id="u1", workspace_id=ws
        ),
        "slides-edit": lambda ws: router.slides_ai_edit(
            "f1", SlidesEditRequest(content={}, prompt="tidy"), user_id="u1", workspace_id=ws
        ),
    }


@pytest.mark.parametrize("route", ["ai-edit", "spreadsheet-edit", "slides-edit"])
async def test_files_edit_refuses_unpaid_workspace(route, ea_user, early_access_posture):
    """Mutation: drop ``assert_early_access_allowed`` from any of the routes."""
    from pocketpaw_ee.cloud._core.errors import CloudError

    resolve = AsyncMock(return_value="agent-a")
    with (
        patch("pocketpaw_ee.cloud.file_versions.router._resolve_default_agent", new=resolve),
        patch("pocketpaw.agents.pool.get_agent_pool", new=MagicMock()),
        pytest.raises(CloudError) as caught,
    ):
        await _edit_calls()[route](ea_user.workspace_id)

    assert caught.value.status_code == 403
    assert caught.value.code == "early_access.not_available"
    resolve.assert_not_awaited()


@pytest.mark.parametrize("route", ["ai-edit", "spreadsheet-edit", "slides-edit"])
async def test_files_edit_runs_for_paid_workspace(route, ea_user, early_access_posture):
    await _make_paid(ea_user.workspace_id)
    # No agent: the route answers 400 after the gate, before any model call.
    resolve = AsyncMock(return_value=None)
    with (
        patch("pocketpaw_ee.cloud.file_versions.router._resolve_default_agent", new=resolve),
        patch("pocketpaw.agents.pool.get_agent_pool", new=MagicMock()),
    ):
        await _edit_calls()[route](ea_user.workspace_id)

    resolve.assert_awaited()


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class _Pool:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, **_kwargs):
        from pocketpaw.agents.protocol import AgentEvent

        self.calls += 1

        async def _gen():
            yield AgentEvent(type="message", content="done")
            yield AgentEvent(type="done", content="")

        return _gen()


async def _run_planner(workspace_id: str) -> _Pool:
    from pocketpaw_ee.cloud.models.task import Task, TaskAssignee
    from pocketpaw_ee.cloud.planner import service as planner_service

    await Task(
        workspace_id=workspace_id,
        project_id="p-ea",
        creator_id="u1",
        title="Write the summary",
        summary="Write it.",
        assignee=TaskAssignee(kind="agent", id="agent-a", name="PlanBot"),
        assignee_id="agent-a",
        assignee_kind="agent",
        status="proposed",
    ).insert()
    pool = _Pool()
    with (
        patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool),
        patch.object(planner_service, "_save_task_output_file", AsyncMock()),
        patch.object(planner_service, "_scan_and_upload_agent_files", AsyncMock(return_value=[])),
    ):
        await planner_service._execute_ready_plan_tasks(
            workspace_id=workspace_id, project_id="p-ea"
        )
    return pool


async def test_planner_skips_unpaid_workspace(ea_user, early_access_posture, recording_bus):
    """Mutation: drop the early-access check from the planner gate."""
    pool = await _run_planner(ea_user.workspace_id)

    assert pool.calls == 0


async def test_planner_runs_for_paid_workspace(ea_user, early_access_posture, recording_bus):
    await _make_paid(ea_user.workspace_id)

    pool = await _run_planner(ea_user.workspace_id)

    assert pool.calls == 1
