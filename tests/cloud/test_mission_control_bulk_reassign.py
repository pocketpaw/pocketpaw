# tests/cloud/test_mission_control_bulk_reassign.py
# Created: 2026-05-13 (feat/mission-control-cleanup) — service-layer tests for
# the Mission Control façade's bulk-reassign endpoint, which delegates per-id
# to ``ee.cloud.tasks.service.agent_reassign_task``. Covers the success path,
# the mixed Tasks-plus-Nudges payload (non-Tasks skipped), and the workspace
# tenancy guarantee (cross-workspace ids land in ``skipped``).
# Updated: 2026-05-13 (fix/mission-control-followup-nits) — added coverage
# for the bare-id branch in ``_classify_task_id`` (no ``task:`` prefix is
# accepted as a Task id for forward compatibility). PR #1097's reviewer
# flagged the branch as untested.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.mission_control import service as mc_service
from pocketpaw_ee.cloud.mission_control.dto import BulkReassignRequest
from pocketpaw_ee.cloud.tasks import service as tasks_service
from pocketpaw_ee.cloud.tasks.dto import AssigneeDTO, CreateTaskRequest

from pocketpaw.instinct.store import InstinctStore

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace_id: str = "w1", user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="req-test",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "mc_bulk_reassign.db")


@pytest.fixture(autouse=True)
def _patch_store_and_pockets(monkeypatch, store: InstinctStore):
    """Same test doubles as the other Mission Control suites."""
    monkeypatch.setattr(mc_service, "get_instinct_store", lambda: store)
    monkeypatch.setattr(
        mc_service.pockets_service,
        "list_pockets",
        AsyncMock(return_value=[{"_id": "p1"}, {"_id": "p2"}]),
    )
    yield


def _to(kind: str = "agent", task_id: str = "agent-rover", name: str = "Rover"):
    return BulkReassignRequest.Assignee(kind=kind, id=task_id, name=name)


async def test_reassigns_tasks_emitting_per_row_event(recording_bus) -> None:
    """Each prefixed Task id maps to one agent_reassign_task call. The
    response carries ``affected`` ids and a single ``bulk_id``."""
    ctx = _ctx()
    a = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="t-a",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
        ),
    )
    b = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="t-b",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
        ),
    )

    recording_bus.events.clear()
    result = await mc_service.agent_bulk_reassign(
        ctx,
        BulkReassignRequest(ids=[f"task:{a.id}", f"task:{b.id}"], to=_to()),
    )
    assert set(result["affected"]) == {f"task:{a.id}", f"task:{b.id}"}
    assert result["skipped"] == []
    assert "bulk_id" in result

    # Tasks service emits TaskUpdated per row; the bulk path doesn't add
    # its own event on top.
    from pocketpaw_ee.cloud._core.realtime.events import TaskUpdated

    updates = [e for e in recording_bus.events if isinstance(e, TaskUpdated)]
    assert len(updates) == 2

    # And the underlying Task rows reflect the new assignee.
    refreshed_a = await tasks_service.agent_get_task(ctx, a.id)
    assert refreshed_a.assignee.kind == "agent"
    assert refreshed_a.assignee.id == "agent-rover"


async def test_skips_non_task_ids() -> None:
    """``nudge:<id>``, bare-prefix garbage, and unknown task ids land in
    ``skipped`` rather than raising. The affected list only carries the
    real Tasks."""
    ctx = _ctx()
    real = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="real",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
        ),
    )

    result = await mc_service.agent_bulk_reassign(
        ctx,
        BulkReassignRequest(
            ids=[
                f"task:{real.id}",
                "nudge:not-a-task",
                "cycle:also-not-a-task",
            ],
            to=_to(),
        ),
    )
    assert result["affected"] == [f"task:{real.id}"]
    assert set(result["skipped"]) == {
        "nudge:not-a-task",
        "cycle:also-not-a-task",
    }


async def test_classifies_bare_id_as_task() -> None:
    """A bare id (no ``task:`` prefix) is accepted as a Task id for
    forward compatibility — some callers pre-strip the prefix before
    handing the selection back to the bulk endpoint."""
    ctx = _ctx()
    real = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="bare",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
        ),
    )

    result = await mc_service.agent_bulk_reassign(
        ctx,
        BulkReassignRequest(ids=[real.id], to=_to()),
    )
    assert result["affected"] == [real.id]
    assert result["skipped"] == []

    # And the bare-id reassignment landed on the underlying row.
    refreshed = await tasks_service.agent_get_task(ctx, real.id)
    assert refreshed.assignee.kind == "agent"
    assert refreshed.assignee.id == "agent-rover"


async def test_tenant_isolation_routes_other_workspace_to_skipped() -> None:
    """A Task in workspace w2 is invisible to a w1 caller; the bulk
    endpoint reports it as ``skipped`` instead of leaking the reassign
    across tenants."""
    cross = await tasks_service.agent_create_task(
        _ctx(workspace_id="w2"),
        CreateTaskRequest(
            title="cross-tenant",
            assignee=AssigneeDTO(kind="human", id="u-other", name="u-other"),
        ),
    )
    result = await mc_service.agent_bulk_reassign(
        _ctx(workspace_id="w1"),
        BulkReassignRequest(ids=[f"task:{cross.id}"], to=_to()),
    )
    assert result["affected"] == []
    assert result["skipped"] == [f"task:{cross.id}"]

    # The task in w2 wasn't touched — its original assignee survives.
    untouched = await tasks_service.agent_get_task(_ctx(workspace_id="w2"), cross.id)
    assert untouched.assignee.id == "u-other"
