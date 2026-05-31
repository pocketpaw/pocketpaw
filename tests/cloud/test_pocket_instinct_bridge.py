# tests/cloud/test_pocket_instinct_bridge.py — RFC 05 M2b.1.
# Created: 2026-05-22 — coverage for the Instinct routing bridge: the
# impure layer that proposes a parked pocket write into an Instinct
# Action and executes it after a human approval.
#
# What this pins:
#   - propose_pocket_write builds an Instinct Action carrying the
#     `_pocket_write` blob, with `assignee` defaulting to the pocket
#     owner — and NO token in the stored parameters.
#   - `approval_route` mode="user" overrides the assignee.
#   - execute_approved_write re-loads creds, re-enters run_action with
#     `from_instinct=True`, and marks the Action executed on success.
#   - a revoked backend → mark_failed with a `backend_revoked` note,
#     no write fired.
#   - a post-approval allowlist miss → mark_failed, no executed status.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.pockets import instinct_bridge  # noqa: E402

from pocketpaw.instinct.models import ActionCategory, ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated InstinctStore on a tmp file, wired into the bridge.

    The bridge lazy-imports ``get_instinct_store`` from
    ``pocketpaw.stores`` — patch it there so propose / execute share one
    temp-backed store and never touch ``~/.pocketpaw/instinct.db``.
    """
    st = InstinctStore(tmp_path / "instinct_bridge_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: st)
    return st


def _pocket(owner: str = "owner-1") -> dict:
    return {"_id": "pocket-1", "workspace": "w1", "name": "Leases", "owner": owner}


def _park(outcome: str | None = "renewal_completed") -> dict:
    """A parked-write blob — the executor's `_park` sentinel payload."""
    return {
        "action": "mark_renewed",
        "method": "POST",
        "path": "/leases/42/renew",
        "params": {"rent": 2000},
        "idempotency_key": "idem-xyz",
        "outcome": outcome,
    }


# ---------------------------------------------------------------------------
# propose_pocket_write
# ---------------------------------------------------------------------------


async def test_propose_builds_action_with_pocket_write_and_owner_assignee(store):
    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(owner="owner-1"),
        backend_config={"base_url": "https://api.example.com", "approval_route": None},
        parked_write=_park(),
        requested_by="requester-9",
    )
    action = await store.get_action(action_id)
    assert action is not None
    assert action.pocket_id == "pocket-1"
    assert action.status == ActionStatus.PENDING
    assert action.category == ActionCategory.EXTERNAL
    # The default approver is the pocket owner.
    assert action.assignee == "owner-1"
    # The parked-write blob rode along in parameters.
    blob = action.parameters["_pocket_write"]
    assert blob["action"] == "mark_renewed"
    assert blob["method"] == "POST"
    assert blob["path"] == "/leases/42/renew"
    assert blob["outcome"] == "renewal_completed"
    assert blob["requested_by"] == "requester-9"
    assert blob["workspace_id"] == "w1"


async def test_propose_never_stores_a_token(store):
    """No credential / token field reaches the Instinct DB — the blob
    carries only method/path/params/idempotency/outcome + context."""
    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(),
        backend_config={"base_url": "https://api.example.com"},
        parked_write=_park(),
        requested_by="requester-9",
    )
    action = await store.get_action(action_id)
    import json

    serialized = json.dumps(action.parameters)
    assert "token" not in serialized
    blob = action.parameters["_pocket_write"]
    assert "token" not in blob
    assert "auth_token" not in blob


async def test_propose_honors_approval_route_user_override(store):
    """An `approval_route` of mode=user routes the assignee to the named
    member instead of the owner."""
    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(owner="owner-1"),
        backend_config={
            "base_url": "https://api.example.com",
            "approval_route": {"mode": "user", "user_id": "approver-7"},
        },
        parked_write=_park(),
        requested_by="requester-9",
    )
    action = await store.get_action(action_id)
    assert action.assignee == "approver-7"


async def test_propose_owner_mode_route_falls_back_to_owner(store):
    """An explicit mode=owner route resolves to the pocket owner."""
    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(owner="owner-1"),
        backend_config={"approval_route": {"mode": "owner", "user_id": None}},
        parked_write=_park(),
        requested_by="requester-9",
    )
    action = await store.get_action(action_id)
    assert action.assignee == "owner-1"


# ---------------------------------------------------------------------------
# execute_approved_write
# ---------------------------------------------------------------------------


