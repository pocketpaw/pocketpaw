# tests/cloud/test_belt_queued_run_approve.py — a QUEUED station run is not approvable.
#
# A queued Belt run (a ``code_change`` Action with ``station_pending`` set, or
# no diff yet) waits for a station to produce its diff. Approving it used to
# flip it to approved and the belt executor then FAILED it ("this is a QUEUED
# station run"), so a Mission Control bulk-approve over the Tray killed live
# factory runs. Every human approve path now leaves it pending and untouched:
# the Mission Control bulk-approve and the instinct router's bulk-approve
# report it under ``missing``, the single approve refuses it with a 409. A
# normal diff-carrying run in the same bulk still approves.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.cloud.mission_control import service as mc_service  # noqa: E402
from pocketpaw_ee.cloud.mission_control.dto import BulkActionRequest  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402

from pocketpaw.instinct.models import ActionStatus, ActionTrigger  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "ws-test"
DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"


async def _run(store: InstinctStore, *, diff: str = "", pending: bool = True):
    blob = {
        "kind": "code_change",
        "schema": 2,
        "repo": "/srv/acme",
        "base_branch": "main",
        "diff": diff,
        "summary": "G41: Text shapes",
        "task": "G41: Text shapes",
        "workspace_id": WS,
        "requested_by": "u1",
        **({"station_pending": True} if pending else {}),
    }
    trigger = ActionTrigger(type="agent", source="belt:mandate", reason="queued")
    return await store.propose(
        WS, "Code change", "", "", trigger, parameters={"_code_change": blob}, workspace_id=WS
    )


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "queued.db")


@pytest.fixture
def executed(monkeypatch) -> list[str]:
    """The belt executor, spied: a queued run must never reach it."""
    seen: list[str] = []

    async def spy(action, *, human_event_id=None):
        seen.append(str(action.id))

    monkeypatch.setattr("pocketpaw_ee.cloud.belt.executor.execute_approved_change", spy)
    return seen


async def test_mission_control_bulk_approve_skips_queued_runs(store, executed, monkeypatch):
    monkeypatch.setattr(mc_service, "get_instinct_store", lambda *a, **k: store)
    monkeypatch.setattr(
        mc_service.pockets_service, "visible_pocket_refs", AsyncMock(return_value=[])
    )
    queued = await _run(store)
    no_diff = await _run(store, pending=False)
    ready = await _run(store, diff=DIFF, pending=False)
    ctx = RequestContext(
        user_id="u1",
        workspace_id=WS,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )

    result = await mc_service.agent_bulk_approve(
        ctx, BulkActionRequest(ids=[f"nudge:{queued.id}", no_diff.id, ready.id])
    )

    assert {row["id"] for row in result["approved"]} == {ready.id}
    assert f"nudge:{queued.id}" in result["missing"] and no_diff.id in result["missing"]
    for action in (queued, no_diff):
        assert (await store.get_action(action.id)).status == ActionStatus.PENDING
    assert executed == [ready.id]


async def test_mission_control_items_say_which_belt_runs_are_ready(store, monkeypatch):
    monkeypatch.setattr(mc_service, "get_instinct_store", lambda *a, **k: store)
    monkeypatch.setattr(
        mc_service.pockets_service, "visible_pocket_refs", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.tasks.service.agent_list_tasks", AsyncMock(return_value=[])
    )
    queued = await _run(store)
    ready = await _run(store, diff=DIFF, pending=False)
    plain = await store.propose(
        WS,
        "Not a belt run",
        "",
        "",
        ActionTrigger(type="agent", source="x", reason="r"),
        parameters={"_external_action": {"connector": "stripe", "workspace_id": WS}},
        workspace_id=WS,
    )
    ctx = RequestContext(
        user_id="u1",
        workspace_id=WS,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )

    items = await mc_service.agent_list_work_items(ctx, {})

    ready_by_id = {it.source_id: it.belt_change_ready for it in items}
    assert ready_by_id == {queued.id: False, ready.id: True, plain.id: None}


@pytest.fixture
def client(store: InstinctStore, monkeypatch):
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))
    user = type(
        "U",
        (),
        {
            "id": "u1",
            "active_workspace": WS,
            "workspaces": [type("M", (), {"workspace": WS, "role": "admin"})()],
        },
    )()
    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: WS
    with patch("pocketpaw_ee.instinct.router._store", return_value=store):
        yield TestClient(app)


async def test_single_approve_refuses_a_queued_run(store, executed, client):
    queued = await _run(store)

    res = client.post(f"/instinct/actions/{queued.id}/approve")

    assert res.status_code == 409, res.text
    assert "queued" in res.text.lower()
    assert (await store.get_action(queued.id)).status == ActionStatus.PENDING
    assert executed == []


async def test_router_bulk_approve_skips_queued_runs(store, executed, client):
    queued = await _run(store)
    ready = await _run(store, diff=DIFF, pending=False)

    res = client.post("/instinct/actions/bulk-approve", json={"ids": [queued.id, ready.id]})

    assert res.status_code == 200, res.text
    body = res.json()
    assert [a["id"] for a in body["affected"]] == [ready.id]
    assert queued.id in body["missing"]
    assert (await store.get_action(queued.id)).status == ActionStatus.PENDING
    assert executed == [ready.id]
