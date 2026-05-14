# tests/cloud/test_mission_control_bulk_snooze.py
# Created: 2026-05-13 (feat/mission-control-cleanup) — service-layer tests for
# the Mission Control façade's bulk-snooze endpoint, which delegates per-id to
# ``ee.cloud.tasks.service.agent_update_task`` setting ``due_at`` to the snooze
# timestamp. Covers the success path (``due_at`` actually landed on the Task),
# the mixed Tasks-plus-Nudges payload (non-Tasks skipped), tenant isolation,
# and ISO-validation surfacing as a 422.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import ValidationError
from ee.cloud.mission_control import service as mc_service
from ee.cloud.mission_control.dto import BulkSnoozeRequest
from ee.cloud.tasks import service as tasks_service
from ee.cloud.tasks.dto import AssigneeDTO, CreateTaskRequest
from ee.instinct.store import InstinctStore

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
    return InstinctStore(tmp_path / "mc_bulk_snooze.db")


@pytest.fixture(autouse=True)
def _patch_store_and_pockets(monkeypatch, store: InstinctStore):
    monkeypatch.setattr(mc_service, "get_instinct_store", lambda: store)
    monkeypatch.setattr(
        mc_service.pockets_service,
        "list_pockets",
        AsyncMock(return_value=[{"_id": "p1"}, {"_id": "p2"}]),
    )
    yield


async def test_snoozes_tasks_setting_due_at() -> None:
    """The snooze ``until_iso`` lands on each Task's ``due_at`` column."""
    ctx = _ctx()
    a = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="snooze-a",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
        ),
    )
    until = (datetime.now(UTC) + timedelta(hours=4)).replace(microsecond=0)

    result = await mc_service.agent_bulk_snooze(
        ctx,
        BulkSnoozeRequest(ids=[f"task:{a.id}"], until_iso=until.isoformat()),
    )
    assert result["affected"] == [f"task:{a.id}"]
    assert result["skipped"] == []
    assert "bulk_id" in result

    # The Task row reflects the snooze.
    refreshed = await tasks_service.agent_get_task(ctx, a.id)
    assert refreshed.due_at is not None
    parsed = datetime.fromisoformat(refreshed.due_at)
    assert parsed.replace(microsecond=0) == until


async def test_skips_non_task_ids() -> None:
    """Nudge ids and any non-Task prefix end up in ``skipped``."""
    ctx = _ctx()
    real = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="real",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
        ),
    )
    until = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    result = await mc_service.agent_bulk_snooze(
        ctx,
        BulkSnoozeRequest(
            ids=[
                f"task:{real.id}",
                "nudge:not-a-task",
                "cycle:also-not-a-task",
            ],
            until_iso=until,
        ),
    )
    assert result["affected"] == [f"task:{real.id}"]
    assert set(result["skipped"]) == {
        "nudge:not-a-task",
        "cycle:also-not-a-task",
    }


async def test_tenant_isolation_routes_other_workspace_to_skipped() -> None:
    cross = await tasks_service.agent_create_task(
        _ctx(workspace_id="w2"),
        CreateTaskRequest(
            title="cross-tenant",
            assignee=AssigneeDTO(kind="human", id="u-other", name="u-other"),
        ),
    )
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    result = await mc_service.agent_bulk_snooze(
        _ctx(workspace_id="w1"),
        BulkSnoozeRequest(ids=[f"task:{cross.id}"], until_iso=until),
    )
    assert result["affected"] == []
    assert result["skipped"] == [f"task:{cross.id}"]

    # The task in w2 wasn't touched — its due_at stays None.
    untouched = await tasks_service.agent_get_task(_ctx(workspace_id="w2"), cross.id)
    assert untouched.due_at is None


async def test_invalid_until_iso_raises_validation_error() -> None:
    """A malformed timestamp surfaces as 422 before any task is touched."""
    with pytest.raises(ValidationError) as exc:
        await mc_service.agent_bulk_snooze(
            _ctx(),
            BulkSnoozeRequest(ids=["task:any"], until_iso="not-a-real-iso"),
        )
    assert exc.value.code == "mission_control.invalid_until_iso"
