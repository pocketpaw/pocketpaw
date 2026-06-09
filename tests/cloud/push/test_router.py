# HTTP-layer tests for the Web Push router (pocketpaw#1391).
# Created: 2026-06-09 (feat/push-subscription-store) — mounts the push router
# on a bare FastAPI app with ``request_context`` overridden to a fixed
# workspace + user, then drives the three endpoints over HTTP. Asserts the
# key endpoint returns ``{"key": ...}`` with the public key ONLY (no private
# material in the response body), the subscribe round-trips + persists, and
# unsubscribe removes the row.
# Updated: 2026-06-09 (review nits) — added coverage for server-side
# User-Agent capture, the ISO-UTC ``created_at`` response field, and the
# cross-workspace endpoint conflict surfacing as HTTP 409.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.push.router import router

_KEY_ENV = "CLOUD_ENCRYPTION_KEY"


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id="w1",
        request_id="req-1",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def enc_key(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def app_client(mongo_db) -> AsyncClient:  # noqa: ARG001 — forces Beanie init
    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[request_context] = _ctx

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def test_vapid_key_endpoint_serves_public_only(app_client, enc_key) -> None:
    resp = await app_client.get("/api/v1/push/vapid-public-key")
    assert resp.status_code == 200
    payload = resp.json()
    # Response is exactly {"key": "<public>"} — no private-key field.
    assert list(payload.keys()) == ["key"]
    assert payload["key"]
    assert "PRIVATE" not in payload["key"]
    # No private-key material anywhere in the serialized body.
    assert "BEGIN PRIVATE KEY" not in resp.text
    assert "private" not in resp.text.lower()


async def test_subscribe_then_unsubscribe_round_trip(app_client) -> None:
    body = {
        "endpoint": "https://push.example/xyz",
        "keys": {"p256dh": "PUB", "auth": "AUTH"},
        "expirationTime": None,
    }
    sub_resp = await app_client.post("/api/v1/push/subscribe", json=body)
    assert sub_resp.status_code == 200
    sub = sub_resp.json()
    assert sub["endpoint"] == "https://push.example/xyz"
    assert "id" in sub
    # Response carries no key material at all.
    assert "keys" not in sub and "p256dh" not in sub_resp.text

    # Idempotent: subscribing the same endpoint again returns the same row.
    sub_resp2 = await app_client.post("/api/v1/push/subscribe", json=body)
    assert sub_resp2.json()["id"] == sub["id"]

    un_resp = await app_client.post(
        "/api/v1/push/unsubscribe", json={"endpoint": "https://push.example/xyz"}
    )
    assert un_resp.status_code == 200
    assert un_resp.json() == {"removed": True}

    # Second unsubscribe is a no-op.
    un_resp2 = await app_client.post(
        "/api/v1/push/unsubscribe", json={"endpoint": "https://push.example/xyz"}
    )
    assert un_resp2.json() == {"removed": False}


async def test_subscribe_captures_user_agent_header(app_client) -> None:
    from pocketpaw_ee.cloud.push import service as push_service

    body = {
        "endpoint": "https://push.example/ua",
        "keys": {"p256dh": "PUB", "auth": "AUTH"},
        "expirationTime": None,
    }
    resp = await app_client.post(
        "/api/v1/push/subscribe",
        json=body,
        headers={"User-Agent": "Mozilla/5.0 (Pixel 8; Chrome/124)"},
    )
    assert resp.status_code == 200
    # The user-agent isn't echoed in the response (no key/PII leakage there),
    # but it IS captured server-side from the header onto the stored row.
    rows = await push_service.list_for_user("w1", "u1")
    assert len(rows) == 1
    assert rows[0].user_agent == "Mozilla/5.0 (Pixel 8; Chrome/124)"


async def test_subscribe_response_exposes_created_at(app_client) -> None:
    body = {
        "endpoint": "https://push.example/ts",
        "keys": {"p256dh": "PUB", "auth": "AUTH"},
        "expirationTime": None,
    }
    resp = await app_client.post("/api/v1/push/subscribe", json=body)
    payload = resp.json()
    # created_at is surfaced (the inherited createdAt) as an ISO-UTC string.
    assert payload.get("created_at")
    assert payload["created_at"].endswith("+00:00")


async def test_subscribe_foreign_workspace_endpoint_returns_409(app_client) -> None:
    from pocketpaw_ee.cloud.push import service as push_service

    # Seed an endpoint owned by a DIFFERENT workspace (w2).
    foreign = {
        "endpoint": "https://push.example/owned-by-w2",
        "keys": {"p256dh": "PUB", "auth": "AUTH"},
        "expirationTime": None,
    }
    await push_service.subscribe("w2", "u9", foreign)

    # The router caller is fixed to w1; subscribing the same endpoint must
    # not take over w2's row — it surfaces a 409 conflict.
    resp = await app_client.post("/api/v1/push/subscribe", json=foreign)
    assert resp.status_code == 409

    # w2 still owns it; nothing reassigned to w1.
    assert await push_service.list_for_user("w1", "u1") == []
    assert len(await push_service.list_for_user("w2", "u9")) == 1
