# tests/ee/conftest.py — Shared pytest fixtures for ee/cloud integration tests.
# Created: 2026-04-19 (Wave 2.2 / feat/wave-2-pytest-fixtures-v2)
#
# Minimal slice of the Wave 2 hardening plan. Three callable factories
# (``user_token_pair``, ``workspace_factory``, ``seeded_channel``) plus the
# supporting scaffolding (mongomock-motor db, mounted FastAPI, httpx client,
# moto s3). Composes on the existing ``mongomock-motor`` primitive already in
# use under ``tests/cloud/chat/conftest.py`` — no new Mongo infrastructure.
#
# Deferred to later waves:
#   * ``fleet_installed_org`` — Wave 3 Cluster D
#   * ``drive_connected_pocket`` — Wave 3 Cluster C
#   * The full seed set from Appendix B — Wave 2.1

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _require_enterprise() -> None:
    """Skip the calling fixture if the ee/cloud dependencies are missing.

    The fixtures in this module mount the real ee/cloud router tree, which
    pulls in ``beanie``, ``motor``, ``fastapi-users``, etc. Those ship with
    the ``pocketpaw-ee`` package (ee/), which is installed by the ``ee``
    dependency group (``uv sync --dev --group ee``). When they're missing we
    emit a ``skip`` rather than an ``ImportError`` so the rest of the
    ``tests/ee/`` suite keeps collecting.
    """

    for module in ("beanie", "motor", "mongomock_motor"):
        if importlib.util.find_spec(module) is None:
            pytest.skip(
                f"Shared ee fixtures require the '{module}' module "
                "(install with: uv sync --dev --group ee).",
                allow_module_level=False,
            )


def _require_moto() -> None:
    """Skip if moto isn't on the path. moto is declared under the ``dev``
    dependency group, so plain ``uv sync --dev`` should satisfy it; this
    guard exists for environments that bootstrap a subset of the dev deps.
    """

    if importlib.util.find_spec("moto") is None:
        pytest.skip(
            "mock_s3 fixture requires the 'moto' module (install with: uv sync --dev).",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# License env + session-scoped AWS creds
# ---------------------------------------------------------------------------


def _make_license_key(secret: str = "test-secret") -> str:
    """Mint a valid HMAC-signed license key for the license middleware."""

    from datetime import datetime, timedelta

    payload = {
        "org": "test-org",
        "plan": "enterprise",
        "seats": 100,
        "exp": (datetime.now(tz=None) + timedelta(days=365)).strftime("%Y-%m-%d"),
    }
    payload_str = json.dumps(payload)
    sig = hashlib.sha256(f"{secret}:{payload_str}".encode()).hexdigest()
    raw = f"{payload_str}.{sig}"
    return base64.b64encode(raw.encode()).decode()


@pytest.fixture(scope="session")
def _license_env_vars() -> dict[str, str]:
    """Build the env var dict once per session — values never change."""

    secret = "test-secret"
    return {
        "POCKETPAW_LICENSE_KEY": _make_license_key(secret),
        "POCKETPAW_LICENSE_SECRET": secret,
        "AUTH_SECRET": "test-auth-secret-for-ee-shared-fixtures",
        # moto/boto3 reads these — dummy values are fine, the mock intercepts.
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }


@pytest.fixture()
def license_env(_license_env_vars: dict[str, str]):
    """Inject license + AWS creds into the process env for the duration of
    the test. Also flushes the license module's lru-style cache so the new
    key takes effect on the next request.
    """

    with patch.dict(os.environ, _license_env_vars):
        import pocketpaw_ee.cloud.license as lic_mod

        lic_mod._cached_license = None
        lic_mod._license_error = None
        yield _license_env_vars
        lic_mod._cached_license = None
        lic_mod._license_error = None


# ---------------------------------------------------------------------------
# Mongo isolation — composes on the existing ``mongomock-motor`` primitive
# used by ``tests/cloud/chat/conftest.py`` / ``tests/cloud/memory/conftest.py``.
#
# Each test gets its own uniquely-named in-memory database, so teardown is
# implicit (the client goes out of scope when the fixture unwinds). No real
# MongoDB service is required.
# ---------------------------------------------------------------------------


@pytest.fixture()
async def beanie_test_db():
    """Initialise Beanie against a fresh mongomock-motor database.

    Beanie >=1.26 calls ``database.list_collection_names`` with
    ``authorizedCollections`` / ``nameOnly``; mongomock-motor's stub rejects
    unknown kwargs. Wrap the method to drop them, mirroring the shim in
    ``tests/cloud/chat/conftest.py``.
    """

    _require_enterprise()

    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    db_name = f"test_ee_shared_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe_list_collection_names(*_args: Any, **_kwargs: Any) -> list[str]:
        return await original()

    db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    yield db


# ---------------------------------------------------------------------------
# FastAPI app + httpx client
# ---------------------------------------------------------------------------


@pytest.fixture()
async def app(license_env, beanie_test_db) -> FastAPI:
    """Mount the ee/cloud router tree onto a fresh FastAPI app.

    The agent pool start/stop coroutines are replaced with no-op mocks so the
    app can boot without a running backend. ``beanie_test_db`` runs first so
    every route hits an initialised in-memory database.
    """

    _require_enterprise()

    from pocketpaw_ee.cloud import mount_cloud

    test_app = FastAPI()

    mock_pool = MagicMock()
    mock_pool.start = AsyncMock()
    mock_pool.stop = AsyncMock()

    with patch("pocketpaw.agents.pool.get_agent_pool", return_value=mock_pool):
        mount_cloud(test_app)

    return test_app


@pytest.fixture()
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """httpx.AsyncClient bound to the in-process ASGI app."""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Shared factory fixtures
# ---------------------------------------------------------------------------


UserTokenPair = Callable[..., Awaitable[dict[str, Any]]]
WorkspaceFactory = Callable[..., Awaitable[str]]
SeededChannel = Callable[..., Awaitable[tuple[str, list[str]]]]


@pytest.fixture()
def user_token_pair(http: AsyncClient) -> UserTokenPair:
    """Callable that registers + logs in a fresh user.

    Returns a dict with keys: ``user_id``, ``email``, ``token``, ``headers``.
    Tests can call it multiple times to get independent users — each invocation
    produces a unique email, so no cleanup is required (the per-test
    mongomock database is thrown away at teardown).
    """

    async def _factory(email: str | None = None, password: str = "Password1!") -> dict[str, Any]:
        email = email or f"ee-fixture-{uuid.uuid4().hex[:8]}@test.example"

        reg = await http.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": password,
                "full_name": "EE Fixture User",
            },
        )
        assert reg.status_code == 201, f"register failed: {reg.status_code} {reg.text}"
        user = reg.json()

        login = await http.post(
            "/api/v1/auth/bearer/login",
            data={"username": email, "password": password},
        )
        assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
        token = login.json()["access_token"]

        return {
            "user_id": user["id"],
            "email": email,
            "token": token,
            "headers": {"Authorization": f"Bearer {token}"},
        }

    return _factory


