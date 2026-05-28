"""Wave 3 Task 14: cascade revocations on workspace member removal.

Asserts that ``remove_member`` cascades through:
- API keys scoped to the workspace (other-workspace keys untouched)
- All auth sessions for the user (system-wide; Redis revocation set
  populated for the per-token denylist)
- Pending invites the user issued for this workspace
  (``revoked_reason='inviter_removed'``)
- An audit-log row with the cascade counts in ``metadata``
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

# ruff: noqa: I001, E402
# Why: importing ``models.user`` BEFORE the other cloud imports primes the
# calendar→cloud.shared.deps chain so ``pocketpaw_ee.cloud.auth`` finishes
# initialising in the right order. Ruff's import sort would scramble this
# and break test collection — keep the manual order.
import fakeredis.aioredis
import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from pocketpaw_ee.cloud.models.user import User as _UserDoc  # must come first
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.auth import api_keys as api_keys_service
from pocketpaw_ee.cloud.models.api_key import APIKey
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.auth_session import AuthSession
from pocketpaw_ee.cloud.models.invite import Invite as _InviteDoc
from pocketpaw_ee.cloud.models.invite import hash_token
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.dto import CreateWorkspaceRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _seed_user(*, email: str, full_name: str = "") -> _UserDoc:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name=full_name,
        workspaces=[],
    )
    await doc.insert()
    return doc


@pytest.fixture(autouse=True)
def resolver_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


@pytest_asyncio.fixture
async def fake_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    yield fake


async def test_remove_member_cascades_revocations(fake_redis) -> None:
    owner = await _seed_user(email="owner@x.c", full_name="Owner")
    target = await _seed_user(email="target@x.c", full_name="Target")

    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    other_ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="B", slug="b")
    )

    # Target is a member of both workspaces.
    await workspace_service._add_member(ws.id, str(target.id), role="member")
    await workspace_service._add_member(other_ws.id, str(target.id), role="member")

    # Two API keys scoped to ws (one already revoked — must NOT be counted).
    k1, _ = await api_keys_service.create_api_key(
        workspace_id=ws.id, owner_user_id=str(target.id), name="k1", scopes=[]
    )
    k2, _ = await api_keys_service.create_api_key(
        workspace_id=ws.id, owner_user_id=str(target.id), name="k2", scopes=[]
    )
    # One API key in OTHER workspace — must remain active.
    k_other, _ = await api_keys_service.create_api_key(
        workspace_id=other_ws.id, owner_user_id=str(target.id), name="k_other", scopes=[]
    )

    # Two sessions for target.
    s1 = AuthSession(user_id=str(target.id), jti="jti-1", device_label="d1")
    s2 = AuthSession(user_id=str(target.id), jti="jti-2", device_label="d2")
    await s1.insert()
    await s2.insert()

    # Pending invite issued by target for this workspace.
    inv = _InviteDoc(
        workspace=ws.id,
        email="newhire@x.c",
        role="member",
        invited_by=str(target.id),
        token=None,
        token_hash=hash_token("pl-" + str(target.id)),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    await inv.insert()

    # Act.
    await workspace_service.remove_member(ws.id, str(target.id), str(owner.id))

    # Membership flipped off the User doc.
    refreshed = await _UserDoc.get(target.id)
    assert refreshed is not None
    assert all(m.workspace != ws.id for m in refreshed.workspaces)
    # Still a member of other_ws.
    assert any(m.workspace == other_ws.id for m in refreshed.workspaces)

    # API keys: both ws-scoped keys revoked, other_ws key intact.
    k1_refreshed = await APIKey.get(PydanticObjectId(str(k1.id)))
    k2_refreshed = await APIKey.get(PydanticObjectId(str(k2.id)))
    k_other_refreshed = await APIKey.get(PydanticObjectId(str(k_other.id)))
    assert k1_refreshed is not None and k1_refreshed.revoked is True
    assert k2_refreshed is not None and k2_refreshed.revoked is True
    assert k_other_refreshed is not None and k_other_refreshed.revoked is False

    # Sessions: every row for the user is revoked.
    sessions = await AuthSession.find(AuthSession.user_id == str(target.id)).to_list()
    assert len(sessions) == 2
    assert all(s.revoked for s in sessions)

    # Redis carries one per-jti marker per revoked session.
    assert await fake_redis.exists(f"revoked_jti:{target.id}:jti-1")
    assert await fake_redis.exists(f"revoked_jti:{target.id}:jti-2")

    # Invite flipped revoked with the cascade reason.
    inv_refreshed = await _InviteDoc.get(inv.id)
    assert inv_refreshed is not None
    assert inv_refreshed.revoked is True
    assert inv_refreshed.revoked_reason == "inviter_removed"

    # Audit row with cascade counts.
    audit_rows = await AuditEvent.find(
        AuditEvent.workspace == ws.id,
        AuditEvent.action == "workspace.member_removed",
    ).to_list()
    assert len(audit_rows) == 1
    cascade = audit_rows[0].metadata.get("cascade") or {}
    assert cascade.get("api_keys_revoked") == 2
    assert cascade.get("sessions_revoked") == 2
    assert cascade.get("invites_revoked") == 1


async def test_remove_member_audit_counts_zero_when_nothing_to_revoke(fake_redis) -> None:
    owner = await _seed_user(email="o@x.c")
    target = await _seed_user(email="t@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(target.id), role="member")

    await workspace_service.remove_member(ws.id, str(target.id), str(owner.id))

    audit_rows = await AuditEvent.find(
        AuditEvent.workspace == ws.id,
        AuditEvent.action == "workspace.member_removed",
    ).to_list()
    assert len(audit_rows) == 1
    cascade = audit_rows[0].metadata.get("cascade") or {}
    assert cascade == {
        "api_keys_revoked": 0,
        "sessions_revoked": 0,
        "invites_revoked": 0,
    }


async def test_remove_member_skips_already_revoked_api_keys(fake_redis) -> None:
    owner = await _seed_user(email="o@x.c")
    target = await _seed_user(email="t@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(target.id), role="member")

    # Pre-revoked key — should not count in cascade total.
    k1, _ = await api_keys_service.create_api_key(
        workspace_id=ws.id, owner_user_id=str(target.id), name="k1", scopes=[]
    )
    k1.revoked = True
    await k1.save()

    # Active key — counts.
    await api_keys_service.create_api_key(
        workspace_id=ws.id, owner_user_id=str(target.id), name="k2", scopes=[]
    )

    await workspace_service.remove_member(ws.id, str(target.id), str(owner.id))

    audit_rows = await AuditEvent.find(
        AuditEvent.workspace == ws.id,
        AuditEvent.action == "workspace.member_removed",
    ).to_list()
    assert audit_rows[0].metadata["cascade"]["api_keys_revoked"] == 1
