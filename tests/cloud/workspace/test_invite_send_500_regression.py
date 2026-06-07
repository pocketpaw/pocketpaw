# tests/cloud/workspace/test_invite_send_500_regression.py
#
# Created 2026-06-07 — regression coverage for the unhandled 500 on
# POST /api/v1/workspaces/{id}/invites (admin sends a workspace invite).
#
# Root cause: the invite-token hashing rollout moved uniqueness from the
# `token` column to `token_hash`, but Beanie never drops indexes that
# disappear from the model. The leftover non-sparse unique index `token_1`
# survived in live deployments. New invites persist `token=None`, so the
# SECOND new invite collided on `{ token: null }` with a pymongo
# DuplicateKeyError that escaped `_mint_invite_for_email` as an unhandled
# 500 (which also drops the CORS header above the middleware, so the
# browser reports a misleading CORS error).
#
# mongomock does NOT enforce unique indexes the way real Mongo does, which
# is why the existing service/route tests never caught this. These tests
# reproduce both halves deterministically without a live Mongo:
#   - the service guard: a DuplicateKeyError on insert must become a
#     ConflictError (409), never propagate as a 500;
#   - the startup reconciler: the stale `token_1` index must be dropped so
#     the feature actually works again, and nothing else gets touched.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import ConflictError
from pocketpaw_ee.cloud.models.invite import Invite as _InviteDoc
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.shared.db import _drop_legacy_invite_token_index
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.dto import CreateInviteRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


async def _seed_user(*, email: str) -> _UserDoc:
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


def _ctx(user_id: str) -> RequestContext:
    from datetime import UTC, datetime

    return RequestContext(
        user_id=user_id,
        workspace_id=None,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _seed_workspace(owner: _UserDoc, *, slug: str) -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc

    ws_doc = _WorkspaceDoc(name="W", slug=slug, owner=str(owner.id))
    await ws_doc.insert()
    ws_id = str(ws_doc.id)
    await workspace_service._add_member(ws_id, str(owner.id), role="owner", set_active=True)
    return ws_id


# ---------------------------------------------------------------------------
# Service guard: DuplicateKeyError on insert -> ConflictError (409), not 500
# ---------------------------------------------------------------------------


async def test_create_invite_maps_duplicate_key_to_conflict(
    owner, monkeypatch, resolver_mock
) -> None:
    """A pymongo DuplicateKeyError raised by the insert must surface as a
    ConflictError (CloudError -> 409), not escape as an unhandled 500.

    Before the fix the raw DuplicateKeyError propagated out of
    create_invite. mongomock won't trigger the unique-index collision on
    its own, so we simulate the exact insert failure real Mongo produces
    against the stale `token_1` index.
    """
    from pymongo.errors import DuplicateKeyError

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create",
        _async_noop,
    )

    ws_id = await _seed_workspace(owner, slug="dup")

    async def _boom(self, *args, **kwargs):  # noqa: ANN001, ARG001
        raise DuplicateKeyError(
            "E11000 duplicate key error collection: paw-enterprise.invites "
            "index: token_1 dup key: { token: null }",
            11000,
        )

    monkeypatch.setattr(_InviteDoc, "insert", _boom, raising=True)

    with pytest.raises(ConflictError) as exc:
        await workspace_service.create_invite(
            _ctx(str(owner.id)), ws_id, CreateInviteRequest(email="fresh@x.c", role="member")
        )
    assert exc.value.code == "invite.create_conflict"


# ---------------------------------------------------------------------------
# Startup reconciler: drop the stale `token_1` unique index
# ---------------------------------------------------------------------------


async def test_drop_legacy_token_index_removes_stale_unique(mongo_db) -> None:
    """When a leftover unique `token_1` index exists, the reconciler drops
    it so new invites stop colliding on token=null."""
    coll = mongo_db["invites"]
    await coll.create_index([("token", 1)], unique=True, name="token_1")
    assert "token_1" in await coll.index_information()

    await _drop_legacy_invite_token_index(mongo_db)

    assert "token_1" not in await coll.index_information()


async def test_drop_legacy_token_index_is_noop_when_absent(mongo_db) -> None:
    """No `token_1` index → reconciler leaves the collection untouched."""
    coll = mongo_db["invites"]
    before = set(await coll.index_information())
    assert "token_1" not in before

    await _drop_legacy_invite_token_index(mongo_db)

    assert set(await coll.index_information()) == before


async def test_drop_legacy_token_index_preserves_token_hash_index(mongo_db) -> None:
    """The reconciler must only touch the legacy `token` index, never the
    current `token_hash` uniqueness the model relies on."""
    coll = mongo_db["invites"]
    await coll.create_index([("token", 1)], unique=True, name="token_1")
    await coll.create_index([("token_hash", 1)], unique=True, name="token_hash_1")

    await _drop_legacy_invite_token_index(mongo_db)

    info = await coll.index_information()
    assert "token_1" not in info
    assert "token_hash_1" in info


# ---------------------------------------------------------------------------
# End-to-end happy path through the real route (deps + real insert +
# response_model serialization). Guards against re-introducing a 500 in the
# normal admin-sends-invite flow.
# ---------------------------------------------------------------------------


async def test_admin_create_invite_fresh_email_route_returns_200() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.auth.core import current_optional_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.workspace.router import router as workspace_router

    admin = await _seed_user(email="admin@acme.test")
    ws_id = await _seed_workspace(admin, slug="acme")
    admin = await _UserDoc.get(admin.id)
    assert admin is not None

    app = FastAPI()
    add_error_handler(app)
    app.include_router(workspace_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: admin
    app.dependency_overrides[current_optional_user] = lambda: admin

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/v1/workspaces/{ws_id}/invites",
        json={"email": "fresh-invitee@acme.test", "role": "member"},
    )
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    assert resp.json()["email"] == "fresh-invitee@acme.test"


async def _async_noop(*_args, **_kwargs):
    return None


@pytest.fixture(autouse=True)
def resolver_mock(monkeypatch):
    """Stub the realtime resolver so emit-time get_resolver() doesn't explode
    (the real one needs init_realtime, which unit tests don't run)."""
    from unittest.mock import MagicMock

    mock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


@pytest.fixture
async def owner() -> _UserDoc:
    return await _seed_user(email="owner@x.c")