async def test_execute_approved_write_fires_and_marks_executed(store, monkeypatch):
    """A happy-path execution: creds re-loaded, run_action re-entered with
    from_instinct=True, the Action flipped to EXECUTED."""
    captured = {}

    async def _get_creds(workspace_id, pocket_id):
        return ("https://api.example.com", "bearer", None, "tok", [], None)

    async def _run_action(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "action": kwargs["action"], "status": 200, "response": {}}

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor", _get_creds
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.action_executor.run_action", _run_action)

    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(),
        backend_config=None,
        parked_write=_park(),
        requested_by="requester-9",
    )
    approved = await store.approve(action_id, approver="owner-1")
    await instinct_bridge.execute_approved_write(approved)

    # The executor was re-entered with from_instinct=True — the gate is
    # skipped, the HTTP call is actually made.
    assert captured["from_instinct"] is True
    assert captured["action"] == "mark_renewed"
    assert captured["path"] == "/leases/42/renew"
    assert captured["token"] == "tok"
    # The Action is now EXECUTED.
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED


async def test_execute_approved_write_revoked_backend_marks_failed(store, monkeypatch):
    """A backend revoked between propose and approve → the write is NOT
    fired and the Action is marked failed with a backend_revoked note."""
    fired = {"hit": False}

    async def _no_creds(workspace_id, pocket_id):
        return None

    async def _run_action(**kwargs):
        fired["hit"] = True
        return {"ok": True}

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor", _no_creds
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.action_executor.run_action", _run_action)

    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(),
        backend_config=None,
        parked_write=_park(),
        requested_by="requester-9",
    )
    approved = await store.approve(action_id)
    await instinct_bridge.execute_approved_write(approved)

    assert fired["hit"] is False
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.FAILED
    assert "backend_revoked" in (final.error or "")


async def test_execute_approved_write_allowlist_miss_marks_failed(store, monkeypatch):
    """A post-approval re-validation rejection (the allowlist no longer
    covers the write) → the Action is marked failed, never executed."""

    async def _get_creds(workspace_id, pocket_id):
        return ("https://api.example.com", "bearer", None, "tok", [], None)

    async def _run_action(**kwargs):
        # The executor rejected the write — the allowlist miss.
        return {"ok": False, "code": "not_allowed", "error": "not in allowlist"}

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor", _get_creds
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.action_executor.run_action", _run_action)

    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(),
        backend_config=None,
        parked_write=_park(),
        requested_by="requester-9",
    )
    approved = await store.approve(action_id)
    await instinct_bridge.execute_approved_write(approved)

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.FAILED
    assert final.status != ActionStatus.EXECUTED
    assert "not_allowed" in (final.error or "")


async def test_execute_approved_write_emits_outcome_on_success(store, monkeypatch):
    """A gated write that succeeds emits a `pocket.outcome` event with
    via_instinct=True and the Instinct action id."""
    emitted = []

    async def _get_creds(workspace_id, pocket_id):
        return ("https://api.example.com", "bearer", None, "tok", [], None)

    async def _run_action(**kwargs):
        return {"ok": True, "action": kwargs["action"], "status": 200}

    async def _emit(event):
        emitted.append(event)

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor", _get_creds
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.action_executor.run_action", _run_action)
    monkeypatch.setattr("pocketpaw_ee.cloud.outcomes.service.emit", _emit)

    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(),
        backend_config=None,
        parked_write=_park(outcome="renewal_completed"),
        requested_by="requester-9",
    )
    approved = await store.approve(action_id, approver="approver-7")
    await instinct_bridge.execute_approved_write(approved)

    assert len(emitted) == 1
    data = emitted[0].data
    assert data["outcome"] == "renewal_completed"
    assert data["via_instinct"] is True
    assert data["instinct_action_id"] == action_id
    assert data["actor"] == "approver-7"


async def test_execute_approved_write_no_outcome_emits_nothing(store, monkeypatch):
    """A gated write whose binding declared NO outcome emits no event."""
    emitted = []

    async def _get_creds(workspace_id, pocket_id):
        return ("https://api.example.com", "bearer", None, "tok", [], None)

    async def _run_action(**kwargs):
        return {"ok": True, "action": kwargs["action"], "status": 200}

    async def _emit(event):
        emitted.append(event)

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor", _get_creds
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.action_executor.run_action", _run_action)
    monkeypatch.setattr("pocketpaw_ee.cloud.outcomes.service.emit", _emit)

    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(),
        backend_config=None,
        parked_write=_park(outcome=None),
        requested_by="requester-9",
    )
    approved = await store.approve(action_id)
    await instinct_bridge.execute_approved_write(approved)

    assert emitted == []
