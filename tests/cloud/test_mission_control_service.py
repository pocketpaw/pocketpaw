# tests/cloud/test_mission_control_service.py
# Created: 2026-05-13 (feat/mission-control-facade) — unit-level coverage of
# the mission_control façade service. Asserts WorkItem projection, section
# routing, agent/pocket/section filters, tenancy gating against
# pockets_service.list_pockets, and outcome aggregation math.
# Updated: 2026-05-13 (feat/mission-control-cleanup) — dropped the
# TestStubEndpoints block now that bulk-reassign and bulk-snooze delegate
# to the Tasks service. Full coverage of those endpoints lives in
# test_mission_control_bulk_reassign.py and test_mission_control_bulk_snooze.py.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import ValidationError
from ee.cloud.mission_control import service as mc_service
from ee.cloud.mission_control.domain import WorkItemSection, WorkItemStatus
from ee.cloud.mission_control.dto import (
    BulkActionRequest,
    ListActivityRequest,
    ListWorkItemsRequest,
    OutcomesQueryRequest,
)
from ee.instinct.models import ActionTrigger
from ee.instinct.store import InstinctStore


def _ctx(workspace_id: str | None = "w1", user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="req-test",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _trigger(source: str = "claude") -> ActionTrigger:
    return ActionTrigger(type="agent", source=source, reason="mc test")


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "mc_service.db")


@pytest.fixture(autouse=True)
def _patch_store_and_pockets(monkeypatch, store: InstinctStore):
    """Wire the service's two read sources to test doubles.

    - ``get_instinct_store`` → the per-test SQLite store
    - ``pockets_service.list_pockets`` → AsyncMock returning the seeded
      pockets so we don't need a Mongo fixture for façade-level tests
    """
    monkeypatch.setattr(mc_service, "get_instinct_store", lambda: store)
    monkeypatch.setattr(
        mc_service.pockets_service,
        "list_pockets",
        AsyncMock(return_value=[{"_id": "p1"}, {"_id": "p2"}]),
    )
    yield


# ---------------------------------------------------------------------------
# agent_list_work_items
# ---------------------------------------------------------------------------


