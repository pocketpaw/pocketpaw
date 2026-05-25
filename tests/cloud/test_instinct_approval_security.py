# tests/cloud/test_instinct_approval_security.py — PR #1183 security-review.
# Created: 2026-05-22 — regression coverage for the security-review fixes
# on the Instinct routing + outcome-emission PR:
#
#   BLOCKER 1   — a workspace-A approver gets 403 approving a workspace-B
#                 `_pocket_write` action, on BOTH the single-approve and
#                 the bulk-approve routes.
#   BLOCKER 2   — bulk-approving a `_pocket_write` action actually fires
#                 `execute_approved_write`; the action reaches `executed`.
#   SHOULD-FIX 1— the audit `approved_by` actor is the authenticated user
#                 id, not the free-text `approver` request field.
#   SHOULD-FIX 3— `count_outcomes` does not count a row whose
#                 `workspace_id` is another workspace's.
#
# Router tests are sync and drive the API entirely over HTTP (seed via
# POST /instinct/actions, read back via GET /instinct/actions) so the
# TestClient owns the only event loop. SHOULD-FIX 3 is a plain async
# service test. `pocketpaw_ee` is import-skipped on an OSS-only install.

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
from pocketpaw_ee.cloud.outcomes import service as outcomes_service  # noqa: E402
from pocketpaw_ee.cloud.outcomes.dto import CountOutcomesRequest  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

TRIGGER = {"type": "agent", "source": "claude", "reason": "security test"}


# ---------------------------------------------------------------------------
# Fixtures — a router-level TestClient with seeded auth + a CloudError handler
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Duck-typed User. ``id`` is the authenticated identity; the
    audit/outcome actor must resolve to THIS, never the request body."""

    def __init__(self, user_id: str = "user-A", workspace_id: str = "ws-A") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _pocket_write_params(workspace_id: str, outcome: str | None = "renewal_completed") -> dict:
    """An Action ``parameters`` payload carrying a parked pocket write — the
    shape ``instinct_bridge.propose_pocket_write`` stores.

    Schema 2 (RFC 09 Slice 2) added the chain-correlation fields
    ``correlation_id`` (the Decision-Graph chain id minted by the
    action_executor at the moment ``agent.proposed`` fired) and
    ``parked_policy_event_id`` (populated by Slice 3 when Instinct emits
    the parked ``policy.evaluated(passed=False)``). These tests don't
    exercise the chain semantics — they pin the security / re-entry
    guarantees of the bridge — but the schema check in
    ``execute_approved_write`` rejects any blob whose schema doesn't
    match the current ``_POCKET_WRITE_SCHEMA``, so the round-trip needs
    matching shape.
    """
    return {
        "_pocket_write": {
            "schema": 2,
            "action": "mark_renewed",
            "method": "POST",
            "path": "/leases/42/renew",
            "params": {"rent": 2000},
            "idempotency_key": "idem-xyz",
            "outcome": outcome,
            "workspace_id": workspace_id,
            "requested_by": "requester-9",
            # RFC 09 Slice 2 — schema-2 chain-correlation fields. The
            # bridge accepts None for both: a malformed correlation_id
            # falls through to ``run_action``'s own mint, and
            # ``parked_policy_event_id`` is populated by Slice 3.
            "correlation_id": None,
            "parked_policy_event_id": None,
        }
    }


@pytest.fixture
def router_store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "approval_security.db")


def _make_client(router_store: InstinctStore, user: _FakeUser, monkeypatch) -> TestClient:
    """Build a TestClient over the instinct router with a CloudError handler
    registered so a ``Forbidden`` maps to a 403 (not a 500)."""
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
    """Seed a pending action over HTTP and return its id."""
    payload: dict = {"pocket_id": pocket_id, "title": title, "trigger": TRIGGER}
    if parameters is not None:
        payload["parameters"] = parameters
    resp = client.post("/instinct/actions", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _status_of(client: TestClient, action_id: str) -> str:
    """Read an action's current status back over HTTP."""
    resp = client.get("/instinct/actions", params={"limit": 500})
    assert resp.status_code == 200, resp.text
    for action in resp.json()["actions"]:
        if action["id"] == action_id:
            return action["status"]
    raise AssertionError(f"action {action_id} not found")


# ---------------------------------------------------------------------------
# BLOCKER 1 — cross-tenant approval escalation
# ---------------------------------------------------------------------------


