"""Wave 3 Task 15 — CSV export + SIEM webhook delivery."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ruff: noqa: I001, E402
# Why: importing ``models.user`` BEFORE the other cloud imports primes the
# calendar→cloud.shared.deps chain so ``pocketpaw_ee.cloud.auth`` finishes
# initialising in the right order. Ruff's import sort would scramble this
# and break test collection — keep the manual order. (Same trick as
# tests/cloud/workspace/test_remove_member_cascade.py.)
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.user import User as _UserDoc  # must come first
from pocketpaw_ee.cloud.models.user import WorkspaceMembership as _Membership
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.audit import webhooks as audit_webhooks
from pocketpaw_ee.cloud.audit.router import workspace_router
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.audit_event import AuditEvent as _AuditEventDoc
from pocketpaw_ee.cloud.models.audit_webhook import AuditWebhook as _AuditWebhookDoc

pytestmark = pytest.mark.usefixtures("mongo_db")


WS = "ws-task15"


async def _seed_admin() -> _UserDoc:
    user = _UserDoc(
        email="admin@x.c",
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="A",
        workspaces=[_Membership(workspace=WS, role="owner", joined_at=datetime.now(UTC))],
    )
    user.active_workspace = WS
    await user.insert()
    return user


@pytest_asyncio.fixture
async def admin_client() -> AsyncClient:
    from datetime import UTC as _UTC

    from pocketpaw_ee.cloud._core.context import (
        RequestContext,
        ScopeKind,
        request_context,
    )

    user = await _seed_admin()
    app = FastAPI()
    add_error_handler(app)
    app.include_router(workspace_router, prefix="/api/v1")

    async def _override_user() -> Any:
        return await _UserDoc.get(user.id)

    async def _override_ctx() -> RequestContext:
        return RequestContext(
            user_id=str(user.id),
            workspace_id=WS,
            request_id="r",
            scope=ScopeKind.NONE,
            started_at=datetime.now(_UTC),
        )

    app.dependency_overrides[current_active_user] = _override_user
    app.dependency_overrides[request_context] = _override_ctx
    app.dependency_overrides[require_license] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _seed_events(n: int, *, base: datetime | None = None) -> list[_AuditEventDoc]:
    base = base or datetime.now(UTC)
    docs: list[_AuditEventDoc] = []
    for i in range(n):
        doc = _AuditEventDoc(
            workspace=WS,
            actor_id=f"u{i}",
            action="workspace.updated",
            target_type="workspace",
            target_id=WS,
            metadata={"i": i, "note": "ok"},
            at=base + timedelta(seconds=i),
        )
        await doc.insert()
        docs.append(doc)
    return docs


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


async def test_export_returns_header_and_rows(admin_client: AsyncClient) -> None:
    await _seed_events(50)
    resp = await admin_client.get(f"/api/v1/workspaces/{WS}/audit/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == [
        "at",
        "actor_id",
        "action",
        "target_type",
        "target_id",
        "ip",
        "user_agent",
        "metadata",
    ]
    assert len(rows) == 51  # header + 50


async def test_export_filters_by_since_until(admin_client: AsyncClient) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await _seed_events(10, base=base)
    since = (base + timedelta(seconds=3)).isoformat()
    until = (base + timedelta(seconds=6)).isoformat()
    resp = await admin_client.get(
        f"/api/v1/workspaces/{WS}/audit/export",
        params={"since": since, "until": until},
    )
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.text)))
    # 4 rows in range (sec 3,4,5,6) + header.
    assert len(rows) == 5


async def test_export_escapes_special_chars(admin_client: AsyncClient) -> None:
    doc = _AuditEventDoc(
        workspace=WS,
        actor_id="u1",
        action="workspace.updated",
        target_type="workspace",
        target_id=WS,
        metadata={"note": "has, comma\nand newline"},
    )
    await doc.insert()
    resp = await admin_client.get(f"/api/v1/workspaces/{WS}/audit/export")
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert len(rows) == 2
    parsed = json.loads(rows[1][7])
    assert parsed["note"] == "has, comma\nand newline"


# ---------------------------------------------------------------------------
# Webhook CRUD
# ---------------------------------------------------------------------------


async def test_post_webhook_rejects_http(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(
        f"/api/v1/workspaces/{WS}/audit/webhooks",
        json={"url": "http://siem.example.com/in"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "webhooks.https_required"


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/in",
        "https://127.0.0.1/in",
        "https://10.0.0.1/in",
        "https://169.254.169.254/latest/meta-data/",
        "https://192.168.1.5/in",
        "https://[::1]/in",
        "https://metadata.google.internal/computeMetadata/v1/",
    ],
)
async def test_post_webhook_rejects_private_address(admin_client: AsyncClient, url: str) -> None:
    """SSRF guard: workspace admins can't aim webhooks at internal targets."""
    resp = await admin_client.post(
        f"/api/v1/workspaces/{WS}/audit/webhooks",
        json={"url": url},
    )
    assert resp.status_code == 403, f"{url} should be rejected"
    assert resp.json()["error"]["code"] == "webhooks.private_address"


