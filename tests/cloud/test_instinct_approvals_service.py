# tests/cloud/test_instinct_approvals_service.py
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — CRUD tests for
# the `instinct_approvals` 4-file shape. Pins:
#   * the create / list / get / approve / reject happy paths
#   * tenant-filter regression on every read
#   * emit-on-write regression for every state-mutating function
#   * Pydantic validation regression on missing required fields

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import (
    ConflictError,
    NotFound,
    ValidationError,
)
from pocketpaw_ee.cloud._core.realtime.events import (
    InstinctApprovalApproved,
    InstinctApprovalCreated,
    InstinctApprovalRejected,
)
from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service

pytestmark = pytest.mark.usefixtures("mongo_db")


def _create_body(**overrides) -> dict:
    body: dict = {
        "pocket_id": "p1",
        "action_name": "do_thing",
        "row_id": "r1",
        "row_data": {"value": 1},
        "verdict": "ESCALATE_APPROVAL",
        "reason": "operator_overlay_escalated",
        "matched_rules": [{"when": "value > 0", "action": "require_approval"}],
        "park": {"method": "POST", "path": "/items"},
    }
    body.update(overrides)
    return body


async def test_create_persists_and_emits(recording_bus) -> None:
    out = await approvals_service.create_approval("w1", "u1", _create_body())
    assert out["workspace_id"] == "w1"
    assert out["pocket_id"] == "p1"
    assert out["action_name"] == "do_thing"
    assert out["status"] == "pending"
    assert out["requested_by"] == "u1"

    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert len(created) == 1
    assert created[0].data["id"] == out["id"]


async def test_create_requires_workspace_id() -> None:
    with pytest.raises(ValidationError):
        await approvals_service.create_approval("", "u1", _create_body())


async def test_create_requires_user_id() -> None:
    with pytest.raises(ValidationError):
        await approvals_service.create_approval("w1", "", _create_body())


async def test_create_validates_pocket_id() -> None:
    body = _create_body(pocket_id="")
    # Pydantic min_length=1 raises a Pydantic ValidationError here —
    # which is *not* the CloudError ValidationError. Either fail mode
    # is acceptable; both indicate the body was rejected at entry.
    with pytest.raises(Exception):
        await approvals_service.create_approval("w1", "u1", body)


async def test_list_filters_by_workspace() -> None:
    await approvals_service.create_approval("wA", "u1", _create_body(pocket_id="pA"))
    await approvals_service.create_approval("wB", "u1", _create_body(pocket_id="pB"))

    in_a = await approvals_service.list_approvals("wA", "u1", {})
    assert len(in_a) == 1
    assert in_a[0]["pocket_id"] == "pA"

    in_b = await approvals_service.list_approvals("wB", "u1", {})
    assert len(in_b) == 1
    assert in_b[0]["pocket_id"] == "pB"


async def test_list_filters_by_status_and_pocket() -> None:
    a1 = await approvals_service.create_approval("w1", "u1", _create_body(pocket_id="p1"))
    await approvals_service.create_approval("w1", "u1", _create_body(pocket_id="p2"))
    await approvals_service.approve("w1", "u_approver", a1["id"], None)

    only_pending = await approvals_service.list_approvals("w1", "u1", {"status": "pending"})
    assert len(only_pending) == 1
    assert only_pending[0]["pocket_id"] == "p2"

    only_p1 = await approvals_service.list_approvals("w1", "u1", {"pocket_id": "p1"})
    assert len(only_p1) == 1
    assert only_p1[0]["id"] == a1["id"]


async def test_get_returns_row() -> None:
    created = await approvals_service.create_approval("w1", "u1", _create_body())
    out = await approvals_service.get_approval("w1", "u1", created["id"])
    assert out["id"] == created["id"]


async def test_get_cross_workspace_not_found() -> None:
    created = await approvals_service.create_approval("wA", "u1", _create_body())
    with pytest.raises(NotFound):
        await approvals_service.get_approval("wB", "u1", created["id"])


async def test_get_bad_id_not_found() -> None:
    with pytest.raises(NotFound):
        await approvals_service.get_approval("w1", "u1", "not-an-objectid")


async def test_approve_state_transition_and_event(recording_bus) -> None:
    created = await approvals_service.create_approval("w1", "u1", _create_body())
    recording_bus.events.clear()

    out = await approvals_service.approve("w1", "u_approver", created["id"], None)
    assert out["status"] == "approved"
    assert out["decided_by"] == "u_approver"
    assert out["decided_at"] is not None

    approved = [e for e in recording_bus.events if isinstance(e, InstinctApprovalApproved)]
    assert len(approved) == 1


async def test_reject_state_transition_and_event(recording_bus) -> None:
    created = await approvals_service.create_approval("w1", "u1", _create_body())
    recording_bus.events.clear()

    out = await approvals_service.reject("w1", "u_approver", created["id"], None)
    assert out["status"] == "rejected"

    rejected = [e for e in recording_bus.events if isinstance(e, InstinctApprovalRejected)]
    assert len(rejected) == 1


async def test_approve_already_decided_conflicts() -> None:
    created = await approvals_service.create_approval("w1", "u1", _create_body())
    await approvals_service.approve("w1", "u_approver", created["id"], None)

    with pytest.raises(ConflictError):
        await approvals_service.approve("w1", "u_approver", created["id"], None)


async def test_reject_already_decided_conflicts() -> None:
    created = await approvals_service.create_approval("w1", "u1", _create_body())
    await approvals_service.reject("w1", "u_approver", created["id"], None)

    with pytest.raises(ConflictError):
        await approvals_service.reject("w1", "u_approver", created["id"], None)


async def test_decide_requires_user_id() -> None:
    created = await approvals_service.create_approval("w1", "u1", _create_body())
    with pytest.raises(ValidationError):
        await approvals_service.approve("w1", "", created["id"], None)