class TestBlocker1CrossTenantApproval:
    def test_single_approve_of_foreign_workspace_pocket_write_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A ws-A approver POSTing /approve on an action whose parked write
        belongs to ws-B is forbidden — the action stays pending."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-B",
                title="ws-B write",
                parameters=_pocket_write_params(workspace_id="ws-B"),
            )
            resp = client.post(f"/instinct/actions/{action_id}/approve")
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, action_id) == "pending"

    def test_single_approve_of_own_workspace_pocket_write_succeeds(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A ws-A approver may approve a ws-A parked write — the workspace
        check does not block a same-tenant approval."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)

        async def _noop_execute(_action):
            return None

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write",
            _noop_execute,
        )
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-A",
                title="ws-A write",
                parameters=_pocket_write_params(workspace_id="ws-A"),
            )
            resp = client.post(f"/instinct/actions/{action_id}/approve")
            assert resp.status_code == 200, resp.text
            assert resp.json()["action"]["status"] == "approved"

    def test_bulk_approve_of_foreign_workspace_pocket_write_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A ws-A approver bulk-approving a list that contains a ws-B parked
        write is forbidden — and no action in the batch flips."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            own = _propose(
                client,
                pocket_id="pocket-A",
                title="ws-A write",
                parameters=_pocket_write_params(workspace_id="ws-A"),
            )
            foreign = _propose(
                client,
                pocket_id="pocket-B",
                title="ws-B write",
                parameters=_pocket_write_params(workspace_id="ws-B"),
            )
            resp = client.post(
                "/instinct/actions/bulk-approve",
                json={"ids": [own, foreign]},
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            # The whole batch is rejected — nothing flipped.
            assert _status_of(client, own) == "pending"
            assert _status_of(client, foreign) == "pending"


# ---------------------------------------------------------------------------
# BLOCKER 2 — bulk-approve must fire parked pocket writes
# ---------------------------------------------------------------------------


class TestBlocker2BulkApproveFiresWrites:
    def test_bulk_approve_fires_pocket_write_and_action_reaches_executed(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A bulk-approved `_pocket_write` action runs through
        `execute_approved_write`; the executor is re-entered with
        from_instinct=True and the action lands at EXECUTED."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        captured: dict = {}

        async def _get_creds(workspace_id, pocket_id):
            return ("https://api.example.com", "bearer", None, "tok", [], None)

        async def _run_action(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "action": kwargs["action"], "status": 200, "response": {}}

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor", _get_creds
        )
        monkeypatch.setattr("pocketpaw_ee.cloud.pockets.action_executor.run_action", _run_action)
        # The bridge lazy-imports get_instinct_store from pocketpaw.stores —
        # point it at the same router_store so propose + execute share state.
        monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: router_store)

        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-A",
                title="ws-A write",
                parameters=_pocket_write_params(workspace_id="ws-A"),
            )
            resp = client.post("/instinct/actions/bulk-approve", json={"ids": [action_id]})
            assert resp.status_code == 200, resp.text
            assert len(resp.json()["affected"]) == 1

            # The write actually fired — re-entered the executor.
            assert captured.get("from_instinct") is True
            assert captured.get("action") == "mark_renewed"
            # The action reached EXECUTED (the bridge's mark_executed).
            assert _status_of(client, action_id) == "executed"

    def test_bulk_approve_pocket_write_failure_does_not_break_response(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A crash inside `execute_approved_write` is swallowed — the bulk
        response still returns 200."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)

        async def _boom(_action):
            raise RuntimeError("bridge blew up")

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write", _boom
        )
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-A",
                title="ws-A write",
                parameters=_pocket_write_params(workspace_id="ws-A"),
            )
            resp = client.post("/instinct/actions/bulk-approve", json={"ids": [action_id]})
            assert resp.status_code == 200, resp.text
            assert len(resp.json()["affected"]) == 1


# ---------------------------------------------------------------------------
# SHOULD-FIX 1 — the audit actor is the authenticated user, not the body
# ---------------------------------------------------------------------------


class TestShouldFix1AuthenticatedActor:
    def test_single_approve_audit_actor_is_authenticated_user(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A request body with `approver="admin"` cannot forge the audit
        actor — the `approved_by` + audit row carry the authenticated id."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(client, pocket_id="pocket-A", title="plain action")
            resp = client.post(
                f"/instinct/actions/{action_id}/approve",
                json={"approver": "admin"},  # forged actor attempt
            )
            assert resp.status_code == 200, resp.text
            # The persisted Action records the authenticated id.
            assert resp.json()["action"]["approved_by"] == "user-A"

            # The audit row's actor is the authenticated id, never "admin".
            audit = client.get("/instinct/audit", params={"pocket_id": "pocket-A"})
            assert audit.status_code == 200, audit.text
            approved_rows = [e for e in audit.json()["entries"] if e["event"] == "action_approved"]
            assert len(approved_rows) == 1
            assert approved_rows[0]["actor"] == "user-A"
            assert approved_rows[0]["actor"] != "admin"

    def test_bulk_approve_audit_actor_is_authenticated_user(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """Bulk-approve also stamps the authenticated id, ignoring a forged
        `approver` field in the body."""
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(client, pocket_id="pocket-A", title="plain action")
            resp = client.post(
                "/instinct/actions/bulk-approve",
                json={"ids": [action_id], "approver": "admin"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["affected"][0]["approved_by"] == "user-A"

            audit = client.get("/instinct/audit", params={"pocket_id": "pocket-A"})
            assert audit.status_code == 200, audit.text
            approved_rows = [e for e in audit.json()["entries"] if e["event"] == "action_approved"]
            assert len(approved_rows) == 1
            assert approved_rows[0]["actor"] == "user-A"


# ---------------------------------------------------------------------------
# SHOULD-FIX 3 — count_outcomes ignores a foreign-workspace row
# ---------------------------------------------------------------------------


async def test_count_outcomes_skips_foreign_workspace_rows(tmp_path: Path) -> None:
    """A ledger row carrying another workspace's id (a corrupt or
    hand-edited line) is not counted into this workspace's totals."""
    import json

    outcomes_service.set_ledger_dir(tmp_path / "outcomes")
    try:
        ledger = outcomes_service._ledger_path("ws-A")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"outcome": "real", "pocket_id": "p1", "workspace_id": "ws-A"},
            # A line that drifted in from another tenant — must NOT count.
            {"outcome": "leaked", "pocket_id": "p9", "workspace_id": "ws-B"},
            {"outcome": "real2", "pocket_id": "p2", "workspace_id": "ws-A"},
        ]
        with ledger.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

        counts = await outcomes_service.count_outcomes("ws-A", CountOutcomesRequest())
        assert counts.total == 2
        assert counts.by_outcome == {"real": 1, "real2": 1}
        assert "leaked" not in counts.by_outcome
    finally:
        outcomes_service.set_ledger_dir("~/.pocketpaw/outcomes")
