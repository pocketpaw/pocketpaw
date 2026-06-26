# tests/cloud/test_instinct_empty_workspace_failclosed.py
# Created: 2026-06-26 (fix/cloud-iso-executor-scope, LOW-1) — fail-closed gate
# for an EMPTY blob-workspace claim across ALL six gated Instinct proposal kinds.
#
# Before LOW-1 only ``_artifact_change`` hard-403'd on an empty blob workspace
# (its own regression lives in test_artifact_change_gate.py). The other six
# kinds — _belt_plan, _code_change, _external_action, _fabric_objects,
# _pocket_create, _pocket_write — used the ``if blob_workspace and
# blob_workspace != current`` short-circuit, i.e. an EMPTY claim PASSED THROUGH
# the tenancy check. Now that the approve-time executors resolve their store
# FROM the blob's workspace_id (C1), an empty claim is a real hole: it would let
# any operator approve a no-workspace blob whose action then misfiles onto the
# shared ledger. This pins that every kind fails closed (403
# ``instinct.missing_workspace_in_blob``) on BOTH approve and reject, with NO
# state mutation — so reverting any one ``if not blob_workspace: raise`` guard
# back to the short-circuit fails here.
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

TRIGGER = {"type": "agent", "source": "claude", "reason": "low-1 fail-closed test"}


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "user-A", workspace_id: str = "ws-A") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


# Each gated kind -> an Action ``parameters`` payload whose blob carries NO
# workspace claim (empty string; the alias key absent). The blob bodies are
# otherwise minimal — the gate runs BEFORE the executor, so only the workspace
# field matters for this assertion.
def _empty_blob_params(kind: str) -> dict:
    bodies: dict[str, dict] = {
        "_belt_plan": {
            "schema": 1,
            "workspace_id": "",
            "mandate_id": "m-1",
            "shift_no": 0,
            "plan": {"tasks": []},
            "correlation_id": None,
        },
        "_code_change": {
            "schema": 2,
            "kind": "code_change",
            "workspace_id": "",
            "requested_by": "attacker-1",
            "base_branch": "main",
            "diff": "",
            "correlation_id": None,
        },
        "_external_action": {
            "schema": 1,
            "workspace_id": "",
            "connector_name": "crm",
            "action": "noop",
            "params": {},
            "requested_by": "attacker-1",
            "correlation_id": None,
        },
        "_fabric_objects": {
            "schema": 1,
            "workspace_id": "",
            "object_types": [],
            "objects": [],
            "links": [],
            "requested_by": "attacker-1",
            "correlation_id": None,
        },
        "_pocket_create": {
            "schema": 1,
            "workspace_id": "",
            "user_id": "attacker-1",
            "pocket_spec": {"name": "x"},
            "correlation_id": None,
        },
        "_pocket_write": {
            "schema": 2,
            "action": "mark_renewed",
            "method": "POST",
            "path": "/victim/renew",
            "params": {},
            "workspace_id": "",
            "requested_by": "attacker-1",
            "correlation_id": None,
        },
    }
    return {kind: bodies[kind]}


@pytest.fixture
def router_store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "low1_gate.db")


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


def _propose(client: TestClient, *, pocket_id: str, parameters: dict) -> str:
    payload = {
        "pocket_id": pocket_id,
        "title": "empty-workspace blob",
        "trigger": TRIGGER,
        "parameters": parameters,
    }
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


GATED_KINDS = [
    "_belt_plan",
    "_code_change",
    "_external_action",
    "_fabric_objects",
    "_pocket_create",
    "_pocket_write",
]


@pytest.mark.parametrize("kind", GATED_KINDS)
def test_approve_of_empty_workspace_blob_is_403(kind: str, router_store, monkeypatch) -> None:
    """APPROVE of an empty-workspace blob → 403 missing_workspace_in_blob, no
    state mutation. Reverting the kind's ``if not blob_workspace`` guard to the
    ``if blob_workspace and ...`` short-circuit makes this approve succeed."""
    client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        action_id = _propose(client, pocket_id="victim", parameters=_empty_blob_params(kind))
        resp = client.post(f"/instinct/actions/{action_id}/approve")
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "instinct.missing_workspace_in_blob"
        assert _status_of(client, action_id) == "pending"


@pytest.mark.parametrize("kind", GATED_KINDS)
def test_reject_of_empty_workspace_blob_is_403(kind: str, router_store, monkeypatch) -> None:
    """REJECT of an empty-workspace blob → 403 too (asymmetric scope is no
    scope): a cross/empty-workspace reject would discard another tenant's row."""
    client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        action_id = _propose(client, pocket_id="victim", parameters=_empty_blob_params(kind))
        resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "nope"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "instinct.missing_workspace_in_blob"
        assert _status_of(client, action_id) == "pending"


@pytest.mark.parametrize("kind", GATED_KINDS)
def test_empty_blob_403_even_from_matching_empty_caller(
    kind: str, router_store, monkeypatch
) -> None:
    """Even a caller whose OWN active workspace resolves empty cannot approve an
    empty-workspace blob — empty must ALWAYS 403, never match-through (the exact
    bypass the short-circuit allowed: empty blob == empty caller → passed)."""
    client = _make_client(router_store, _FakeUser("user-X", ""), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        action_id = _propose(client, pocket_id="victim", parameters=_empty_blob_params(kind))
        resp = client.post(f"/instinct/actions/{action_id}/approve")
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "instinct.missing_workspace_in_blob"
        assert _status_of(client, action_id) == "pending"