@pytest.fixture()
def workspace_factory(http: AsyncClient) -> WorkspaceFactory:
    """Callable that creates a workspace owned by the given user and sets it
    as their active workspace. Returns the workspace id.

    Teardown is implicit — the mongomock database is dropped when
    ``beanie_test_db`` unwinds at the end of the test.
    """

    async def _factory(
        user: dict[str, Any],
        *,
        name: str = "Shared Fixture Workspace",
        slug: str | None = None,
        activate: bool = True,
    ) -> str:
        slug = slug or f"ws-{uuid.uuid4().hex[:8]}"

        resp = await http.post(
            "/api/v1/workspaces",
            json={"name": name, "slug": slug},
            headers=user["headers"],
        )
        assert resp.status_code == 200, f"create workspace failed: {resp.status_code} {resp.text}"
        workspace_id: str = resp.json()["_id"]

        if activate:
            act = await http.post(
                "/api/v1/auth/set-active-workspace",
                json={"workspace_id": workspace_id},
                headers=user["headers"],
            )
            assert act.status_code == 200, (
                f"activate workspace failed: {act.status_code} {act.text}"
            )

        return workspace_id

    return _factory


@pytest.fixture()
def seeded_channel(http: AsyncClient) -> SeededChannel:
    """Callable that creates a chat channel (group) in the caller's active
    workspace and seeds it with ``count`` text messages.

    Channels and groups are the same concept in ee/cloud — the route tree
    exposes them under ``/api/v1/chat/groups``. Returns
    ``(channel_id, [message_ids])`` so tests can round-trip against either.
    """

    async def _factory(
        user: dict[str, Any],
        *,
        name: str | None = None,
        count: int = 3,
    ) -> tuple[str, list[str]]:
        name = name or f"channel-{uuid.uuid4().hex[:6]}"

        create = await http.post(
            "/api/v1/chat/groups",
            json={"name": name},
            headers=user["headers"],
        )
        assert create.status_code == 200, (
            f"create channel failed: {create.status_code} {create.text}"
        )
        channel_id: str = create.json()["_id"]

        message_ids: list[str] = []
        for i in range(count):
            resp = await http.post(
                f"/api/v1/chat/groups/{channel_id}/messages",
                json={"content": f"seed message {i}"},
                headers=user["headers"],
            )
            assert resp.status_code == 200, (
                f"send message {i} failed: {resp.status_code} {resp.text}"
            )
            message_ids.append(resp.json()["_id"])

        return channel_id, message_ids

    return _factory


# ---------------------------------------------------------------------------
# S3 mock — session-scoped moto. Keeps the AWS SDK wire off-network for every
# test that touches uploads, without the cost of re-initialising the moto
# backend on every function. Tests should use uniquely-named buckets.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mock_s3(_license_env_vars: dict[str, str]):
    """Session-scoped AWS mock via moto 5's unified ``mock_aws`` decorator.

    Yields a ready-to-use boto3 S3 client bound to the in-memory backend.
    AWS credentials are injected via ``_license_env_vars`` so the boto3
    client can authenticate even when the host has no real creds.
    """

    _require_moto()

    import boto3
    from moto import mock_aws

    with patch.dict(os.environ, _license_env_vars), mock_aws():
        client = boto3.client("s3", region_name=_license_env_vars["AWS_DEFAULT_REGION"])
        yield client
