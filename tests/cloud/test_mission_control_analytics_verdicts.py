# tests/cloud/test_mission_control_analytics_verdicts.py — the evals slice:
# agent_analytics buckets Task.verify["verdict"]["status"] into solved/partial/
# not_solved/unknown, computes solved_rate over CHECKABLE verdicts only
# (unknown excluded, reported separately), returns None when nothing is
# checkable (UI renders an em dash, never a fake 0%/100%), and stays
# tenant-isolated. Created: 2026-07-11 (feat/evals-solved-rate).
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pocketpaw_ee.cloud.mission_control import service as mc_service
from pocketpaw_ee.cloud.models.task import Task as TaskDoc
from pocketpaw_ee.cloud.models.task import TaskAssignee as TaskAssigneeDoc

_WS = "ws-verdicts"

# NOTE: the suite runs pytest-asyncio in AUTO mode — async tests need no marks.


async def _task(workspace_id: str, verdict_status: str | None) -> None:
    doc = TaskDoc(
        workspace_id=workspace_id,
        creator_id="u1",
        title="t",
        summary="s",
        assignee=TaskAssigneeDoc(kind="agent", id="a1", name="Bot"),
        assignee_id="a1",
        assignee_kind="agent",
        status="done",
        verify={"verdict": {"status": verdict_status}} if verdict_status else {},
    )
    await doc.insert()


def _ctx(workspace_id: str = _WS) -> SimpleNamespace:
    return SimpleNamespace(workspace_id=workspace_id, user_id="u1")


def _no_pockets():
    return patch.object(mc_service.pockets_service, "list_pockets", new=AsyncMock(return_value=[]))


async def test_solved_rate_excludes_unknown(mongo_db):  # noqa: ARG001 — Beanie init
    # 2 solved, 1 partial, 1 not_solved (checkable=4) + 2 unknown-ish (one
    # explicit unknown, one verify=={} from a loop-off task).
    for status in ("solved", "solved", "partial", "not_solved", "unknown", None):
        await _task(_WS, status)

    with _no_pockets():
        res = await mc_service.agent_analytics(_ctx(), window="7d")

    assert res.solved_count == 2
    assert res.partial_count == 1
    assert res.not_solved_count == 1
    assert res.unknown_count == 1  # verify=={} carries no verdict at all
    assert res.solved_rate == 50.0  # 2 / (2+1+1), unknown excluded


async def test_solved_rate_none_when_nothing_checkable(mongo_db):  # noqa: ARG001
    await _task(_WS, "unknown")
    await _task(_WS, None)

    with _no_pockets():
        res = await mc_service.agent_analytics(_ctx(), window="7d")

    assert res.solved_rate is None
    assert res.unknown_count == 1


async def test_verdicts_are_tenant_isolated(mongo_db):  # noqa: ARG001
    await _task(_WS, "solved")
    await _task("ws-other", "not_solved")  # must never leak into _WS's rollup

    with _no_pockets():
        res = await mc_service.agent_analytics(_ctx(), window="7d")

    assert res.solved_count == 1
    assert res.not_solved_count == 0
    assert res.solved_rate == 100.0
