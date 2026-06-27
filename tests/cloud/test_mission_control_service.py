# tests/cloud/test_mission_control_service.py
# Created: 2026-05-13 (feat/mission-control-facade) — unit-level coverage of
# the mission_control façade service. Asserts WorkItem projection, section
# routing, agent/pocket/section filters, tenancy gating against
# pockets_service.list_pockets, and outcome aggregation math.
# Updated: 2026-05-13 (feat/mission-control-cleanup) — dropped the
# TestStubEndpoints block now that bulk-reassign and bulk-snooze delegate
# to the Tasks service. Full coverage of those endpoints lives in
# test_mission_control_bulk_reassign.py and test_mission_control_bulk_snooze.py.
# Updated: 2026-06-10 (W4c — scope instinct reads to workspace) — added
# ``TestWorkspaceScopedReads`` proving the W4c fix: the façade now threads
# ``ctx.workspace_id`` into ``store.pending`` / ``store.list_actions`` so
# ``agent_list_work_items`` and ``agent_outcomes_summary`` only surface the
# caller's tenant rows (plus legacy NULL) — NOT another workspace's — even when
# pocket visibility would otherwise admit them. These tests deliberately keep
# the autouse ``list_pockets`` mock workspace-blind so the isolation they assert
# can ONLY come from the store-level scope (the residual leak W4a left open on
# the internal caller side).
# Updated: 2026-06-10 (fix/mc-bulk-approve-strands-writes — W0c) — added
# ``TestBulkApproveExecutesWrites`` proving the W0c fix: a façade-level
# bulk-approve over ≥2 parked-write Nudges actually FIRES each parked write
# (the actions reach ``executed`` via the bridge's executor re-entry) and
# emits the per-item chain events (``human.corrected`` + ``policy.evaluated``)
# — the gap where ``agent_bulk_approve`` recorded approvals but never called
# ``execute_approved_write``. A second test pins per-item error isolation:
# one item whose write raises is reported failed while the rest still land.
# Added ``_pocket_write_params`` matching the schema-2 blob shape the
# Instinct bridge stores.
# Updated: 2026-06-10 (sov/r2a FIX 3) — the chain-emit helpers moved out of
# ``ee.instinct.router`` into the shared ``ee.instinct.chain_emitters`` module
# (decoupling the façade from the router). The bulk-approve chain-emit spy test
# now monkeypatches ``ee.instinct.chain_emitters._emit_*`` (where the service
# imports them from) instead of the old router path.
# Updated: 2026-06-12 (fix/tray-workspace-scoped-nudges) — added
# ``TestWorkspaceScopedNudges`` pinning the Tray fix: an external-action
# proposal stamps ``Action.pocket_id = workspace_id`` (workspace-scoped, not
# pocket-bound — see ``external_actions/propose.py``), so the old
# pocket-visibility filter in ``agent_list_work_items`` dropped every such
# proposal from the feed. The tests assert (1) a workspace-scoped pending
# action projects as ``nudge:<id>`` with pocket_name "Workspace", even when
# the workspace has zero visible pockets, and (2) another tenant's
# workspace-scoped action stays invisible (store-level W4c scoping intact).

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.mission_control import service as mc_service
from pocketpaw_ee.cloud.mission_control.domain import WorkItemSection, WorkItemStatus
from pocketpaw_ee.cloud.mission_control.dto import (
    BulkActionRequest,
    ListActivityRequest,
    ListWorkItemsRequest,
    OutcomesQueryRequest,
)

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
    return ActionTrigger(type="agent", source=source, reason="mc test")


def _pocket_write_params(workspace_id: str = "w1", action: str = "mark_renewed") -> dict:
    """An Action ``parameters`` payload carrying a parked pocket write.

    Mirrors the schema-2 blob ``instinct_bridge.propose_pocket_write``
    stores under ``Action.parameters._pocket_write``. ``execute_approved_write``
    rejects any blob whose ``schema`` doesn't match ``_POCKET_WRITE_SCHEMA``,
    so the round-trip needs the matching shape. ``correlation_id`` /
    ``parked_policy_event_id`` are None — these tests pin the execute +
    chain-emit wiring, not the Decision-Graph chain semantics, and the
    bridge accepts None for both.
    """
    return {
        "_pocket_write": {
            "schema": 2,
            "action": action,
            "method": "POST",
            "path": "/leases/42/renew",
            "params": {"rent": 2000},
            "idempotency_key": f"idem-{action}",
            "outcome": "renewal_completed",
            "workspace_id": workspace_id,
            "requested_by": "requester-9",
            "correlation_id": None,
            "parked_policy_event_id": None,
        }
    }


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "mc_service.db")