class TestListWorkItems:
    @pytest.mark.asyncio
    async def test_workspace_required_or_validation_error(self, store: InstinctStore) -> None:
        with pytest.raises(ValidationError) as exc_info:
            await mc_service.agent_list_work_items(_ctx(workspace_id=None), {})
        assert exc_info.value.code == "mission_control.workspace_required"

    @pytest.mark.asyncio
    async def test_projects_pending_action_to_tray_section(self, store: InstinctStore) -> None:
        await store.propose("p1", "Order more wool", "low stock", "order 30", _trigger())
        items = await mc_service.agent_list_work_items(_ctx(), {})
        assert len(items) == 1
        item = items[0]
        assert item.section == WorkItemSection.TRAY
        assert item.status == WorkItemStatus.AWAITING_APPROVAL
        assert item.title == "Order more wool"
        assert item.source_kind == "nudge"
        assert item.pocket_id == "p1"

    @pytest.mark.asyncio
    async def test_section_filter_narrows_to_one_pane(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "pending one", "", "", _trigger())
        b = await store.propose("p1", "approved one", "", "", _trigger())
        await store.approve(b.id)

        tray = await mc_service.agent_list_work_items(
            _ctx(), ListWorkItemsRequest(section=WorkItemSection.TRAY)
        )
        assert [it.source_id for it in tray] == [a.id]
        pawprints = await mc_service.agent_list_work_items(
            _ctx(), ListWorkItemsRequest(section=WorkItemSection.PAWPRINTS)
        )
        assert [it.source_id for it in pawprints] == [b.id]

    @pytest.mark.asyncio
    async def test_pocket_filter_excludes_other_pockets(self, store: InstinctStore) -> None:
        await store.propose("p1", "p1 item", "", "", _trigger())
        await store.propose("p2", "p2 item", "", "", _trigger())
        out = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(pocket="p1"))
        assert {it.pocket_id for it in out} == {"p1"}

    @pytest.mark.asyncio
    async def test_agent_filter_matches_trigger_source(self, store: InstinctStore) -> None:
        await store.propose("p1", "by claude", "", "", _trigger("claude"))
        await store.propose("p1", "by sage", "", "", _trigger("sage"))
        out = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(agent="sage"))
        assert [it.title for it in out] == ["by sage"]

    @pytest.mark.asyncio
    async def test_tenancy_filters_out_invisible_pockets(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        # Restrict visible pockets to p1 only.
        monkeypatch.setattr(
            mc_service.pockets_service,
            "list_pockets",
            AsyncMock(return_value=[{"_id": "p1"}]),
        )
        await store.propose("p1", "visible", "", "", _trigger())
        await store.propose("p2", "hidden", "", "", _trigger())
        out = await mc_service.agent_list_work_items(_ctx(), {})
        assert [it.title for it in out] == ["visible"]

    @pytest.mark.asyncio
    async def test_returns_empty_when_workspace_has_no_pockets(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        monkeypatch.setattr(
            mc_service.pockets_service, "list_pockets", AsyncMock(return_value=[])
        )
        await store.propose("p1", "would surface", "", "", _trigger())
        out = await mc_service.agent_list_work_items(_ctx(), {})
        assert out == []

    @pytest.mark.asyncio
    async def test_limit_caps_returned_items(self, store: InstinctStore) -> None:
        for i in range(10):
            await store.propose("p1", f"item-{i}", "", "", _trigger())
        out = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(limit=3))
        assert len(out) == 3


# ---------------------------------------------------------------------------
# bulk_approve / bulk_reject
# ---------------------------------------------------------------------------


class TestBulkApproveService:
    @pytest.mark.asyncio
    async def test_approves_visible_actions(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        b = await store.propose("p1", "B", "", "", _trigger())
        result = await mc_service.agent_bulk_approve(
            _ctx(), BulkActionRequest(ids=[a.id, b.id])
        )
        assert "bulk_id" in result
        approved_ids = {row["id"] for row in result["approved"]}
        assert approved_ids == {a.id, b.id}

    @pytest.mark.asyncio
    async def test_blocks_actions_in_invisible_pockets(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        monkeypatch.setattr(
            mc_service.pockets_service,
            "list_pockets",
            AsyncMock(return_value=[{"_id": "p1"}]),
        )
        a = await store.propose("p1", "A", "", "", _trigger())
        hidden = await store.propose("p2", "B", "", "", _trigger())
        result = await mc_service.agent_bulk_approve(
            _ctx(), BulkActionRequest(ids=[a.id, hidden.id])
        )
        assert {row["id"] for row in result["approved"]} == {a.id}
        assert hidden.id in result["missing"]

    @pytest.mark.asyncio
    async def test_bulk_reject_requires_reason(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        with pytest.raises(ValidationError) as exc:
            await mc_service.agent_bulk_reject(
                _ctx(),
                BulkActionRequest(ids=[a.id], reason=None),
            )
        assert exc.value.code == "mission_control.reason_required"


# ---------------------------------------------------------------------------
# outcomes summary
# ---------------------------------------------------------------------------


class TestOutcomesSummary:
    @pytest.mark.asyncio
    async def test_counts_per_status_for_visible_pockets(self, store: InstinctStore) -> None:
        approved = await store.propose("p1", "A", "", "", _trigger())
        rejected = await store.propose("p1", "B", "", "", _trigger())
        pending = await store.propose("p1", "C", "", "", _trigger())  # noqa: F841
        await store.approve(approved.id)
        await store.reject(rejected.id, reason="nah")

        summary = await mc_service.agent_outcomes_summary(
            _ctx(), OutcomesQueryRequest(window="24h")
        )
        assert summary.total == 3
        assert summary.approved == 1
        assert summary.rejected == 1
        assert summary.pending == 1
        assert summary.executed == 0
        assert summary.failed == 0

    @pytest.mark.asyncio
    async def test_window_filter_excludes_old_rows(self, store: InstinctStore) -> None:
        # Seed a row whose created_at SQL default is 'now', but force an
        # older updated_at on it via direct DB write so the window cutoff
        # excludes it.
        import aiosqlite

        a = await store.propose("p1", "old", "", "", _trigger())
        old_ts = (datetime.now() - timedelta(days=30)).isoformat()
        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET created_at = ?, updated_at = ? WHERE id = ?",
                (old_ts, old_ts, a.id),
            )
            await db.commit()
        await store.propose("p1", "recent", "", "", _trigger())

        summary = await mc_service.agent_outcomes_summary(
            _ctx(), OutcomesQueryRequest(window="24h")
        )
        # Only the "recent" row falls in the 24h window.
        assert summary.total == 1


# ---------------------------------------------------------------------------
# activity feed
# ---------------------------------------------------------------------------


class TestListActivity:
    @pytest.mark.asyncio
    async def test_returns_buffer_entries_newest_first(self, store: InstinctStore) -> None:
        import time

        from ee.cloud.activity.buffer import ActivityEvent, get_buffer

        buf = get_buffer()
        buf.reset()
        now = time.time()
        for i in range(3):
            buf.push(
                ActivityEvent(
                    workspace_id="w1",
                    kind="thinking",
                    agent_id="a1",
                    summary=f"step {i}",
                    pocket_id=None,
                    ts=now + i,
                )
            )
        out = await mc_service.agent_list_activity(_ctx(), ListActivityRequest(limit=10))
        assert [e.summary for e in out] == ["step 2", "step 1", "step 0"]
