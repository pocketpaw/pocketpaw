# tests/ee/test_shared_fixtures.py — Smoke tests for the Wave 2 shared fixtures.
# Created: 2026-04-19 (Wave 2.2 / feat/wave-2-pytest-fixtures-v2)
#
# Each test below exercises exactly one of the three callable factories
# defined in ``tests/ee/conftest.py``. They prove the fixture wires a
# mongomock-motor db through a mounted ee/cloud FastAPI and that the
# round-trip flows (register -> login, create workspace, create channel,
# post messages) work end-to-end. A fourth test verifies the session-scoped
# ``mock_s3`` fixture can perform a basic put/get against the in-memory moto
# backend.

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_workspace_factory_creates_and_cleans_up(
    http: AsyncClient,
    user_token_pair,
    workspace_factory,
) -> None:
    """A workspace minted by ``workspace_factory`` should be reachable via the
    GET route and linked as the caller's active workspace.

    Teardown is implicit — the mongomock-motor database is thrown away when
    ``beanie_test_db`` unwinds. We assert the workspace exists during the
    test so a regression in the factory (e.g. failing to persist) shows up
    as an immediate 404 rather than a silent drift on the next wave's tests.
    """

    user = await user_token_pair()
    ws_id = await workspace_factory(user)

    # Reachable by id — returned payload matches the factory's output.
    resp = await http.get(f"/api/v1/workspaces/{ws_id}", headers=user["headers"])
    assert resp.status_code == 200
    assert resp.json()["_id"] == ws_id

    # Activated for the caller — set-active-workspace is implicit in the factory.
    me = await http.get("/api/v1/auth/me", headers=user["headers"])
    assert me.status_code == 200
    assert me.json().get("active_workspace") == ws_id or me.json().get("activeWorkspace") == ws_id


@pytest.mark.asyncio
async def test_user_token_pair_returns_usable_token(
    http: AsyncClient,
    user_token_pair,
) -> None:
    """The bearer token produced by ``user_token_pair`` must unlock the
    authenticated ``/auth/me`` endpoint and echo back the same email/user
    that the factory claims to have registered.
    """

    user = await user_token_pair()

    resp = await http.get("/api/v1/auth/me", headers=user["headers"])
    assert resp.status_code == 200

    profile = resp.json()
    assert profile["email"] == user["email"]
    assert profile["id"] == user["user_id"]


@pytest.mark.asyncio
async def test_seeded_channel_has_expected_messages(
    http: AsyncClient,
    user_token_pair,
    workspace_factory,
    seeded_channel,
) -> None:
    """Seeding five messages should round-trip: GET on the channel's messages
    endpoint must return five items whose ids match the ones the factory
    reported creating (order-insensitive).
    """

    user = await user_token_pair()
    await workspace_factory(user)
    channel_id, message_ids = await seeded_channel(user, count=5)

    assert len(message_ids) == 5

    resp = await http.get(
        f"/api/v1/chat/groups/{channel_id}/messages",
        headers=user["headers"],
    )
    assert resp.status_code == 200

    payload = resp.json()
    items = payload["items"] if isinstance(payload, dict) else payload
    returned_ids = {item["_id"] for item in items}
    assert returned_ids == set(message_ids)


def test_mock_s3_put_and_get_roundtrip(mock_s3) -> None:
    """Sanity-check the session-scoped ``mock_s3`` fixture — put an object,
    get it back, and confirm the payload matches. This only proves moto is
    wired in; the upload adapter tests that actually consume the mock live
    under ``tests/uploads/`` and are out of scope for this slice.
    """

    import uuid

    bucket = f"shared-fixture-{uuid.uuid4().hex[:8]}"
    mock_s3.create_bucket(Bucket=bucket)

    mock_s3.put_object(Bucket=bucket, Key="hello.txt", Body=b"wave-2-fixtures")

    obj = mock_s3.get_object(Bucket=bucket, Key="hello.txt")
    assert obj["Body"].read() == b"wave-2-fixtures"
