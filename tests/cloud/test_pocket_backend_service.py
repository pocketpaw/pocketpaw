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
# Updated: 2026-05-22 (RFC 05 M2b.1) — the executor tuple is now a
# 6-tuple (trailing `approval_route`), the summaries carry
# `approval_route`, and set_pocket_approval_route is covered: it
# validates a mode=user approver as a workspace member and rejects when
# the pocket has no backend.
# Updated: 2026-06-12 (feat/connector-as-pocket-backend) — the set/get
# summaries now carry `backend_type` + `connector_name`, and the executor
# tuple is an 8-tuple (trailing backend_type/connector_name). Added
# CONNECTOR-backend coverage: a connector backend stores backend_type/
# connector_name with no base_url and validates the connector is enabled
# (rejects unknown connector / missing name / bad backend_type); the executor
# tuple carries them with no token; switching http->connector clears the stale
# credential; and a legacy row (no backend_type) reads back as http.
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
    # feat/connector-as-pocket-backend: the set/get summary now carries
    # `backend_type` ("http" here) + `connector_name` (None for http).
    assert result == {
        "backend_type": "http",
        "connector_name": None,
        "base_url": "https://api.example.com",
        "auth_type": "bearer",
        "configured": True,
    }
    # The summary never carries the token.
    assert "auth_token" not in result
    assert "token" not in result

    summary = await pockets_service.get_pocket_backend("w1", "pocket-1")
    # RFC 05 M2a: the summary carries the write allowlist (empty by
    # default — fail-closed). RFC 05 M2b.1: it also carries the approval
    # route (None by default — the owner approves). connector-as-backend:
    # it also carries backend_type/connector_name. The token is still
    # never present.
    assert summary == {
        "backend_type": "http",
        "connector_name": None,
        "base_url": "https://api.example.com",
        "auth_type": "bearer",
        "configured": True,
        "allowed_writes": [],
        "approval_route": None,
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
    # connector-as-backend: the executor tuple is now an 8-tuple — trailing
    # `backend_type` / `connector_name` after the write allowlist + route.
    (
        base_url,
        auth_type,
        auth_header,
        token,
        allowed_writes,
        approval_route,
        backend_type,
        connector_name,
    ) = creds
    assert base_url == "https://api.example.com"
    assert auth_type == "api_key"
    assert auth_header == "X-Custom-Key"
    assert token == "my-api-key"
    assert allowed_writes == []
    # RFC 05 M2b.1: no route set → None (the pocket owner approves).
    assert approval_route is None
    assert backend_type == "http"
    assert connector_name is None


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
    # connector-as-backend: the executor tuple is now an 8-tuple — trailing
    # elements are write allowlist, approval route, backend_type,
    # connector_name.
    _, auth_type, _, token, allowed_writes, approval_route, backend_type, connector_name = creds
    assert auth_type == "none"
    assert token == ""
    assert allowed_writes == []
    assert approval_route is None
    assert backend_type == "http"
    assert connector_name is None


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


# ---------------------------------------------------------------------------
# Connector backend (feat/connector-as-pocket-backend)
# ---------------------------------------------------------------------------


async def _enable_connector(workspace_id: str, name: str) -> None:
    """Enable a real registry connector for a workspace (workspace scope)."""
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.connectors.dto import EnableConnectorRequest

    await connectors_service.enable_connector(
        workspace_id, name, EnableConnectorRequest(scope="workspace")
    )


async def test_set_connector_backend_summary(mongo_db):
    """A connector backend stores backend_type/connector_name, needs no
    base_url, and the summary carries both (never a token)."""
    await _enable_connector("w1", "github")

    result = await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="",  # not required for a connector backend
        auth_type="none",
        auth_token="",
        backend_type="connector",
        connector_name="github",
    )
    assert result == {
        "backend_type": "connector",
        "connector_name": "github",
        "base_url": "",
        "auth_type": "none",
        "configured": True,
    }

    summary = await pockets_service.get_pocket_backend("w1", "pocket-1")
    assert summary == {
        "backend_type": "connector",
        "connector_name": "github",
        "base_url": "",
        "auth_type": "none",
        "configured": True,
        "allowed_writes": [],
        "approval_route": None,
    }
    assert "token" not in summary
    assert "encrypted_token" not in summary


async def test_set_connector_backend_rejects_unknown_connector(mongo_db):
    """A connector_name that names no enabled workspace connector is rejected
    with a clear error — never silently stored."""
    with pytest.raises(ValidationError) as excinfo:
        await pockets_service.set_pocket_backend(
            workspace_id="w1",
            user_id="u1",
            pocket_id="pocket-1",
            base_url="",
            auth_type="none",
            auth_token="",
            backend_type="connector",
            connector_name="not-enabled-anywhere",
        )
    assert excinfo.value.code == "pocket_backend.unknown_connector"


async def test_set_connector_backend_requires_connector_name(mongo_db):
    """backend_type='connector' without a connector_name is rejected."""
    with pytest.raises(ValidationError) as excinfo:
        await pockets_service.set_pocket_backend(
            workspace_id="w1",
            user_id="u1",
            pocket_id="pocket-1",
            base_url="",
            auth_type="none",
            auth_token="",
            backend_type="connector",
            connector_name=None,
        )
    assert excinfo.value.code == "pocket_backend.missing_connector"


