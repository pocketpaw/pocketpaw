# Tests for the Web Push service (pocketpaw#1391).
# Created: 2026-06-09 (feat/push-subscription-store) — exercises the
# subscribe upsert + dedupe path, unsubscribe removal, per-workspace VAPID
# keypair generation (public-key-only exposure, stable across reads, private
# key encrypted at rest + never leaked), and tenant scoping. Uses the shared
# ``mongo_db`` fixture (mongomock-motor) so the service runs real Beanie
# writes against an isolated in-memory DB; ``enc_key`` supplies a Fernet key
# for the VAPID private-key encryption path.
# Updated: 2026-06-09 (review nits) — added coverage for server-captured
# ``user_agent`` persistence and the workspace-scoped subscribe behavior
# (same-workspace re-subscribe upserts; a foreign-workspace endpoint raises
# ConflictError instead of silently reassigning the row across tenants).

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud._core.errors import ConflictError
from pocketpaw_ee.cloud.models.vapid_keypair import VapidKeypair
from pocketpaw_ee.cloud.push import service as push_service
from pocketpaw_ee.cloud.push.dto import UnsubscribeRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

_KEY_ENV = "CLOUD_ENCRYPTION_KEY"


@pytest.fixture
def enc_key(monkeypatch):
    """Set a valid Fernet key so VAPID private-key encryption works."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(_KEY_ENV, key)
    return key


def _sub_body(endpoint: str = "https://push.example/abc") -> dict:
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": "PUB256", "auth": "AUTHSECRET"},
        "expirationTime": None,
    }


# ---------------------------------------------------------------------------
# Subscribe — persist + idempotent upsert on endpoint
# ---------------------------------------------------------------------------


async def test_subscribe_persists() -> None:
    sub = await push_service.subscribe("w1", "u1", _sub_body())
    assert sub.endpoint == "https://push.example/abc"
    assert sub.workspace_id == "w1"
    assert sub.user_id == "u1"
    assert sub.keys.p256dh == "PUB256"

    rows = await push_service.list_for_user("w1", "u1")
    assert len(rows) == 1
    assert rows[0].endpoint == sub.endpoint


async def test_subscribe_idempotent_on_endpoint() -> None:
    await push_service.subscribe("w1", "u1", _sub_body())
    # Same endpoint, new key material → updates, does not duplicate.
    body2 = _sub_body()
    body2["keys"]["p256dh"] = "ROTATED"
    sub2 = await push_service.subscribe("w1", "u1", body2)

    rows = await push_service.list_for_user("w1", "u1")
    assert len(rows) == 1  # deduped on endpoint
    assert rows[0].keys.p256dh == "ROTATED"
    assert rows[0].id == sub2.id


async def test_subscribe_captures_user_agent() -> None:
    sub = await push_service.subscribe(
        "w1", "u1", _sub_body(), user_agent="Mozilla/5.0 (Test Browser)"
    )
    assert sub.user_agent == "Mozilla/5.0 (Test Browser)"

    rows = await push_service.list_for_user("w1", "u1")
    assert rows[0].user_agent == "Mozilla/5.0 (Test Browser)"


async def test_subscribe_upsert_updates_user_agent() -> None:
    await push_service.subscribe("w1", "u1", _sub_body(), user_agent="old-agent")
    # Re-subscribing the same endpoint refreshes the stored user-agent.
    sub2 = await push_service.subscribe("w1", "u1", _sub_body(), user_agent="new-agent")
    rows = await push_service.list_for_user("w1", "u1")
    assert len(rows) == 1
    assert rows[0].user_agent == "new-agent"
    assert rows[0].id == sub2.id


async def test_subscribe_default_user_agent_is_empty() -> None:
    sub = await push_service.subscribe("w1", "u1", _sub_body())
    assert sub.user_agent == ""


async def test_subscribe_distinct_endpoints_create_rows() -> None:
    await push_service.subscribe("w1", "u1", _sub_body("https://push.example/one"))
    await push_service.subscribe("w1", "u1", _sub_body("https://push.example/two"))
    rows = await push_service.list_for_user("w1", "u1")
    assert {r.endpoint for r in rows} == {
        "https://push.example/one",
        "https://push.example/two",
    }


async def test_subscribe_re_subscribe_moves_endpoint_to_new_user_same_workspace() -> None:
    # A shared device endpoint re-subscribed by a different user IN THE SAME
    # workspace updates ownership rather than duplicating (the upsert matches
    # on (endpoint, workspace) and the endpoint is unique).
    await push_service.subscribe("w1", "u1", _sub_body())
    await push_service.subscribe("w1", "u2", _sub_body())
    assert await push_service.list_for_user("w1", "u1") == []
    u2_rows = await push_service.list_for_user("w1", "u2")
    assert len(u2_rows) == 1


async def test_subscribe_foreign_workspace_endpoint_raises_conflict() -> None:
    # An endpoint already owned by workspace w1 cannot be silently reassigned
    # to workspace w2 by an authed caller in w2 — the unique index catches the
    # insert and the service surfaces a ConflictError instead of a takeover.
    await push_service.subscribe("w1", "u1", _sub_body())

    with pytest.raises(ConflictError):
        await push_service.subscribe("w2", "u2", _sub_body())

    # w1 still owns the row; nothing leaked to w2.
    w1_rows = await push_service.list_for_user("w1", "u1")
    assert len(w1_rows) == 1
    assert w1_rows[0].workspace_id == "w1"
    assert await push_service.list_for_user("w2", "u2") == []


async def test_subscribe_exposes_created_at() -> None:
    sub = await push_service.subscribe("w1", "u1", _sub_body())
    # The inherited TimestampedDocument.createdAt is surfaced on the domain.
    assert sub.created_at is not None


# ---------------------------------------------------------------------------
# Unsubscribe — remove by endpoint, workspace-scoped
# ---------------------------------------------------------------------------


async def test_unsubscribe_removes_row() -> None:
    await push_service.subscribe("w1", "u1", _sub_body())
    removed = await push_service.unsubscribe(
        "w1", "u1", UnsubscribeRequest(endpoint="https://push.example/abc")
    )
    assert removed is True
    assert await push_service.list_for_user("w1", "u1") == []


async def test_unsubscribe_missing_returns_false() -> None:
    removed = await push_service.unsubscribe("w1", "u1", {"endpoint": "https://push.example/nope"})
    assert removed is False


async def test_unsubscribe_is_workspace_scoped() -> None:
    await push_service.subscribe("w1", "u1", _sub_body())
    # Another workspace can't delete w1's row even with the right endpoint.
    removed = await push_service.unsubscribe("w2", "u1", {"endpoint": "https://push.example/abc"})
    assert removed is False
    assert len(await push_service.list_for_user("w1", "u1")) == 1


# ---------------------------------------------------------------------------
# VAPID keypair — per-workspace, public-only exposure
# ---------------------------------------------------------------------------


async def test_vapid_public_key_generated_once_and_stable(enc_key) -> None:
    key1 = await push_service.get_vapid_public_key("w1")
    key2 = await push_service.get_vapid_public_key("w1")
    assert key1 == key2  # generate-once, reused
    assert key1  # non-empty base64url public key

    # Exactly one keypair row for the workspace.
    rows = [d async for d in VapidKeypair.find({"workspace": "w1"})]
    assert len(rows) == 1


async def test_vapid_keypair_is_per_tenant(enc_key) -> None:
    key_w1 = await push_service.get_vapid_public_key("w1")
    key_w2 = await push_service.get_vapid_public_key("w2")
    assert key_w1 != key_w2  # distinct keypair per workspace


async def test_vapid_private_key_encrypted_at_rest_and_never_public(enc_key) -> None:
    public = await push_service.get_vapid_public_key("w1")

    doc = await VapidKeypair.find_one({"workspace": "w1"})
    assert doc is not None
    # Stored private value is Fernet ciphertext, not plaintext PEM.
    assert "BEGIN PRIVATE KEY" not in doc.private_pem_encrypted
    assert doc.private_pem_encrypted.startswith("gAAAA")  # Fernet token marker

    # The decrypted PEM is recoverable server-side only.
    pem = await push_service.get_decrypted_private_pem("w1")
    assert "BEGIN PRIVATE KEY" in pem

    # The public key returned to callers carries no private material.
    assert "PRIVATE" not in public
    assert public != doc.private_pem_encrypted
