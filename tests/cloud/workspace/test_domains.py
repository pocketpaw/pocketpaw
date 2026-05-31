"""Tests for verified-domain capture + auto-join (Wave 3 Task 12)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import Workspace
from pocketpaw_ee.cloud.workspace import domains as domains_service
from pocketpaw_ee.cloud.workspace.router import router as workspace_router

_ADMIN_EMAIL = "admin@acme.com"
_ADMIN_PASSWORD = "StrongPass123!"


# ---------------------------------------------------------------------------
# DNS mocking — dnspython returns answers whose elements expose `.strings`
# as a list of bytes (a single TXT record can be split across chunks that
# must be concatenated). The shape below matches that contract.
# ---------------------------------------------------------------------------


class _MockRR:
    def __init__(self, value: str) -> None:
        self.strings = [value.encode()]


def _resolver_returning(values: list[str]) -> AsyncMock:
    """Returns an AsyncMock that mimics ``dns.asyncresolver.resolve``."""
    mock = AsyncMock(return_value=[_MockRR(v) for v in values])
    return mock


def _resolver_raising() -> AsyncMock:
    import dns.exception

    return AsyncMock(side_effect=dns.exception.DNSException("no answer"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(workspace_router, prefix="/api/v1")
    return app


async def _seed_admin_and_workspace() -> tuple[str, str]:
    async for db in get_user_db():
        manager = UserManager(db)
        admin = await manager.create(UserCreate(email=_ADMIN_EMAIL, password=_ADMIN_PASSWORD))
        break
    ws = Workspace(name="Acme", slug="acme", owner=str(admin.id))
    await ws.insert()
    admin.workspaces.append(WorkspaceMembership(workspace=str(ws.id), role="owner"))
    admin.active_workspace = str(ws.id)
    await admin.save()
    return str(admin.id), str(ws.id)


@pytest_asyncio.fixture
async def env(mongo_db):  # noqa: ARG001
    admin_id, ws_id = await _seed_admin_and_workspace()
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/api/v1/auth/login",
            data={"username": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code in (200, 204), resp.text
        yield client, admin_id, ws_id


@pytest_asyncio.fixture
async def member_client(env, mongo_db):  # noqa: ARG001
    """A logged-in non-admin client attached to the same workspace."""
    _admin_client, _admin_id, ws_id = env
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "member@acme.com", "password": _ADMIN_PASSWORD},
        )
        assert resp.status_code in (200, 201), resp.text
        # The registration may auto-join via verified-domain (none verified yet)
        # — but we explicitly attach as a regular member to test the admin guard.
        member = await User.find_one(User.email == "member@acme.com")
        assert member is not None
        if not any(m.workspace == ws_id for m in member.workspaces):
            member.workspaces.append(WorkspaceMembership(workspace=ws_id, role="member"))
        member.active_workspace = ws_id
        await member.save()
        await client.post(
            "/api/v1/auth/login",
            data={"username": "member@acme.com", "password": _ADMIN_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        yield client, ws_id


# ---------------------------------------------------------------------------
# Service-level: normalize + add + remove
# ---------------------------------------------------------------------------


def test_normalize_domain_strips_and_lowercases():
    assert domains_service.normalize_domain("  Acme.com  ") == "acme.com"
    assert domains_service.normalize_domain("@Acme.COM") == "acme.com"
    assert domains_service.normalize_domain("https://Acme.com/path") == "acme.com"
    assert domains_service.normalize_domain("acme.com:443") == "acme.com"


def test_normalize_domain_rejects_garbage():
    for bad in ("", "no-dot", "spaces in.com", "@@", "foo@bar.com"):
        with pytest.raises(ValidationError):
            domains_service.normalize_domain(bad)


@pytest.mark.asyncio
async def test_add_domain_returns_token_idempotent(env):
    _client, _admin_id, ws_id = env
    entry1 = await domains_service.add_domain(ws_id, "Acme.com")
    assert entry1.domain == "acme.com"
    assert entry1.verification_token.startswith("paw-verify=")
    assert entry1.verified is False

    # Re-adding the same domain returns the SAME token (idempotent).
    entry2 = await domains_service.add_domain(ws_id, "acme.com")
    assert entry2.verification_token == entry1.verification_token


@pytest.mark.asyncio
async def test_list_and_remove_domain(env):
    _client, _admin_id, ws_id = env
    await domains_service.add_domain(ws_id, "acme.com")
    await domains_service.add_domain(ws_id, "acme.io")

    listed = await domains_service.list_domains(ws_id)
    assert {e.domain for e in listed} == {"acme.com", "acme.io"}

    await domains_service.remove_domain(ws_id, "acme.io")
    listed = await domains_service.list_domains(ws_id)
    assert {e.domain for e in listed} == {"acme.com"}

    with pytest.raises(NotFound):
        await domains_service.remove_domain(ws_id, "acme.io")


# ---------------------------------------------------------------------------
# DNS verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_domain_matches_txt(env):
    _client, _admin_id, ws_id = env
    entry = await domains_service.add_domain(ws_id, "acme.com")

    with patch(
        "dns.asyncresolver.resolve",
        _resolver_returning(["some-other-record", entry.verification_token]),
    ):
        verified = await domains_service.verify_domain(ws_id, "acme.com")
    assert verified.verified is True
    assert verified.verified_at is not None

    # Persisted on the workspace doc.
    ws = await Workspace.get(PydanticObjectId(ws_id))
    assert ws is not None
    persisted = next(d for d in ws.verified_domains if d.domain == "acme.com")
    assert persisted.verified is True


@pytest.mark.asyncio
async def test_verify_domain_no_matching_record_forbidden(env):
    _client, _admin_id, ws_id = env
    await domains_service.add_domain(ws_id, "acme.com")

    with patch("dns.asyncresolver.resolve", _resolver_returning(["unrelated-record"])):
        with pytest.raises(Forbidden) as exc:
            await domains_service.verify_domain(ws_id, "acme.com")
    assert exc.value.code == "domain.txt_not_found"


@pytest.mark.asyncio
async def test_verify_domain_dns_failure_forbidden(env):
    _client, _admin_id, ws_id = env
    await domains_service.add_domain(ws_id, "acme.com")

    with patch("dns.asyncresolver.resolve", _resolver_raising()):
        with pytest.raises(Forbidden):
            await domains_service.verify_domain(ws_id, "acme.com")


@pytest.mark.asyncio
async def test_verify_domain_unknown_raises_not_found(env):
    _client, _admin_id, ws_id = env
    with pytest.raises(NotFound):
        await domains_service.verify_domain(ws_id, "never-added.com")


# ---------------------------------------------------------------------------
# Auto-join via on_after_register
# ---------------------------------------------------------------------------


async def _verify_acme_domain(ws_id: str) -> str:
    """Helper — add + DNS-verify 'acme.com' on a workspace."""
    entry = await domains_service.add_domain(ws_id, "acme.com")
    with patch(
        "dns.asyncresolver.resolve",
        _resolver_returning([entry.verification_token]),
    ):
        await domains_service.verify_domain(ws_id, "acme.com")
    return entry.verification_token


@pytest.mark.asyncio
async def test_register_auto_joins_verified_domain(env):
    client, _admin_id, ws_id = env
    await _verify_acme_domain(ws_id)

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "newhire@acme.com", "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code in (200, 201), resp.text

    user = await User.find_one(User.email == "newhire@acme.com")
    assert user is not None
    assert any(m.workspace == ws_id and m.role == "member" for m in user.workspaces)
    assert user.active_workspace == ws_id


@pytest.mark.asyncio
async def test_register_other_domain_does_not_auto_join(env):
    client, _admin_id, ws_id = env
    await _verify_acme_domain(ws_id)

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "stranger@outsider.com", "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code in (200, 201), resp.text

    user = await User.find_one(User.email == "stranger@outsider.com")
    assert user is not None
    assert not any(m.workspace == ws_id for m in user.workspaces)


@pytest.mark.asyncio
async def test_auto_join_disabled_does_not_join(env):
    client, _admin_id, ws_id = env
    await _verify_acme_domain(ws_id)
    await domains_service.set_auto_join(ws_id, "acme.com", False)

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "later@acme.com", "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code in (200, 201), resp.text

    user = await User.find_one(User.email == "later@acme.com")
    assert user is not None
    assert not any(m.workspace == ws_id for m in user.workspaces)


@pytest.mark.asyncio
async def test_unverified_domain_does_not_auto_join(env):
    client, _admin_id, ws_id = env
    # Added but never DNS-verified.
    await domains_service.add_domain(ws_id, "acme.com")

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "early@acme.com", "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code in (200, 201), resp.text

    user = await User.find_one(User.email == "early@acme.com")
    assert user is not None
    assert not any(m.workspace == ws_id for m in user.workspaces)


# ---------------------------------------------------------------------------
# Router — admin guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_can_add_list_verify_remove(env):
    client, _admin_id, ws_id = env

    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/domains",
        json={"domain": "acme.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain"] == "acme.com"
    token = body["verification_token"]

    resp = await client.get(f"/api/v1/workspaces/{ws_id}/domains")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    with patch("dns.asyncresolver.resolve", _resolver_returning([token])):
        resp = await client.post(f"/api/v1/workspaces/{ws_id}/domains/acme.com/verify")
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"] is True

    resp = await client.patch(
        f"/api/v1/workspaces/{ws_id}/domains/acme.com",
        json={"auto_join": False},
    )
    assert resp.status_code == 200
    assert resp.json()["auto_join"] is False

    resp = await client.delete(f"/api/v1/workspaces/{ws_id}/domains/acme.com")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_member_cannot_add_domain(member_client):
    client, ws_id = member_client
    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/domains",
        json={"domain": "acme.com"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_cannot_verify_or_delete(member_client, env):
    member, ws_id = member_client
    admin_client, _admin_id, _ws_id = env
    # Admin seeds the domain.
    resp = await admin_client.post(
        f"/api/v1/workspaces/{ws_id}/domains",
        json={"domain": "acme.com"},
    )
    assert resp.status_code == 200

    resp = await member.post(f"/api/v1/workspaces/{ws_id}/domains/acme.com/verify")
    assert resp.status_code == 403
    resp = await member.delete(f"/api/v1/workspaces/{ws_id}/domains/acme.com")
    assert resp.status_code == 403