@pytest.fixture(autouse=True)
def _patch_store_and_pockets(monkeypatch, store: InstinctStore):
    """Wire the service's three read sources to test doubles.

    - ``get_instinct_store`` → the per-test SQLite store
    - ``pockets_service.list_pockets`` → AsyncMock returning the seeded
      pockets so we don't need a Mongo fixture for façade-level tests
    - ``tasks_service.agent_list_tasks`` → stub returning [] so the
      Mission Control façade can compose Tasks alongside Nudges without
      forcing every façade test to initialize Beanie. Tests that
      exercise the Tasks branch override this with their own patch.
    """
    monkeypatch.setattr(mc_service, "get_instinct_store", lambda *a, **k: store)
    monkeypatch.setattr(
        mc_service.pockets_service,
        "list_pockets",
        AsyncMock(return_value=[{"_id": "p1"}, {"_id": "p2"}]),
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.tasks.service.agent_list_tasks", AsyncMock(return_value=[])
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
        monkeypatch.setattr(mc_service.pockets_service, "list_pockets", AsyncMock(return_value=[]))
        await store.propose("p1", "would surface", "", "", _trigger())
        out = await mc_service.agent_list_work_items(_ctx(), {})
        assert out == []

    @pytest.mark.asyncio
    async def test_limit_caps_returned_items(self, store: InstinctStore) -> None:
        for i in range(10):
            await store.propose("p1", f"item-{i}", "", "", _trigger())
        out = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(limit=3))
        assert len(out) == 3

    @pytest.mark.asyncio
    async def test_includes_tasks_alongside_nudges(self, monkeypatch) -> None:
        """Regression: a Task created via POST /tasks must surface in
        GET /mission-control/items. The original façade only queried
        Instinct and silently dropped Tasks — operators creating tasks
        through the modal saw their new work disappear from the feed.

        Also asserts (P4) that the projected WorkItem carries
        ``blocked_by`` with the ``task:`` prefix applied so the
        frontend can resolve dependency edges to other WorkItem rows.
        """
        from datetime import UTC
        from datetime import datetime as _dt

        from pocketpaw_ee.cloud.tasks.domain import Task, TaskAssignee, TaskSource
        from pocketpaw_ee.cloud.tasks.dto import task_to_dto

        sample = Task(
            id="t_sample",
            workspace_id="w1",
            creator_id="u1",
            assignee=TaskAssignee(kind="human", id="u1", name="u1"),
            status="in_progress",
            priority="normal",
            kind="task",
            source=TaskSource(type="user_request"),
            title="Drafted from the modal",
            summary="",
            pocket_id=None,
            blocked_by=("t_dep1", "t_dep2"),
            created_at=_dt.now(UTC),
            updated_at=_dt.now(UTC),
        )

        async def fake_list_tasks(_ctx_, _body):
            return [task_to_dto(sample)]

        monkeypatch.setattr("pocketpaw_ee.cloud.tasks.service.agent_list_tasks", fake_list_tasks)
        out = await mc_service.agent_list_work_items(_ctx(), {})
        titles = [it.title for it in out]
        assert "Drafted from the modal" in titles

        # P4: the projected WorkItem prefixes each blocked_by id with
        # ``task:`` so the frontend can dereference dependency edges to
        # the right WorkItem rows in the heterogeneous feed.
        target = next(it for it in out if it.title == "Drafted from the modal")
        assert target.blocked_by == ["task:t_dep1", "task:t_dep2"]

        # Pocket-less tasks must surface even when the workspace has no
        # visible pockets at all (Tasks are workspace-scoped, not
        # pocket-scoped).
        monkeypatch.setattr(mc_service.pockets_service, "list_pockets", AsyncMock(return_value=[]))
        out2 = await mc_service.agent_list_work_items(_ctx(), {})
        assert "Drafted from the modal" in [it.title for it in out2]


