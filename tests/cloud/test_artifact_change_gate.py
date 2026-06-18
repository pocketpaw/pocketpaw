# tests/cloud/test_artifact_change_gate.py
# Created: 2026-06-18 (feat/branch-primitive-instinct-gate, BP-3) — security +
# behaviour coverage for the Branch-primitive artifact-change MERGE GATE on the
# Instinct router (Part B + Part C).
#
# What this pins (router level, sync TestClient over HTTP — mirrors
# test_instinct_approval_security.py):
#   SECURITY (Part C) — a cross-workspace caller gets 403 BEFORE any state
#     mutation, on BOTH approve and reject (single + bulk), with the same
#     ``instinct.cross_workspace_approval`` error code. Asymmetric tenant scope
#     is no tenant scope (pocketpaw#1183 / #1250).
#   MERGE (Part B) — an approved ``_artifact_change`` Action dispatches the merge
#     executor (publish + deploy); a same-tenant approve reaches it.
#   DISCARD (Part B) — a rejected ``_artifact_change`` Action dispatches the
#     discard executor; the published pointer is untouched.
#
# The executor itself is patched off here (its real publish/mark_merged/discard
# behaviour against beanie is covered in tests/ee/versions/test_merge_gate_service.py
# + the executor behaviour test below). These tests pin the GATE: who is allowed
# through, and that the right executor branch fires.
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

TRIGGER = {"type": "agent", "source": "claude", "reason": "artifact-change gate test"}


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "user-A", workspace_id: str = "ws-A") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _artifact_change_params(workspace_id: str) -> dict:
    """An Action ``parameters`` payload carrying an ``_artifact_change`` blob —
    the merge-gate candidate shape. Tenancy lives on the blob's ``workspace``.
    ``correlation_id`` is set so the (best-effort) chain emits have an id to
    chain off; the gate tests don't assert chain semantics."""
    return {
        "_artifact_change": {
            "schema": 1,
            "scope_type": "pocket",
            "scope_id": "pocket-art",
            "branch": "cand",
            "from_version_id": "ver-from",
            "to_version_id": "ver-to",
            "workspace": workspace_id,
            "user_id": "requester-9",
            "correlation_id": None,
            "proposed_event_id": None,
        }
    }


@pytest.fixture
def router_store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "artifact_gate.db")


def _make_client(router_store: InstinctStore, user: _FakeUser, monkeypatch) -> TestClient:
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return TestClient(app)


def _propose(client: TestClient, *, pocket_id: str, title: str, parameters: dict | None = None):
    payload: dict = {"pocket_id": pocket_id, "title": title, "trigger": TRIGGER}
    if parameters is not None:
        payload["parameters"] = parameters
    resp = client.post("/instinct/actions", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _status_of(client: TestClient, action_id: str) -> str:
    resp = client.get("/instinct/actions", params={"limit": 500})
    assert resp.status_code == 200, resp.text
    for action in resp.json()["actions"]:
        if action["id"] == action_id:
            return action["status"]
    raise AssertionError(f"action {action_id} not found")


# ---------------------------------------------------------------------------
# Part C — cross-workspace 403 on BOTH approve and reject (single + bulk)
# ---------------------------------------------------------------------------


class TestArtifactChangeCrossWorkspace:
    def test_single_approve_of_foreign_workspace_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-art",
                title="ws-B merge",
                parameters=_artifact_change_params(workspace_id="ws-B"),
            )
            resp = client.post(f"/instinct/actions/{action_id}/approve")
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            # No mutation — still pending.
            assert _status_of(client, action_id) == "pending"

    def test_single_reject_of_foreign_workspace_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """The reject side MUST 403 too — a cross-workspace reject would discard
        another tenant's candidate. Asymmetric scope is no scope."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-art",
                title="ws-B merge",
                parameters=_artifact_change_params(workspace_id="ws-B"),
            )
            resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "nope"})
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, action_id) == "pending"

    def test_bulk_approve_of_foreign_workspace_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            own = _propose(
                client,
                pocket_id="pocket-art",
                title="ws-A merge",
                parameters=_artifact_change_params(workspace_id="ws-A"),
            )
            foreign = _propose(
                client,
                pocket_id="pocket-art",
                title="ws-B merge",
                parameters=_artifact_change_params(workspace_id="ws-B"),
            )
            resp = client.post("/instinct/actions/bulk-approve", json={"ids": [own, foreign]})
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            # Whole batch rejected — nothing flips.
            assert _status_of(client, own) == "pending"
            assert _status_of(client, foreign) == "pending"

    def test_bulk_reject_of_foreign_workspace_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            own = _propose(
                client,
                pocket_id="pocket-art",
                title="ws-A merge",
                parameters=_artifact_change_params(workspace_id="ws-A"),
            )
            foreign = _propose(
                client,
                pocket_id="pocket-art",
                title="ws-B merge",
                parameters=_artifact_change_params(workspace_id="ws-B"),
            )
            resp = client.post(
                "/instinct/actions/bulk-reject",
                json={"ids": [own, foreign], "reason": "batch nope"},
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, own) == "pending"
            assert _status_of(client, foreign) == "pending"


# ---------------------------------------------------------------------------
# Part B — same-tenant approve MERGES, same-tenant reject DISCARDS
# ---------------------------------------------------------------------------


class TestArtifactChangeDispatch:
    def test_same_workspace_approve_dispatches_merge(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A same-tenant approve passes the gate and reaches the MERGE executor
        (patched off — the real publish/deploy is covered against beanie)."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        calls: dict = {}

        async def _fake_merge(action, *, human_event_id=None):
            calls["merge"] = action.id

        monkeypatch.setattr(
            "pocketpaw_ee.versions.instinct_executor.execute_approved_change", _fake_merge
        )
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-art",
                title="ws-A merge",
                parameters=_artifact_change_params(workspace_id="ws-A"),
            )
            resp = client.post(f"/instinct/actions/{action_id}/approve")
            assert resp.status_code == 200, resp.text
            assert resp.json()["action"]["status"] == "approved"
            # The MERGE executor fired for this action.
            assert calls.get("merge") == action_id

    def test_same_workspace_reject_dispatches_discard(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A same-tenant reject passes the gate and reaches the DISCARD
        executor; the action transitions to rejected."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        calls: dict = {}

        async def _fake_discard(action):
            calls["discard"] = action.id

        monkeypatch.setattr(
            "pocketpaw_ee.versions.instinct_executor.discard_rejected_change", _fake_discard
        )
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-art",
                title="ws-A merge",
                parameters=_artifact_change_params(workspace_id="ws-A"),
            )
            resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "not yet"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "rejected"
            # The DISCARD executor fired.
            assert calls.get("discard") == action_id

    def test_merge_executor_failure_does_not_break_approve(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A crash inside the merge executor is swallowed — the approve still
        returns 200 (best-effort, like every other blob hook)."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)

        async def _boom(action, *, human_event_id=None):
            raise RuntimeError("merge blew up")

        monkeypatch.setattr(
            "pocketpaw_ee.versions.instinct_executor.execute_approved_change", _boom
        )
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-art",
                title="ws-A merge",
                parameters=_artifact_change_params(workspace_id="ws-A"),
            )
            resp = client.post(f"/instinct/actions/{action_id}/approve")
            assert resp.status_code == 200, resp.text
            assert resp.json()["action"]["status"] == "approved"
