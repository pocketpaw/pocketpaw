"""Tests for the auth service.

Uses the shared ``mongo_db`` fixture so service functions exercise real
Beanie reads/writes against an isolated mongomock-motor DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import NotFound, ValidationError
from ee.cloud.auth import service as auth_service
from ee.cloud.models.user import User as _UserDoc
from ee.cloud.models.user import WorkspaceMembership

pytestmark = pytest.mark.usefixtures("mongo_db")


async def _seed_user(
    *,
    email: str = "a@b.c",
    full_name: str = "Alice",
    workspace_role: str = "member",
) -> _UserDoc:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name=full_name,
        workspaces=[
            WorkspaceMembership(
                workspace="w1",
                role=workspace_role,
                joined_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
    )
    await doc.insert()
    return doc


def _ctx(user_id: str) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=None,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime(2026, 4, 27, tzinfo=UTC),
    )


async def test_get_profile_returns_user() -> None:
    doc = await _seed_user()
    out = await auth_service.get_profile(_ctx(str(doc.id)))
    assert out.id == str(doc.id)
    assert out.email == "a@b.c"


async def test_get_profile_raises_not_found() -> None:
    from beanie import PydanticObjectId

    bogus = str(PydanticObjectId())
    with pytest.raises(NotFound):
        await auth_service.get_profile(_ctx(bogus))


async def test_update_profile_changes_full_name() -> None:
    doc = await _seed_user()
    out = await auth_service.update_profile(_ctx(str(doc.id)), full_name="Bob")
    assert out.full_name == "Bob"


async def test_update_profile_partial_only_full_name() -> None:
    doc = await _seed_user()
    await auth_service.update_profile(_ctx(str(doc.id)), full_name="Bob")
    refreshed = await _UserDoc.get(doc.id)
    assert refreshed is not None
    assert refreshed.full_name == "Bob"
    assert refreshed.avatar == ""  # untouched


async def test_set_active_workspace_persists() -> None:
    doc = await _seed_user()
    out = await auth_service.set_active_workspace(_ctx(str(doc.id)), "w42")
    assert out.active_workspace == "w42"


async def test_set_active_workspace_empty_raises_validation() -> None:
    doc = await _seed_user()
    with pytest.raises(ValidationError):
        await auth_service.set_active_workspace(_ctx(str(doc.id)), "")


async def test_set_avatar_path_persists() -> None:
    doc = await _seed_user()
    out = await auth_service.set_avatar_path(_ctx(str(doc.id)), "/api/v1/auth/avatar/u1.png")
    assert out.avatar == "/api/v1/auth/avatar/u1.png"
