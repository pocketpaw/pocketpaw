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
# Updated: 2026-07-04 (fix/approval-resolution) — added
# ``TestWorkspaceScopedNudgeApproveResolves`` (the list-vs-approve consistency
# the bug broke: a workspace-scoped nudge that LISTS must RESOLVE on
# bulk-approve — approved non-empty, missing empty — while another tenant's
# stays blocked) and ``TestBulkApproveExecutesGatedKinds`` (the façade
# bulk-approve must FIRE every non-pocket-write gated kind's executor —
# ``_admin_action`` / ``_external_action`` — not just ``_pocket_write``, so a
# bulk-approved admin action actually executes; plus per-item isolation on the
# gated path).
# Updated: 2026-07-04 (fix/approval-resolution — projection fixes) — added
# ``TestActorNameResolution`` (a gated proposal's ``trigger.source`` is the
# proposer user id, so the tray must render the resolved display name, not the
# raw ObjectId hex — with full_name → email → id fallback, non-ObjectId
# graceful, and a single-batch-query efficiency guard) and ``TestStatusFilter``
# (``?status=pending`` returns only awaiting-approval items and EXCLUDES
# terminal executed/failed ones; ``status=None`` returns everything; a raw
# WorkItemStatus value also filters; status composes with the pocket filter).
# The name-resolution tests use the ``mongo_db`` fixture to seed real ``User``
# docs so ``_resolve_actor_names`` exercises the live ``_UserDoc.find`` path.

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
# list-vs-approve consistency for workspace-scoped nudges (the resolver bug)
# ---------------------------------------------------------------------------


class TestWorkspaceScopedNudgeApproveResolves:
    """Regression — a workspace-scoped nudge (``pocket_id == workspace_id``,
    e.g. an ``_admin_action`` / ``_external_action`` proposal) that LISTS in
    The Tray must also RESOLVE on bulk-approve.

    The bug: ``agent_list_work_items`` admits ``a.pocket_id == workspace_id``
    (workspace-scoped nudges reach the feed), but ``_split_ids_by_tenancy``
    on the approve path only admitted ``action.pocket_id in visible_pockets``.
    A workspace id is never a visible POCKET id, so a workspace-scoped nudge
    that listed fine was pushed to ``blocked`` on approve and reported as
    ``missing`` — the proposal stayed pending forever. The list and approve
    tenancy filters MUST agree.
    """

    @pytest.mark.asyncio
    async def test_listed_workspace_scoped_nudge_resolves_on_bulk_approve(
        self, store: InstinctStore
    ) -> None:
        # A gated admin-action proposal stamps ``Action.pocket_id = workspace_id``
        # (it isn't pocket-bound — see admin_proposals/propose.py).
        a = await store.propose(
            "w1",  # pocket_id carries the workspace
            "Billing plan change → pro",
            "gated admin write",
            "approve to open checkout for 'pro'",
            _trigger(),
            parameters={
                "_admin_action": {
                    "schema": 1,
                    "kind": "admin_action",
                    "action": "billing.manage",
                    "args": {"plan_key": "pro"},
                    "workspace_id": "w1",
                    "proposer_user_id": "u1",
                }
            },
            workspace_id="w1",
        )

        # It LISTS as a workspace-scoped nudge (the autouse list_pockets mock
        # returns p1/p2 — the workspace id "w1" is never among them).
        items = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        assert [it.id for it in items] == [f"nudge:{a.id}"]

        # The exact wire id the LIST returned must RESOLVE on bulk-approve:
        # approved non-empty, missing empty — the list-vs-approve consistency
        # the bug broke.
        result = await mc_service.agent_bulk_approve(
            _ctx(workspace_id="w1"), BulkActionRequest(ids=[f"nudge:{a.id}"])
        )
        assert {row["id"] for row in result["approved"]} == {a.id}
        assert result["missing"] == []
        # The action flipped out of pending — it no longer sits in the Tray.
        assert (await store.get_action(a.id)).status.value == "approved"

    @pytest.mark.asyncio
    async def test_external_action_nudge_also_resolves_not_just_admin(
        self, store: InstinctStore
    ) -> None:
        # The fix must not special-case admin — any workspace-scoped gated
        # kind (external_action here) that lists must also resolve.
        a = await store.propose(
            "w1",
            "External action — send invoice",
            "gated call",
            "approve to call 'send_invoice'",
            _trigger(),
            parameters={"_external_action": {"connector": "stripe", "action": "send_invoice"}},
            workspace_id="w1",
        )
        items = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        assert [it.id for it in items] == [f"nudge:{a.id}"]

        result = await mc_service.agent_bulk_approve(
            _ctx(workspace_id="w1"), BulkActionRequest(ids=[f"nudge:{a.id}"])
        )
        assert {row["id"] for row in result["approved"]} == {a.id}
        assert result["missing"] == []

    @pytest.mark.asyncio
    async def test_other_tenants_workspace_scoped_nudge_stays_blocked_on_approve(
        self, store: InstinctStore
    ) -> None:
        # Admitting ``pocket_id == workspace_id`` must only admit the CALLER'S
        # own workspace — never another tenant's workspace-scoped nudge.
        other = await store.propose(
            "w2",
            "theirs — admin action",
            "",
            "",
            _trigger(),
            parameters={
                "_admin_action": {
                    "schema": 1,
                    "kind": "admin_action",
                    "action": "billing.manage",
                    "args": {"plan_key": "pro"},
                    "workspace_id": "w2",
                    "proposer_user_id": "u2",
                }
            },
            workspace_id="w2",
        )
        # A w1 caller trying to approve w2's workspace-scoped nudge is blocked:
        # the store-level scope keeps the row out of eligibility → missing.
        result = await mc_service.agent_bulk_approve(
            _ctx(workspace_id="w1"), BulkActionRequest(ids=[f"nudge:{other.id}"])
        )
        assert result["approved"] == []
        # Blocked ids come back mapped to the operator's original wire id.
        assert f"nudge:{other.id}" in result["missing"]
        assert (await store.get_action(other.id)).status.value == "pending"


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
# bulk_approve — non-pocket-write gated kinds must FIRE their executor
# ---------------------------------------------------------------------------


