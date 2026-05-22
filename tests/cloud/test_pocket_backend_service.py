# tests/cloud/test_pocket_backend_service.py — RFC 04 alpha.
# Created: 2026-05-21 — Service-layer coverage for the per-pocket backend
# binding: set / get / get-for-executor / remove. Exercises the real
# Beanie path against the in-memory mongomock-motor DB (mongo_db fixture).
#
# Updated: 2026-05-21 (PR #1177 security pass) — added coverage that
# remove_pocket_backend writes an audit-log entry.
# Updated: 2026-05-22 (RFC 05 M2a) — get_pocket_backend now carries an
# `allowed_writes` list and get_pocket_backend_for_executor returns a
# 5-tuple (the trailing element is the write allowlist). Assertions
# updated to the new contract.
#
# What this pins:
#   - set_pocket_backend then get_pocket_backend returns configured:true
#     and the right base_url/auth_type — never the token.
#   - get_pocket_backend returns None when no row exists.
#   - get_pocket_backend_for_executor decrypts the token round-trip.
#   - set_pocket_backend upserts (a second call updates, not duplicates).
#   - set_pocket_backend rejects a non-https / internal base URL.
#   - remove_pocket_backend deletes the row, is idempotent, and audit-logs.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.pockets import service as pockets_service


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "service-test-auth-secret")


async def test_set_then_get_backend(mongo_db):
    result = await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="bearer",
        auth_token="secret-token-xyz",
    )
    assert result == {
        "base_url": "https://api.example.com",
        "auth_type": "bearer",
        "configured": True,
    }
    # The summary never carries the token.
    assert "auth_token" not in result
    assert "token" not in result

    summary = await pockets_service.get_pocket_backend("w1", "pocket-1")
    # RFC 05 M2a: the summary now also carries the write allowlist —
    # empty by default (fail-closed). The token is still never present.
    assert summary == {
        "base_url": "https://api.example.com",
        "auth_type": "bearer",
        "configured": True,
        "allowed_writes": [],
    }
    assert "token" not in summary
    assert "encrypted_token" not in summary


async def test_get_backend_returns_none_when_unset(mongo_db):
    assert await pockets_service.get_pocket_backend("w1", "no-such-pocket") is None


async def test_get_backend_is_workspace_scoped(mongo_db):
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    # A different workspace cannot see the row.
    assert await pockets_service.get_pocket_backend("w2", "pocket-1") is None


async def test_get_for_executor_decrypts_token(mongo_db):
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="api_key",
        auth_token="my-api-key",
        auth_header="X-Custom-Key",
    )
    creds = await pockets_service.get_pocket_backend_for_executor("w1", "pocket-1")
    assert creds is not None
    base_url, auth_type, auth_header, token, allowed_writes = creds
    assert base_url == "https://api.example.com"
    assert auth_type == "api_key"
    assert auth_header == "X-Custom-Key"
    assert token == "my-api-key"
    assert allowed_writes == []


async def test_get_for_executor_none_when_unset(mongo_db):
    assert await pockets_service.get_pocket_backend_for_executor("w1", "missing") is None


async def test_get_for_executor_no_token_for_none_auth(mongo_db):
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    creds = await pockets_service.get_pocket_backend_for_executor("w1", "pocket-1")
    assert creds is not None
    # RFC 05 M2a: the executor tuple gained a trailing `allowed_writes`.
    _, auth_type, _, token, allowed_writes = creds
    assert auth_type == "none"
    assert token == ""
    assert allowed_writes == []


async def test_set_backend_upserts(mongo_db):
    from pocketpaw_ee.cloud.models.pocket_backend import PocketBackendCredential

    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://old.example.com",
        auth_type="bearer",
        auth_token="old-token",
    )
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://new.example.com",
        auth_type="bearer",
        auth_token="new-token",
    )
    rows = await PocketBackendCredential.find(
        PocketBackendCredential.pocket_id == "pocket-1"
    ).to_list()
    assert len(rows) == 1  # upsert, not duplicate

    creds = await pockets_service.get_pocket_backend_for_executor("w1", "pocket-1")
    assert creds[0] == "https://new.example.com"
    assert creds[3] == "new-token"


async def test_set_backend_rejects_http_url(mongo_db):
    with pytest.raises(ValidationError):
        await pockets_service.set_pocket_backend(
            workspace_id="w1",
            user_id="u1",
            pocket_id="pocket-1",
            base_url="http://api.example.com",
            auth_type="none",
            auth_token="",
        )


async def test_set_backend_rejects_internal_url(mongo_db):
    with pytest.raises(ValidationError):
        await pockets_service.set_pocket_backend(
            workspace_id="w1",
            user_id="u1",
            pocket_id="pocket-1",
            base_url="https://169.254.169.254",
            auth_type="none",
            auth_token="",
        )


async def test_set_backend_requires_token_for_auth(mongo_db):
    with pytest.raises(ValidationError):
        await pockets_service.set_pocket_backend(
            workspace_id="w1",
            user_id="u1",
            pocket_id="pocket-1",
            base_url="https://api.example.com",
            auth_type="bearer",
            auth_token="",
        )


async def test_remove_backend(mongo_db):
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    assert await pockets_service.get_pocket_backend("w1", "pocket-1") is not None

    await pockets_service.remove_pocket_backend("w1", "u1", "pocket-1")
    assert await pockets_service.get_pocket_backend("w1", "pocket-1") is None

    # Idempotent — removing again does not raise.
    await pockets_service.remove_pocket_backend("w1", "u1", "pocket-1")


async def test_remove_backend_audit_logs(mongo_db, monkeypatch):
    """remove_pocket_backend writes an audit entry for the revocation."""
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="bearer",
        auth_token="secret-token",
    )

    logged: list = []

    class _FakeLogger:
        def log(self, event):
            logged.append(event)

    import pocketpaw.security.audit as audit_mod

    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: _FakeLogger())

    await pockets_service.remove_pocket_backend("w1", "u1", "pocket-1")

    assert len(logged) == 1
    event = logged[0]
    assert event.actor == "u1"
    assert event.action == "pocket.backend.remove"
    assert event.target == "pocket-1"
    # The token is never part of the audit entry.
    assert "secret-token" not in str(event.context)
