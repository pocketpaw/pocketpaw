"""Tests for the workspace audit-event log (Wave 2 Task 10).

Verifies:
- Every mutating workspace op writes exactly one audit row with the
  right action / target / actor.
- ``list_events`` filters by action and respects composite ``(at, _id)``
  cursor pagination ordering.
- The ``GET /workspaces/{id}/audit`` endpoint is admin-gated — a plain
  member receives 403 via ``require_action("audit.read")``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.audit.dto import AuditQueryRequest
from pocketpaw_ee.cloud.audit.router import workspace_router
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.audit_event import AuditEvent as _AuditEventDoc
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.user import WorkspaceMembership as _Membership
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.dto import (
    BulkInviteRequest,
    CreateInviteRequest,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _seed_user(email: str = "owner@x.c") -> _UserDoc:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="U",
        workspaces=[],
    )
    await doc.insert()
    return doc


@pytest.fixture(autouse=True)
def _resolver_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub the realtime resolver — same shape as workspace tests."""
    mock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


@pytest.fixture
def _legacy_bus(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    class _FakeBus:
        async def emit(self, name: str, payload: dict) -> None:
            events.append((name, payload))

    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.event_bus", _FakeBus())
    return events


async def _rows_for(workspace_id: str) -> list[_AuditEventDoc]:
    return await _AuditEventDoc.find({"workspace": workspace_id}).sort("at").to_list()


# ---------------------------------------------------------------------------
# record() write fan-out from workspace mutations
# ---------------------------------------------------------------------------


async def test_create_workspace_writes_audit_row() -> None:
    owner = await _seed_user()
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    rows = await _rows_for(ws.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "workspace.created"
    assert row.actor_id == str(owner.id)
    assert row.target_type == "workspace"
    assert row.target_id == ws.id


async def test_update_workspace_writes_audit_row() -> None:
    owner = await _seed_user()
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service.update(_ctx(str(owner.id)), ws.id, UpdateWorkspaceRequest(name="B"))
    rows = await _rows_for(ws.id)
    actions = [r.action for r in rows]
    assert "workspace.updated" in actions


async def test_delete_workspace_writes_audit_row() -> None:
    owner = await _seed_user()
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service.delete(_ctx(str(owner.id)), ws.id)
    rows = await _rows_for(ws.id)
    assert "workspace.deleted" in [r.action for r in rows]


async def test_update_member_role_writes_audit_row() -> None:
    owner = await _seed_user("o@x.c")
    other = await _seed_user("p@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(other.id), role="member")
    await workspace_service.update_member_role(ws.id, str(other.id), "admin", str(owner.id))
    rows = [r for r in await _rows_for(ws.id) if r.action == "workspace.member_role_changed"]
    assert len(rows) == 1
    assert rows[0].metadata == {"from_role": "member", "to_role": "admin"}
    assert rows[0].target_id == str(other.id)


async def test_remove_member_writes_audit_row(_legacy_bus) -> None:
    owner = await _seed_user("o@x.c")
    other = await _seed_user("p@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(other.id), role="member")
    await workspace_service.remove_member(ws.id, str(other.id), str(owner.id))
    rows = [r for r in await _rows_for(ws.id) if r.action == "workspace.member_removed"]
    assert len(rows) == 1
    assert rows[0].target_id == str(other.id)


async def test_create_invite_writes_audit_row() -> None:
    owner = await _seed_user("o@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="new@x.c", role="member"),
    )
    rows = [r for r in await _rows_for(ws.id) if r.action == "workspace.invite_created"]
    assert len(rows) == 1
    assert rows[0].metadata["invite_id"] == invite.id
    assert rows[0].metadata["email"] == "new@x.c"


async def test_bulk_create_invites_writes_per_email_rows() -> None:
    owner = await _seed_user("o@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service.bulk_create_invites(
        _ctx(str(owner.id)),
        ws.id,
        BulkInviteRequest(emails=["a@x.c", "b@x.c", "c@x.c"], role="member"),
    )
    rows = [r for r in await _rows_for(ws.id) if r.action == "workspace.invite_created"]
    assert len(rows) == 3
    assert {r.metadata["email"] for r in rows} == {"a@x.c", "b@x.c", "c@x.c"}


async def test_accept_invite_writes_audit_row() -> None:
    owner = await _seed_user("o@x.c")
    invitee = await _seed_user("i@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="i@x.c", role="member"),
    )
    await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)
    rows = [r for r in await _rows_for(ws.id) if r.action == "workspace.invite_accepted"]
    assert len(rows) == 1
    assert rows[0].actor_id == str(invitee.id)


async def test_revoke_invite_writes_audit_row() -> None:
    owner = await _seed_user("o@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="x@x.c", role="member"),
    )
    await workspace_service.revoke_invite(ws.id, invite.id, str(owner.id))
    rows = [r for r in await _rows_for(ws.id) if r.action == "workspace.invite_revoked"]
    assert len(rows) == 1
    assert rows[0].metadata["invite_id"] == invite.id


async def test_decline_invite_writes_audit_row() -> None:
    owner = await _seed_user("o@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="x@x.c", role="member"),
    )
    await workspace_service.decline_invite(invite.token)
    rows = [r for r in await _rows_for(ws.id) if r.action == "workspace.invite_declined"]
    assert len(rows) == 1
    assert rows[0].actor_id == "email:x@x.c"


# ---------------------------------------------------------------------------
# list_events filtering and cursor pagination
# ---------------------------------------------------------------------------


async def test_list_events_filters_by_action() -> None:
    owner = await _seed_user()
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service.update(_ctx(str(owner.id)), ws.id, UpdateWorkspaceRequest(name="B"))

    page = await audit_service.list_events(ws.id, AuditQueryRequest(action="workspace.updated"))
    assert len(page.items) == 1
    assert page.items[0].action == "workspace.updated"


async def test_list_events_cursor_pagination_is_stable() -> None:
    owner = await _seed_user()
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    # Seed enough rows to require multiple pages.
    for i in range(7):
        await audit_service.record(
            ws.id,
            str(owner.id),
            "workspace.updated",
            target_type="workspace",
            target_id=ws.id,
            metadata={"i": i},
        )

    seen_ids: list[str] = []
    cursor: str | None = None
    while True:
        page = await audit_service.list_events(ws.id, AuditQueryRequest(limit=3, cursor=cursor))
        seen_ids.extend(item.id for item in page.items)
        if not page.next_cursor:
            break
        cursor = page.next_cursor

    # No duplicates, every row exactly once (8 rows total: 1 create + 7 updates).
    assert len(seen_ids) == len(set(seen_ids))
    assert len(seen_ids) == 8


async def test_list_events_bad_cursor_raises() -> None:
    owner = await _seed_user()
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    with pytest.raises(Exception) as exc:
        await audit_service.list_events(ws.id, AuditQueryRequest(cursor="garbage"))
    assert "audit.bad_cursor" in str(exc.value) or hasattr(exc.value, "code")


# ---------------------------------------------------------------------------
# HTTP-layer: require_action("audit.read") gates non-admins.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def audit_client_member() -> AsyncClient:
    """FastAPI app with the audit workspace_router mounted under a member user."""

    member = await _seed_user("member@x.c")
    member.workspaces = [
        _Membership(workspace="ws-test-1", role="member", joined_at=datetime.now(UTC))
    ]
    await member.save()

    app = FastAPI()
    add_error_handler(app)
    app.include_router(workspace_router, prefix="/api/v1")

    async def _override_user() -> Any:
        return await _UserDoc.get(member.id)

    app.dependency_overrides[current_active_user] = _override_user
    app.dependency_overrides[require_license] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def test_member_gets_403_on_audit_read(audit_client_member: AsyncClient) -> None:
    resp = await audit_client_member.get("/api/v1/workspaces/ws-test-1/audit")
    assert resp.status_code == 403