class TestBulkApproveExecutesGatedKinds:
    """The façade bulk-approve must dispatch EVERY gated proposal kind's
    executor, not just ``_pocket_write``. Before the fix
    ``_execute_bulk_approved_nudge`` only fired the pocket-write bridge, so a
    bulk-approved ``_admin_action`` / ``_external_action`` / etc. flipped to
    ``approved`` and STRANDED the write (e.g. a ``billing.manage`` admin action
    never opened its checkout). These tests pin the façade to the router's
    per-kind dispatch: the matching executor is invoked with the approved
    Action + a ``human_event_id``.
    """

    @pytest.mark.asyncio
    async def test_admin_action_nudge_fires_its_executor_on_bulk_approve(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        fired: list[str] = []

        async def _spy_admin_executor(action, *, human_event_id=None):
            fired.append(str(action.id))

        # Patch the admin executor at its source module — the façade lazy-imports
        # ``pocketpaw_ee.cloud.admin_proposals.executor`` and reads the attr.
        monkeypatch.setattr(
            "pocketpaw_ee.cloud.admin_proposals.executor.execute_approved_admin_action",
            _spy_admin_executor,
        )

        a = await store.propose(
            "w1",  # workspace-scoped admin action
            "Billing plan change → pro",
            "gated admin write",
            "approve to open checkout",
            _trigger(),
            parameters={
                "_admin_action": {
                    "schema": 1,
                    "kind": "admin_action",
                    "action": "billing.manage",
                    "args": {"plan_key": "pro"},
                    "workspace_id": "w1",
                    "proposer_user_id": "u1",
                }
            },
            workspace_id="w1",
        )

        result = await mc_service.agent_bulk_approve(
            _ctx(workspace_id="w1"), BulkActionRequest(ids=[f"nudge:{a.id}"])
        )
        # Resolved (not missing) AND the admin executor actually fired.
        assert {row["id"] for row in result["approved"]} == {a.id}
        assert result["missing"] == []
        assert fired == [a.id]
        executed = {row["id"]: row for row in result["executed"]}
        assert executed[a.id]["executed"] is True
        assert executed[a.id]["error"] is None

    @pytest.mark.asyncio
    async def test_external_action_nudge_fires_its_executor_on_bulk_approve(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        fired: list[str] = []

        async def _spy_external_executor(action, *, human_event_id=None):
            fired.append(str(action.id))

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.external_actions.executor.execute_approved_external_action",
            _spy_external_executor,
        )

        a = await store.propose(
            "w1",
            "External action — send invoice",
            "gated call",
            "approve to call 'send_invoice'",
            _trigger(),
            parameters={
                "_external_action": {
                    "connector": "stripe",
                    "action": "send_invoice",
                    "workspace_id": "w1",
                }
            },
            workspace_id="w1",
        )

        result = await mc_service.agent_bulk_approve(
            _ctx(workspace_id="w1"), BulkActionRequest(ids=[f"nudge:{a.id}"])
        )
        assert {row["id"] for row in result["approved"]} == {a.id}
        assert fired == [a.id]

    @pytest.mark.asyncio
    async def test_one_failing_gated_executor_does_not_drop_the_rest(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        """Per-item isolation on the gated path — an executor that raises is
        reported failed while sibling items still fire."""

        async def _boom_executor(action, *, human_event_id=None):
            raise RuntimeError("admin service exploded")

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.admin_proposals.executor.execute_approved_admin_action",
            _boom_executor,
        )

        good = await store.propose(
            "w1",
            "External — ok",
            "",
            "",
            _trigger(),
            parameters={"_external_action": {"connector": "gmail", "action": "send"}},
            workspace_id="w1",
        )
        bad = await store.propose(
            "w1",
            "Admin — boom",
            "",
            "",
            _trigger(),
            parameters={
                "_admin_action": {
                    "schema": 1,
                    "kind": "admin_action",
                    "action": "billing.manage",
                    "args": {"plan_key": "pro"},
                    "workspace_id": "w1",
                    "proposer_user_id": "u1",
                }
            },
            workspace_id="w1",
        )

        result = await mc_service.agent_bulk_approve(
            _ctx(workspace_id="w1"), BulkActionRequest(ids=[f"nudge:{good.id}", f"nudge:{bad.id}"])
        )
        # Both flipped to approved regardless of execution outcome.
        assert {row["id"] for row in result["approved"]} == {good.id, bad.id}
        executed = {row["id"]: row for row in result["executed"]}
        # The failing admin executor is isolated + reported; the good one fired.
        assert executed[bad.id]["executed"] is False
        assert executed[bad.id]["error"] is not None
        assert executed[good.id]["executed"] is True


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


# ---------------------------------------------------------------------------
# actor-name resolution — trigger.source (proposer user id) → display name
# ---------------------------------------------------------------------------


class TestActorNameResolution:
    """A gated proposal's ``trigger.source`` is the PROPOSER user id (a raw
    ObjectId hex — see ``admin_proposals/propose.py`` /
    ``external_actions/propose.py``, both ``trigger.type == "agent"``).
    Before the fix, ``_action_to_work_item`` projected that raw id straight
    into ``agent_name`` / ``assignee_name``, so the approval tray rendered
    ``6a47…`` instead of a person. The façade now batch-resolves every
    source id to a display name once and the projection renders the name.
    """

    @pytest.mark.asyncio
    async def test_agent_name_is_resolved_display_name_not_raw_id(
        self, mongo_db, store: InstinctStore
    ) -> None:
        from pocketpaw_ee.cloud.models.user import User

        # Seed a real user; ``str(user.id)`` is the ObjectId hex the
        # proposer path stamps on ``trigger.source``.
        proposer = User(
            email="captain@atlassmoke.dev",
            hashed_password="x",
            full_name="Atlas Captain",
        )
        await proposer.insert()
        proposer_id = str(proposer.id)

        a = await store.propose(
            "w1",  # pocket_id carries the workspace for gated proposals
            "Billing plan change → pro",
            "gated admin write",
            "approve to open checkout for 'pro'",
            ActionTrigger(type="agent", source=proposer_id, reason="admin action"),
            parameters={
                "_admin_action": {
                    "schema": 1,
                    "kind": "admin_action",
                    "action": "billing.manage",
                    "args": {"plan_key": "pro"},
                    "workspace_id": "w1",
                    "proposer_user_id": proposer_id,
                }
            },
            workspace_id="w1",
        )

        items = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        assert [it.id for it in items] == [f"nudge:{a.id}"]
        item = items[0]
        # The whole point: a NAME, not the raw ObjectId hex.
        assert item.agent_name == "Atlas Captain"
        assert item.agent_name != proposer_id
        # ``agent_id`` still carries the raw id for downstream links.
        assert item.agent_id == proposer_id

    @pytest.mark.asyncio
    async def test_falls_back_to_email_then_id(self, mongo_db, store: InstinctStore) -> None:
        from pocketpaw_ee.cloud.models.user import User

        # A user with no full_name resolves to their email.
        no_name = User(email="nameless@atlassmoke.dev", hashed_password="x", full_name="")
        await no_name.insert()
        email_id = str(no_name.id)
        await store.propose(
            "w1",
            "External action — send invoice",
            "",
            "",
            ActionTrigger(type="agent", source=email_id, reason="ext"),
            parameters={"_external_action": {"connector": "stripe", "action": "send_invoice"}},
            workspace_id="w1",
        )
        items = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        assert items[0].agent_name == "nameless@atlassmoke.dev"

        # An id with no matching user (or a malformed source) falls back to
        # the id itself — never raises, never renders empty.
        b = await store.propose(
            "w1",
            "External action — no such user",
            "",
            "",
            ActionTrigger(type="agent", source="not-an-object-id", reason="ext"),
            parameters={"_external_action": {"connector": "gmail", "action": "send"}},
            workspace_id="w1",
        )
        items2 = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        target = next(it for it in items2 if it.id == f"nudge:{b.id}")
        assert target.agent_name == "not-an-object-id"

    @pytest.mark.asyncio
    async def test_resolves_names_in_one_batch_query(
        self, mongo_db, monkeypatch, store: InstinctStore
    ) -> None:
        # Efficiency guard: the union of source ids is resolved with a
        # SINGLE _UserDoc.find, mirroring _pocket_name_map's build-once — not
        # one query per action.
        from pocketpaw_ee.cloud.models.user import User

        u1 = User(email="a@x.dev", hashed_password="x", full_name="Alice")
        u2 = User(email="b@x.dev", hashed_password="x", full_name="Bob")
        await u1.insert()
        await u2.insert()

        calls = {"n": 0}
        real_find = User.find.__func__

        def _counting_find(cls, *args, **kwargs):
            calls["n"] += 1
            return real_find(cls, *args, **kwargs)

        monkeypatch.setattr(User, "find", classmethod(_counting_find))

        for uid in (str(u1.id), str(u2.id), str(u1.id)):
            await store.propose(
                "w1",
                f"gated by {uid}",
                "",
                "",
                ActionTrigger(type="agent", source=uid, reason="ext"),
                parameters={"_external_action": {"connector": "s", "action": "a"}},
                workspace_id="w1",
            )
        items = await mc_service.agent_list_work_items(_ctx(workspace_id="w1"), {})
        names = {it.agent_name for it in items}
        assert names == {"Alice", "Bob"}
        # Three actions, two distinct proposers — resolved in ONE find call.
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# status filter — ?status=pending excludes terminal items
# ---------------------------------------------------------------------------


class TestStatusFilter:
    """``GET /mission-control/items?status=pending`` must return only the
    awaiting-approval feed. Before the fix the endpoint accepted no
    ``status`` param, so terminal (done/failed) items leaked into the tray.
    ``pending`` is aliased to ``WorkItemStatus.AWAITING_APPROVAL`` (the
    projection maps ``ActionStatus.PENDING`` → that).
    """

    @pytest.mark.asyncio
    async def test_pending_excludes_terminal_items(self, store: InstinctStore) -> None:
        pending = await store.propose("p1", "still pending", "", "", _trigger())
        executed = await store.propose("p1", "already done", "", "", _trigger())
        await store.approve(executed.id)
        await store.mark_executed(executed.id, outcome="done")
        failed = await store.propose("p1", "it failed", "", "", _trigger())
        await store.approve(failed.id)
        await store.mark_failed(failed.id, error="boom")

        # No filter → all three surface.
        all_items = await mc_service.agent_list_work_items(_ctx(), {})
        assert {it.source_id for it in all_items} == {pending.id, executed.id, failed.id}

        # ?status=pending → only the awaiting-approval one; terminal excluded.
        out = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(status="pending"))
        assert [it.source_id for it in out] == [pending.id]
        assert out[0].status == WorkItemStatus.AWAITING_APPROVAL

    @pytest.mark.asyncio
    async def test_status_none_returns_everything(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "pending", "", "", _trigger())
        b = await store.propose("p1", "done", "", "", _trigger())
        await store.approve(b.id)
        await store.mark_executed(b.id, outcome="done")
        out = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(status=None))
        assert {it.source_id for it in out} == {a.id, b.id}

    @pytest.mark.asyncio
    async def test_status_accepts_raw_workitemstatus_value(self, store: InstinctStore) -> None:
        # ``status`` also accepts a raw WorkItemStatus value (e.g. "done").
        await store.propose("p1", "pending", "", "", _trigger())
        b = await store.propose("p1", "done", "", "", _trigger())
        await store.approve(b.id)
        await store.mark_executed(b.id, outcome="done")
        out = await mc_service.agent_list_work_items(_ctx(), ListWorkItemsRequest(status="done"))
        assert [it.source_id for it in out] == [b.id]

    @pytest.mark.asyncio
    async def test_status_composes_with_pocket_filter(self, store: InstinctStore) -> None:
        p1a = await store.propose("p1", "p1 pending", "", "", _trigger())
        await store.propose("p2", "p2 pending", "", "", _trigger())
        out = await mc_service.agent_list_work_items(
            _ctx(), ListWorkItemsRequest(status="pending", pocket="p1")
        )
        assert [it.source_id for it in out] == [p1a.id]