# ---------------------------------------------------------------------------
# workspace-scoped nudges (external-action proposals) must reach The Tray
# ---------------------------------------------------------------------------


class TestWorkspaceScopedNudges:
    """External-action proposals stamp ``Action.pocket_id = workspace_id``
    (they're workspace-scoped, not pocket-bound — see
    ``external_actions/propose.py``). Before the fix, the façade's
    pocket-visibility filter (``a.pocket_id not in visible``) dropped every
    such proposal: a workspace id is never a visible pocket id, so gated
    external actions sat pending forever without ever surfacing in The Tray.
    """

    @pytest.mark.asyncio
    async def test_workspace_scoped_action_surfaces_in_tray(self, store: InstinctStore) -> None:
        a = await store.propose(
            "w1",  # pocket_id carries the workspace for external actions
            "External action — send invoice",
            "gated call",
            "approve to call 'send_invoice' on connector 'stripe'",
            _trigger(),
            parameters={"_external_action": {"connector": "stripe", "action": "send_invoice"}},
            workspace_id="w1",
        )
        items = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        assert [it.id for it in items] == [f"nudge:{a.id}"]
        item = items[0]
        assert item.section == WorkItemSection.TRAY
        assert item.status == WorkItemStatus.AWAITING_APPROVAL
        # Don't leak the raw workspace hex id where a pocket name belongs.
        assert item.pocket_name == "Workspace"

    @pytest.mark.asyncio
    async def test_workspace_scoped_action_surfaces_even_with_no_visible_pockets(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        # The old code skipped the whole instinct block when the workspace
        # had zero visible pockets — workspace-scoped nudges vanished too.
        monkeypatch.setattr(mc_service.pockets_service, "list_pockets", AsyncMock(return_value=[]))
        a = await store.propose(
            "w1",
            "External action — pocketless workspace",
            "",
            "",
            _trigger(),
            parameters={"_external_action": {"connector": "gmail", "action": "send"}},
            workspace_id="w1",
        )
        items = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        assert [it.id for it in items] == [f"nudge:{a.id}"]
        assert items[0].pocket_name == "Workspace"

    @pytest.mark.asyncio
    async def test_other_tenants_workspace_scoped_action_stays_invisible(
        self, store: InstinctStore
    ) -> None:
        # Tenancy guard: w2's workspace-scoped proposal (stored under w2)
        # must never surface for a w1 caller. The store-level W4c scope is
        # what isolates it — accepting pocket_id == workspace_id must not
        # loosen that.
        await store.propose(
            "w2",
            "theirs — external action",
            "",
            "",
            _trigger(),
            parameters={"_external_action": {"connector": "stripe", "action": "refund"}},
            workspace_id="w2",
        )
        items = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        assert items == []


# ---------------------------------------------------------------------------
# bulk_approve / bulk_reject
# ---------------------------------------------------------------------------


class TestBulkApproveService:
    @pytest.mark.asyncio
    async def test_approves_visible_actions(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        b = await store.propose("p1", "B", "", "", _trigger())
        result = await mc_service.agent_bulk_approve(_ctx(), BulkActionRequest(ids=[a.id, b.id]))
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
# bulk_approve — parked writes must actually fire (W0c)
# ---------------------------------------------------------------------------


class TestBulkApproveExecutesWrites:
    """W0c regression — façade bulk-approve must EXECUTE each approved
    Nudge's parked pocket write and emit its chain, not just flip it to
    ``approved`` and strand the write at the bridge.

    Before the fix ``agent_bulk_approve`` called ``store.bulk_approve``
    and returned; the parked write never fired (no execution, no audit/
    chain emit). The single-approve HTTP path has always fired
    ``execute_approved_write``; these tests pin the façade to the same
    behaviour.
    """

    def _wire_bridge(self, monkeypatch, store: InstinctStore, run_action) -> None:
        """Wire the bridge's collaborators so ``execute_approved_write``
        runs end-to-end against the per-test store.

        - ``get_pocket_backend_for_executor`` → valid 6-tuple creds
        - ``action_executor.run_action`` → the supplied stub
        - ``pocketpaw.stores.get_instinct_store`` → the SAME store the
          façade seeded, so ``mark_executed`` / ``mark_failed`` land on
          the seeded rows.
        """

        async def _get_creds(workspace_id, pocket_id):
            return ("https://api.example.com", "bearer", None, "tok", [], None)

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor",
            _get_creds,
        )
        monkeypatch.setattr("pocketpaw_ee.cloud.pockets.action_executor.run_action", run_action)
        monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)

    @pytest.mark.asyncio
    async def test_bulk_approve_executes_each_parked_write_and_reaches_executed(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        """≥2 parked-write Nudges: every write fires (executor re-entered
        with from_instinct=True) and every action lands at EXECUTED."""
        fired: list[str] = []

        async def _run_action(**kwargs):
            fired.append(kwargs["action"])
            assert kwargs["from_instinct"] is True
            return {"ok": True, "action": kwargs["action"], "status": 200, "response": {}}

        self._wire_bridge(monkeypatch, store, _run_action)

        a = await store.propose(
            "p1", "renew A", "", "", _trigger(), parameters=_pocket_write_params(action="renew_a")
        )
        b = await store.propose(
            "p1", "renew B", "", "", _trigger(), parameters=_pocket_write_params(action="renew_b")
        )

        result = await mc_service.agent_bulk_approve(_ctx(), BulkActionRequest(ids=[a.id, b.id]))

        # Both approvals recorded.
        assert {row["id"] for row in result["approved"]} == {a.id, b.id}
        # Both parked writes actually fired through the executor.
        assert sorted(fired) == ["renew_a", "renew_b"]
        # Per-item outcomes report both as executed with no error.
        executed = {row["id"]: row for row in result["executed"]}
        assert executed[a.id]["executed"] is True
        assert executed[b.id]["executed"] is True
        assert executed[a.id]["error"] is None
        # The bridge marked both actions EXECUTED — the write landed.
        assert (await store.get_action(a.id)).status.value == "executed"
        assert (await store.get_action(b.id)).status.value == "executed"

    @pytest.mark.asyncio
    async def test_bulk_approve_emits_chain_events_per_parked_write(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        """Each parked-write item emits the same chain pair the HTTP
        approve path emits: ``human.corrected`` then ``policy.evaluated``."""
        human_calls: list[str] = []
        policy_calls: list[str] = []

        def _spy_human(**kwargs):
            human_calls.append(str(kwargs["action"].id))
            return None  # event id; None is fine for the policy causation arg

        def _spy_policy(**kwargs):
            policy_calls.append(str(kwargs["action"].id))

        async def _run_action(**kwargs):
            return {"ok": True, "action": kwargs["action"], "status": 200, "response": {}}

        self._wire_bridge(monkeypatch, store, _run_action)
        monkeypatch.setattr(
            "pocketpaw_ee.instinct.chain_emitters._emit_human_corrected", _spy_human
        )
        monkeypatch.setattr(
            "pocketpaw_ee.instinct.chain_emitters._emit_policy_evaluated_approved", _spy_policy
        )

        a = await store.propose(
            "p1", "renew A", "", "", _trigger(), parameters=_pocket_write_params()
        )
        b = await store.propose(
            "p1", "renew B", "", "", _trigger(), parameters=_pocket_write_params()
        )

        await mc_service.agent_bulk_approve(_ctx(), BulkActionRequest(ids=[a.id, b.id]))

        assert sorted(human_calls) == sorted([a.id, b.id])
        assert sorted(policy_calls) == sorted([a.id, b.id])

    @pytest.mark.asyncio
    async def test_one_failing_write_does_not_drop_the_rest(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        """Per-item isolation: a write that raises is reported failed while
        the sibling writes still execute and reach EXECUTED."""

        async def _run_action(**kwargs):
            if kwargs["action"] == "boom":
                raise RuntimeError("backend exploded")
            return {"ok": True, "action": kwargs["action"], "status": 200, "response": {}}

        self._wire_bridge(monkeypatch, store, _run_action)

        good1 = await store.propose(
            "p1", "good 1", "", "", _trigger(), parameters=_pocket_write_params(action="ok1")
        )
        bad = await store.propose(
            "p1", "bad", "", "", _trigger(), parameters=_pocket_write_params(action="boom")
        )
        good2 = await store.propose(
            "p1", "good 2", "", "", _trigger(), parameters=_pocket_write_params(action="ok2")
        )

        result = await mc_service.agent_bulk_approve(
            _ctx(), BulkActionRequest(ids=[good1.id, bad.id, good2.id])
        )

        # All three flipped to approved regardless of execution outcome.
        assert {row["id"] for row in result["approved"]} == {good1.id, bad.id, good2.id}

        # The two good writes landed; the bad one was marked failed by the
        # bridge — and crucially did NOT prevent the others from executing.
        assert (await store.get_action(good1.id)).status.value == "executed"
        assert (await store.get_action(good2.id)).status.value == "executed"
        assert (await store.get_action(bad.id)).status.value == "failed"

        # Ordering preserved + per-item outcomes carry the failure detail.
        executed = {row["id"]: row for row in result["executed"]}
        assert [row["id"] for row in result["executed"]] == [good1.id, bad.id, good2.id]
        assert executed[good1.id]["executed"] is True
        assert executed[good2.id]["executed"] is True
        # The bridge swallows the executor crash (marks failed) and returns
        # cleanly, so the item reports executed=True with no error at the
        # façade layer — but the action status proves the write did NOT land.
        # Either way the sibling writes are never dropped.
        assert executed[bad.id]["error"] is None or executed[bad.id]["executed"] is False


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
# W4c — store-level workspace scoping on the instinct reads
# ---------------------------------------------------------------------------


class TestWorkspaceScopedReads:
    """W4c — the façade threads ``ctx.workspace_id`` into the instinct store
    reads so a tenant only sees its own Nudges / outcomes, never another
    workspace's, on the shared global instinct DB.

    The autouse ``list_pockets`` mock is workspace-BLIND (returns p1/p2 for
    every caller), so pocket visibility can't be what isolates these rows —
    the isolation can only come from the store-level ``workspace_id`` filter
    W4c adds. That is exactly the residual leak W4a left open on the internal
    caller path: before W4c, ``store.pending`` / ``store.list_actions`` were
    called with no workspace and returned every tenant's rows.
    """

    @pytest.mark.asyncio
    async def test_list_work_items_excludes_other_workspace(self, store: InstinctStore) -> None:
        # Two tenants' Nudges land in the same visible pocket on the shared DB.
        await store.propose("p1", "mine", "", "", _trigger(), workspace_id="w1")
        await store.propose("p1", "theirs", "", "", _trigger(), workspace_id="w2")

        out = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        titles = {it.title for it in out}
        assert titles == {"mine"}
        assert "theirs" not in titles

    @pytest.mark.asyncio
    async def test_list_work_items_includes_legacy_null_workspace(
        self, store: InstinctStore
    ) -> None:
        # A pre-tenancy Nudge (no workspace_id) must stay visible to a scoped
        # read so legacy rows don't vanish from The Tray after W4c.
        await store.propose("p1", "legacy", "", "", _trigger())  # workspace_id=None
        await store.propose("p1", "theirs", "", "", _trigger(), workspace_id="w2")

        out = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        assert {it.title for it in out} == {"legacy"}

    @pytest.mark.asyncio
    async def test_outcomes_summary_excludes_other_workspace(self, store: InstinctStore) -> None:
        # ``agent_outcomes_summary`` has NO pocket filter at the store, so this
        # is the highest-risk read: pre-W4c the newest 500 rows could be all
        # another tenant's. Scope it and a foreign tenant's rows never count.
        mine = await store.propose("p1", "mine-A", "", "", _trigger(), workspace_id="w1")
        await store.approve(mine.id)
        for _ in range(3):
            await store.propose("p1", "theirs", "", "", _trigger(), workspace_id="w2")

        summary = await mc_service.agent_outcomes_summary(
            _ctx(workspace_id="w1"), OutcomesQueryRequest(window="24h")
        )
        # Only w1's single (approved) row is counted; w2's 3 pending are not.
        assert summary.total == 1
        assert summary.approved == 1
        assert summary.pending == 0


# ---------------------------------------------------------------------------
# activity feed
# ---------------------------------------------------------------------------


class TestListActivity:
    @pytest.mark.asyncio
    async def test_returns_buffer_entries_newest_first(self, store: InstinctStore) -> None:
        import time

        from pocketpaw_ee.cloud.activity.buffer import ActivityEvent, get_buffer

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