async def test_delivery_disables_webhook_if_url_flips_private(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A webhook that resolved to a public IP at create time but flips to a
    private IP later (DNS rebinding / takeover) gets auto-disabled on the
    next delivery attempt without firing the POST."""
    create = await admin_client.post(
        f"/api/v1/workspaces/{WS}/audit/webhooks",
        json={"url": "https://siem.example.com/in"},
    )
    assert create.status_code == 200
    wid = create.json()["id"]

    # Force the safety check to fail at delivery time.
    def _always_unsafe(_url: str) -> None:
        from pocketpaw_ee.cloud._core.errors import Forbidden

        raise Forbidden("webhooks.private_address", "flipped")

    monkeypatch.setattr(audit_webhooks, "_validate_url_safety", _always_unsafe)

    # Use the real delivery path; httpx should never be called.
    with patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post:
        await audit_webhooks.deliver(_make_event())
        assert mock_post.await_count == 0

    refetched = await _AuditWebhookDoc.get(wid)
    assert refetched is not None
    assert refetched.enabled is False
    assert refetched.last_error and "unsafe url" in refetched.last_error


async def test_webhook_roundtrip(admin_client: AsyncClient) -> None:
    create = await admin_client.post(
        f"/api/v1/workspaces/{WS}/audit/webhooks",
        json={"url": "https://siem.example.com/in"},
    )
    assert create.status_code == 200
    body = create.json()
    wid = body["id"]
    assert body["secret"]
    assert body["enabled"] is True

    listing = await admin_client.get(f"/api/v1/workspaces/{WS}/audit/webhooks")
    assert listing.status_code == 200
    items = listing.json()
    assert len(items) == 1
    assert items[0]["id"] == wid
    assert items[0]["secret"] is None

    patched = await admin_client.patch(
        f"/api/v1/workspaces/{WS}/audit/webhooks/{wid}",
        json={"enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    deleted = await admin_client.delete(f"/api/v1/workspaces/{WS}/audit/webhooks/{wid}")
    assert deleted.status_code == 200
    after = await admin_client.get(f"/api/v1/workspaces/{WS}/audit/webhooks")
    assert after.json() == []


async def test_webhook_secret_encrypted_at_rest(admin_client: AsyncClient) -> None:
    """Stored ``secret`` must round-trip through Fernet, never persist plaintext."""
    create = await admin_client.post(
        f"/api/v1/workspaces/{WS}/audit/webhooks",
        json={"url": "https://siem.example.com/in"},
    )
    assert create.status_code == 200
    wid = create.json()["id"]
    plaintext = create.json()["secret"]

    doc = await _AuditWebhookDoc.get(wid)
    assert doc is not None
    assert doc.secret != plaintext  # not stored as plaintext
    assert doc.secret.startswith("gAAAAA")  # Fernet ciphertext marker
    # Round-trip via the module's decrypt helper.
    assert audit_webhooks._decrypt_secret(doc.secret) == plaintext


async def test_rotate_secret_changes_value(admin_client: AsyncClient) -> None:
    create = await admin_client.post(
        f"/api/v1/workspaces/{WS}/audit/webhooks",
        json={"url": "https://siem.example.com/in"},
    )
    wid = create.json()["id"]
    original = create.json()["secret"]

    rotated = await admin_client.post(f"/api/v1/workspaces/{WS}/audit/webhooks/{wid}/rotate")
    assert rotated.status_code == 200
    new_secret = rotated.json()["secret"]
    assert new_secret != original


# ---------------------------------------------------------------------------
# Delivery: signature, failure tracking, auto-disable, replay protection.
# ---------------------------------------------------------------------------


def _make_event() -> _AuditEventDoc:
    return _AuditEventDoc(
        workspace=WS,
        actor_id="u1",
        action="workspace.updated",
        target_type="workspace",
        target_id=WS,
        metadata={"k": "v"},
    )


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


def _patch_httpx(mock_post: AsyncMock):
    client = MagicMock()
    client.post = mock_post

    class _CtxClient:
        async def __aenter__(self_inner):
            return client

        async def __aexit__(self_inner, *a):
            return None

    return patch.object(httpx, "AsyncClient", lambda *a, **kw: _CtxClient())


async def test_deliver_signs_payload_with_timestamp_prefix() -> None:
    secret = "topsecret"
    hook = _AuditWebhookDoc(
        workspace=WS,
        url="https://siem.example.com/in",
        secret=secret,
        created_by="admin",
    )
    await hook.insert()
    event = _make_event()
    await event.insert()

    mock_post = AsyncMock(return_value=_FakeResponse(200))
    with _patch_httpx(mock_post):
        await audit_webhooks.deliver(event)

    assert mock_post.await_count == 1
    call = mock_post.await_args
    headers = call.kwargs["headers"]
    body = call.kwargs["content"]
    ts = headers["X-Paw-Audit-Timestamp"]
    expected = hmac.new(
        secret.encode(),
        f"{ts}.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-Paw-Audit-Signature"] == f"sha256={expected}"

    reloaded = await _AuditWebhookDoc.get(hook.id)
    assert reloaded.last_status == 200
    assert reloaded.failure_count == 0
    assert reloaded.last_error is None


async def test_deliver_records_failure_on_500() -> None:
    hook = _AuditWebhookDoc(
        workspace=WS,
        url="https://siem.example.com/in",
        secret="s",
        created_by="admin",
    )
    await hook.insert()
    event = _make_event()
    await event.insert()

    mock_post = AsyncMock(return_value=_FakeResponse(500))
    with _patch_httpx(mock_post):
        await audit_webhooks.deliver(event)

    reloaded = await _AuditWebhookDoc.get(hook.id)
    assert reloaded.failure_count == 1
    assert reloaded.last_status == 500
    assert reloaded.last_error == "http 500"
    assert reloaded.enabled is True


async def test_deliver_auto_disables_after_10_failures() -> None:
    hook = _AuditWebhookDoc(
        workspace=WS,
        url="https://siem.example.com/in",
        secret="s",
        created_by="admin",
        failure_count=9,
    )
    await hook.insert()
    event = _make_event()
    await event.insert()

    mock_post = AsyncMock(return_value=_FakeResponse(500))
    with _patch_httpx(mock_post):
        await audit_webhooks.deliver(event)

    reloaded = await _AuditWebhookDoc.get(hook.id)
    assert reloaded.failure_count == 10
    assert reloaded.enabled is False


async def test_replay_protection_signature_differs_at_different_times() -> None:
    hook = _AuditWebhookDoc(
        workspace=WS,
        url="https://siem.example.com/in",
        secret="s",
        created_by="admin",
    )
    await hook.insert()
    event = _make_event()
    await event.insert()

    sigs: list[str] = []
    bodies: list[str] = []

    async def _capture(url, content, headers, timeout):  # noqa: ARG001
        sigs.append(headers["X-Paw-Audit-Signature"])
        bodies.append(content)
        return _FakeResponse(200)

    mock_post = AsyncMock(side_effect=_capture)
    with _patch_httpx(mock_post), patch("pocketpaw_ee.cloud.audit.webhooks.time") as t:
        t.time.return_value = 1_000_000
        await audit_webhooks.deliver(event)
        t.time.return_value = 1_000_500
        await audit_webhooks.deliver(event)

    assert bodies[0] == bodies[1]
    assert sigs[0] != sigs[1]


async def test_record_schedules_delivery_fire_and_forget(monkeypatch) -> None:
    called: list[Any] = []

    def _fake_schedule(event):
        called.append(event)

    monkeypatch.setattr(audit_webhooks, "schedule_delivery", _fake_schedule)

    await audit_service.record(
        WS,
        "u1",
        "workspace.updated",
        target_type="workspace",
        target_id=WS,
        metadata={"k": "v"},
    )
    assert len(called) == 1
    assert called[0].action == "workspace.updated"