async def test_set_backend_rejects_unknown_backend_type(mongo_db):
    with pytest.raises(ValidationError) as excinfo:
        await pockets_service.set_pocket_backend(
            workspace_id="w1",
            user_id="u1",
            pocket_id="pocket-1",
            base_url="",
            auth_type="none",
            auth_token="",
            backend_type="weird",
            connector_name=None,
        )
    assert excinfo.value.code == "pocket_backend.invalid_backend_type"


async def test_connector_backend_for_executor_tuple(mongo_db):
    """The executor tuple carries backend_type='connector' + connector_name and
    no token (a connector backend has no credential)."""
    await _enable_connector("w1", "github")
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="",
        auth_type="none",
        auth_token="",
        backend_type="connector",
        connector_name="github",
    )
    creds = await pockets_service.get_pocket_backend_for_executor("w1", "pocket-1")
    assert creds is not None
    base_url, auth_type, _hdr, token, _aw, _route, backend_type, connector_name = creds
    assert backend_type == "connector"
    assert connector_name == "github"
    assert base_url == ""
    assert auth_type == "none"
    assert token == ""


async def test_switch_http_to_connector_clears_credential(mongo_db):
    """Switching an existing http backend to a connector backend clears the
    stale base_url / encrypted token so no credential lingers."""
    from pocketpaw_ee.cloud.models.pocket_backend import PocketBackendCredential

    # First an http backend with a real encrypted token.
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="bearer",
        auth_token="secret-token",
    )
    # Then switch it to a connector backend.
    await _enable_connector("w1", "github")
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="",
        auth_type="none",
        auth_token="",
        backend_type="connector",
        connector_name="github",
    )
    row = await PocketBackendCredential.find_one(
        PocketBackendCredential.pocket_id == "pocket-1",
        PocketBackendCredential.workspace_id == "w1",
    )
    assert row.backend_type == "connector"
    assert row.connector_name == "github"
    assert row.base_url == ""
    assert row.auth_type == "none"
    assert row.encrypted_token is None
    assert row.nonce is None
    assert row.salt is None


async def test_legacy_row_reads_as_http_backend(mongo_db):
    """Back-compat: a row written WITHOUT backend_type/connector_name (a legacy
    http backend) reads as backend_type='http' / connector_name=None — both via
    the model default and the service summaries."""
    from pocketpaw_ee.cloud.models.pocket_backend import PocketBackendCredential

    # Insert a row the way pre-feature code did — no backend_type / connector_name
    # passed. The model default fills backend_type='http'.
    await PocketBackendCredential(
        pocket_id="legacy-pocket",
        workspace_id="w1",
        base_url="https://legacy.example.com",
        auth_type="none",
    ).insert()

    summary = await pockets_service.get_pocket_backend("w1", "legacy-pocket")
    assert summary["backend_type"] == "http"
    assert summary["connector_name"] is None
    assert summary["base_url"] == "https://legacy.example.com"

    creds = await pockets_service.get_pocket_backend_for_executor("w1", "legacy-pocket")
    assert creds is not None
    assert creds[6] == "http"  # backend_type
    assert creds[7] is None  # connector_name


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


# ---------------------------------------------------------------------------
# set_pocket_approval_route — RFC 05 M2b.1
# ---------------------------------------------------------------------------


async def test_set_approval_route_user_mode_validates_membership(mongo_db, monkeypatch):
    """A mode=user route is stored only when the user_id is a current
    workspace member; the executor tuple then carries the route."""
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )

    from pocketpaw_ee.cloud.workspace import service as workspace_service

    async def _members(_ws):
        return ["u1", "approver-7"]

    monkeypatch.setattr(workspace_service, "list_member_ids", _members)

    result = await pockets_service.set_pocket_approval_route(
        "w1", "u1", "pocket-1", {"mode": "user", "user_id": "approver-7"}
    )
    assert result["approval_route"] == {"mode": "user", "user_id": "approver-7"}

    creds = await pockets_service.get_pocket_backend_for_executor("w1", "pocket-1")
    assert creds[5] == {"mode": "user", "user_id": "approver-7"}


async def test_set_approval_route_rejects_non_member_approver(mongo_db, monkeypatch):
    """A mode=user route naming a non-member is rejected."""
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )

    from pocketpaw_ee.cloud.workspace import service as workspace_service

    async def _members(_ws):
        return ["u1"]  # approver-9 is NOT a member

    monkeypatch.setattr(workspace_service, "list_member_ids", _members)

    with pytest.raises(ValidationError):
        await pockets_service.set_pocket_approval_route(
            "w1", "u1", "pocket-1", {"mode": "user", "user_id": "approver-9"}
        )


async def test_set_approval_route_owner_mode_stores_none(mongo_db):
    """An explicit mode=owner route stores None — the default."""
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id="pocket-1",
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    result = await pockets_service.set_pocket_approval_route(
        "w1", "u1", "pocket-1", {"mode": "owner", "user_id": None}
    )
    assert result["approval_route"] is None


async def test_set_approval_route_rejects_when_no_backend(mongo_db):
    """A route with no backend to gate is meaningless — rejected."""
    with pytest.raises(ValidationError):
        await pockets_service.set_pocket_approval_route(
            "w1", "u1", "missing-pocket", {"mode": "user", "user_id": "x"}
        )
