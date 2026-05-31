# tests/cloud/test_pocket_refresh_service.py — RFC 04 M3.
# Created: 2026-05-22 — Service-layer coverage for the data-source refresh
# additions: the interval-source scan helper, the webhook-secret trio
# (resolve / get / rotate), and the raw-spec read. Exercises the real
# Beanie path against the in-memory mongomock-motor DB (mongo_db fixture).
#
# What this pins:
#   - list_interval_source_pockets returns only pockets with an `interval`
#     source — pockets with no interval source are excluded.
#   - rotate_webhook_secret generates a secret; get_webhook_secret reads it.
#   - resolve_webhook_pocket authenticates a correct secret and returns the
#     executor creds; a wrong / empty / unset secret returns None.
#   - resolve_webhook_pocket returns None for a missing pocket — identical
#     to a wrong secret, so the endpoint is not a pocket-existence oracle.
#   - rotate invalidates the previous secret.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import ValidationError  # noqa: E402
from pocketpaw_ee.cloud.pockets import service as pockets_service  # noqa: E402


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "refresh-test-auth-secret")


async def _make_pocket(workspace="w1", owner="u1", sources=None):
    """Insert a pocket directly via the Beanie model and return its id."""
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    spec = {"ui": {"type": "root", "children": []}, "state": {}}
    if sources is not None:
        spec["sources"] = sources
    doc = _PocketDoc(
        workspace=workspace,
        name="p",
        description="",
        type="custom",
        owner=owner,
        visibility="workspace",
        rippleSpec=spec,
    )
    await doc.insert()
    return str(doc.id)


# ---------------------------------------------------------------------------
# list_interval_source_pockets
# ---------------------------------------------------------------------------


async def test_list_interval_source_pockets_filters(mongo_db):
    """Only pockets that declare an `interval`-refresh source are returned."""
    interval_pid = await _make_pocket(
        sources={
            "prs": {
                "method": "GET",
                "path": "/pulls",
                "bind": "state.prs",
                "refresh": ["pocket_open", "interval"],
                "refresh_interval_seconds": 300,
            }
        }
    )
    # A pocket with a source but no interval trigger — must be excluded.
    await _make_pocket(
        sources={
            "issues": {
                "method": "GET",
                "path": "/issues",
                "bind": "state.issues",
                "refresh": ["pocket_open", "manual"],
            }
        }
    )
    # A pocket with no sources at all — excluded.
    await _make_pocket(sources=None)

    rows = await pockets_service.list_interval_source_pockets()
    ids = {r["pocket_id"] for r in rows}
    assert interval_pid in ids
    assert len(ids) == 1
    row = next(r for r in rows if r["pocket_id"] == interval_pid)
    assert "prs" in row["sources"]
    assert row["workspace_id"] == "w1"


async def test_list_interval_source_pockets_spans_workspaces(mongo_db):
    """The scan is global — the scheduler has no tenant context."""
    pid_a = await _make_pocket(
        workspace="ws-a",
        sources={"s": {"method": "GET", "path": "/x", "bind": "x", "refresh": ["interval"]}},
    )
    pid_b = await _make_pocket(
        workspace="ws-b",
        sources={"s": {"method": "GET", "path": "/y", "bind": "y", "refresh": ["interval"]}},
    )
    rows = await pockets_service.list_interval_source_pockets()
    ids = {r["pocket_id"] for r in rows}
    assert {pid_a, pid_b} <= ids


# ---------------------------------------------------------------------------
# Webhook secret: rotate + get
# ---------------------------------------------------------------------------


async def test_rotate_then_get_webhook_secret(mongo_db):
    pid = await _make_pocket()
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pid,
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    # No secret until a rotate.
    assert await pockets_service.get_webhook_secret("w1", pid) is None

    secret = await pockets_service.rotate_webhook_secret("w1", "u1", pid)
    assert secret
    assert await pockets_service.get_webhook_secret("w1", pid) == secret


async def test_rotate_invalidates_previous_secret(mongo_db):
    pid = await _make_pocket()
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pid,
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    first = await pockets_service.rotate_webhook_secret("w1", "u1", pid)
    second = await pockets_service.rotate_webhook_secret("w1", "u1", pid)
    assert first != second
    # The old secret no longer authenticates.
    assert await pockets_service.resolve_webhook_pocket(pid, first) is None
    assert await pockets_service.resolve_webhook_pocket(pid, second) is not None


async def test_webhook_secret_requires_backend(mongo_db):
    """A webhook secret with no backend to refresh against is rejected."""
    pid = await _make_pocket()
    with pytest.raises(ValidationError):
        await pockets_service.get_webhook_secret("w1", pid)
    with pytest.raises(ValidationError):
        await pockets_service.rotate_webhook_secret("w1", "u1", pid)


# ---------------------------------------------------------------------------
# resolve_webhook_pocket — constant-time auth, not an oracle
# ---------------------------------------------------------------------------


async def test_resolve_webhook_pocket_valid_secret(mongo_db):
    pid = await _make_pocket()
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pid,
        base_url="https://api.example.com",
        auth_type="bearer",
        auth_token="backend-token",
    )
    secret = await pockets_service.rotate_webhook_secret("w1", "u1", pid)

    creds = await pockets_service.resolve_webhook_pocket(pid, secret)
    assert creds is not None
    base_url, auth_type, _hdr, token, _allowed, _route, workspace_id = creds
    assert base_url == "https://api.example.com"
    assert auth_type == "bearer"
    assert token == "backend-token"  # decrypted round-trip
    assert workspace_id == "w1"


async def test_resolve_webhook_pocket_wrong_secret_is_none(mongo_db):
    pid = await _make_pocket()
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pid,
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    await pockets_service.rotate_webhook_secret("w1", "u1", pid)
    assert await pockets_service.resolve_webhook_pocket(pid, "wrong-secret") is None


async def test_resolve_webhook_pocket_empty_secret_is_none(mongo_db):
    pid = await _make_pocket()
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pid,
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    await pockets_service.rotate_webhook_secret("w1", "u1", pid)
    assert await pockets_service.resolve_webhook_pocket(pid, "") is None


async def test_resolve_webhook_pocket_no_secret_set_is_none(mongo_db):
    """A pocket with a backend but no webhook secret rejects every caller."""
    pid = await _make_pocket()
    await pockets_service.set_pocket_backend(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pid,
        base_url="https://api.example.com",
        auth_type="none",
        auth_token="",
    )
    # No rotate has run — webhook_secret is None.
    assert await pockets_service.resolve_webhook_pocket(pid, "anything") is None


async def test_resolve_webhook_pocket_missing_pocket_is_none(mongo_db):
    """A genuinely-missing pocket returns None — identical to a wrong
    secret, so the endpoint cannot be probed as a pocket-existence oracle."""
    assert await pockets_service.resolve_webhook_pocket("000000000000000000000000", "x") is None
