# test_mission_control_project_filter.py — project_id filter on
# GET /mission-control/items.
# Created: 2026-05-16 — Mission Control backend completion. Verifies that
#   passing ``project_id`` to ``agent_list_work_items`` narrows the
#   Nudge half of the feed via the visible-pocket set (a Nudge's project
#   assignment comes from its parent pocket).

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.mission_control import service as mc_service
from pocketpaw_ee.cloud.mission_control.dto import ListWorkItemsRequest

from pocketpaw.instinct.models import ActionTrigger
from pocketpaw.instinct.store import InstinctStore


def _ctx(workspace_id: str | None = "w1", user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="req-test",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _trigger(source: str = "claude") -> ActionTrigger:
    return ActionTrigger(type="agent", source=source, reason="proj test")


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "mc_project.db")


@pytest.fixture(autouse=True)
def _patch_store(monkeypatch, store: InstinctStore):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(mc_service, "get_instinct_store", lambda: store)
    # Façade composes Tasks alongside Nudges; stub the Tasks read so the
    # Instinct-only project-filter tests don't need a Beanie test DB.
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.tasks.service.agent_list_tasks", AsyncMock(return_value=[])
    )
    yield


@pytest.mark.asyncio
async def test_project_id_filter_narrows_visible_pockets(monkeypatch, store: InstinctStore) -> None:
    """Nudges inherit their project assignment from the parent pocket.

    When the caller asks for ``project_id=proj-A``, the visible-pocket
    set is pre-filtered to pockets in that project, and Nudges hanging
    off pockets in OTHER projects don't surface.
    """

    # Pocket p1 lives in project proj-A; pocket p2 lives in project proj-B.
    async def _list_pockets(workspace_id, user_id, *, project_id=None):
        if project_id == "proj-A":
            return [{"_id": "p1"}]
        if project_id == "proj-B":
            return [{"_id": "p2"}]
        # Unfiltered call returns both
        return [{"_id": "p1"}, {"_id": "p2"}]

    monkeypatch.setattr(mc_service.pockets_service, "list_pockets", _list_pockets)

    await store.propose("p1", "in-A", "", "", _trigger())
    await store.propose("p2", "in-B", "", "", _trigger())

    in_a = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(project_id="proj-A"))
    assert {it.title for it in in_a} == {"in-A"}

    in_b = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(project_id="proj-B"))
    assert {it.title for it in in_b} == {"in-B"}

    # Without the filter, both surface.
    both = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest())
    assert {it.title for it in both} == {"in-A", "in-B"}


@pytest.mark.asyncio
async def test_project_id_empty_string_filters_for_unassigned(
    monkeypatch, store: InstinctStore
) -> None:
    """Passing an empty string narrows to the Mission Control 'Unassigned'
    bucket — pockets without a ``project_id`` reference."""

    seen_project_ids: list[str | None] = []

    async def _list_pockets(workspace_id, user_id, *, project_id=None):
        seen_project_ids.append(project_id)
        # Only the "unassigned" call returns a pocket here.
        if project_id == "":
            return [{"_id": "p-unassigned"}]
        return []

    monkeypatch.setattr(mc_service.pockets_service, "list_pockets", _list_pockets)
    await store.propose("p-unassigned", "loose", "", "", _trigger())

    items = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(project_id=""))
    assert {it.title for it in items} == {"loose"}
    assert seen_project_ids == [""]


@pytest.mark.asyncio
async def test_returns_empty_when_no_pockets_in_project(monkeypatch, store: InstinctStore) -> None:
    """If the project has zero visible pockets, the feed comes back
    empty — Mission Control never falls back to the cross-project view."""

    async def _list_pockets(workspace_id, user_id, *, project_id=None):
        return []  # No pockets visible under this filter

    monkeypatch.setattr(
        mc_service.pockets_service,
        "list_pockets",
        AsyncMock(side_effect=_list_pockets),
    )
    await store.propose("p1", "should not surface", "", "", _trigger())
    out = await mc_service.agent_list_work_items(
        _ctx(), ListWorkItemsRequest(project_id="proj-empty")
    )
    assert out == []
