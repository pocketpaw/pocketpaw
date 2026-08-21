"""Tests for the OSS operational-alert → notification bridge (T-10).

Created 2026-08-06 (feat/coupling-alerts-to-bell). Covers:
- alert SystemEvent → one notification per default-workspace admin
  (kind=alert_<type>, source type="alert")
- non-alert SystemEvents are ignored
- malformed alert payloads (None / non-dict / missing alert_type) are safe
- a notifications_service.create failure never raises out of the handler
  (and doesn't starve the remaining admins)
- registration is idempotent and survives a bus reset
- cross-tenant safety: only the FIRST-created workspace's admins are
  notified on a multi-workspace instance (real Beanie path via mongo_db)
- 2026-08-20 (review): get_default_workspace_id skips soft-deleted
  workspaces (oldest LIVE wins), and push's _target_url returns None for
  source_type="alert" (no alerts route — no deep link)

Mutation plan: tests/mutations/alerts_bridge.json — each gate here names
the mutation that breaks it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.notifications.bridges import alerts as alerts_bridge

from pocketpaw.bus import get_message_bus
from pocketpaw.bus.events import SystemEvent


@pytest.fixture
def patched_audience(monkeypatch):
    """Resolve the default workspace + admins without Mongo."""
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.get_default_workspace_id",
        AsyncMock(return_value="ws-default"),
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.list_admin_ids",
        AsyncMock(return_value=["admin-1", "admin-2"]),
    )


@pytest.fixture
def patched_create(monkeypatch):
    fake = AsyncMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.notifications.service.create", fake)
    return fake


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


async def test_alert_creates_notification_per_admin(patched_audience, patched_create):
    """Mutation 'drop-fanout' (make _fan_out return early) breaks this."""
    await alerts_bridge._on_system_event(
        SystemEvent(
            event_type="alert",
            data={
                "alert_type": "budget_exhausted",
                "severity": "critical",
                "message": "Budget exhausted: $5.0000 spent against $5.0000 cap",
            },
        )
    )
    assert patched_create.call_count == 2
    recipients = {c.kwargs["recipient"] for c in patched_create.call_args_list}
    assert recipients == {"admin-1", "admin-2"}
    kw = patched_create.call_args_list[0].kwargs
    assert kw["workspace_id"] == "ws-default"
    assert kw["kind"] == "alert_budget_exhausted"
    assert kw["title"] == "Budget exhausted"
    assert "Budget exhausted:" in kw["body"]
    assert kw["source"].type == "alert"
    assert kw["source"].id == "budget_exhausted"


async def test_unknown_alert_type_still_fans_out(patched_audience, patched_create):
    """A NEW OSS alert type must not be silently dropped. Mutation
    'gate-on-known-titles' (skip types missing from _TITLES) breaks this."""
    await alerts_bridge._on_system_event(
        SystemEvent(event_type="alert", data={"alert_type": "disk_pressure"})
    )
    assert patched_create.call_count == 2
    kw = patched_create.call_args_list[0].kwargs
    assert kw["kind"] == "alert_disk_pressure"
    assert kw["title"] == "Disk pressure"
    assert kw["body"]  # non-empty fallback body even without a message


async def test_non_alert_system_events_ignored(patched_audience, patched_create):
    """Mutation 'drop-event-type-filter' breaks this."""
    for event_type in ("tool_start", "session_titled", "channel_disconnected", "error"):
        await alerts_bridge._on_system_event(
            SystemEvent(event_type=event_type, data={"alert_type": "budget_exhausted"})
        )
    patched_create.assert_not_called()


# ---------------------------------------------------------------------------
# Malformed payloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"alert_type": ""},
        {"alert_type": 42},
        {"severity": "critical", "message": "no type"},
        "not-a-dict",
        ["also", "not", "a", "dict"],
    ],
)
async def test_malformed_alert_payload_is_safe(patched_audience, patched_create, data):
    """Neither raises nor creates. Mutation 'drop-alert-type-guard' breaks
    this (an int alert_type would raise inside f-string/replace path or
    create a garbage notification)."""
    await alerts_bridge._on_system_event(SystemEvent(event_type="alert", data=data))
    patched_create.assert_not_called()


# ---------------------------------------------------------------------------
# Failure isolation — the alert path must never break
# ---------------------------------------------------------------------------


async def test_notification_failure_never_raises(patched_audience, monkeypatch):
    """Mutation 'unwrap-create' (remove the per-recipient try/except)
    breaks this."""
    boom = AsyncMock(side_effect=RuntimeError("mongo down"))
    monkeypatch.setattr("pocketpaw_ee.cloud.notifications.service.create", boom)
    # Must not raise.
    await alerts_bridge._on_system_event(
        SystemEvent(event_type="alert", data={"alert_type": "error_spike"})
    )
    # And the failure must not starve the second admin — both attempted.
    assert boom.call_count == 2


async def test_handler_never_raises_even_if_fanout_bugs(monkeypatch):
    """The subscriber contract is never-raise, even on a bug in _fan_out
    itself. Mutation 'unwrap-handler' (re-raise in _on_system_event)
    breaks this."""
    monkeypatch.setattr(
        alerts_bridge, "_fan_out", AsyncMock(side_effect=RuntimeError("bug in fan-out"))
    )
    await alerts_bridge._on_system_event(
        SystemEvent(event_type="alert", data={"alert_type": "error_spike"})
    )


async def test_audience_resolution_failure_never_raises(monkeypatch, patched_create):
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.get_default_workspace_id",
        AsyncMock(side_effect=RuntimeError("beanie not initialized")),
    )
    await alerts_bridge._on_system_event(
        SystemEvent(event_type="alert", data={"alert_type": "error_spike"})
    )
    patched_create.assert_not_called()


async def test_no_workspace_yet_drops_alert(monkeypatch, patched_create):
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.get_default_workspace_id",
        AsyncMock(return_value=None),
    )
    await alerts_bridge._on_system_event(
        SystemEvent(event_type="alert", data={"alert_type": "error_spike"})
    )
    patched_create.assert_not_called()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_is_idempotent():
    """Registering twice yields exactly one subscription. Mutation
    'drop-unsubscribe-first' breaks this (double fan-out per alert)."""
    alerts_bridge.register_alert_notification_listeners()
    alerts_bridge.register_alert_notification_listeners()
    subs = get_message_bus()._system_subscribers
    assert subs.count(alerts_bridge._on_system_event) == 1


async def test_registered_handler_receives_published_alert(patched_audience, patched_create):
    """End-to-end through the real OSS bus: publish_system → handler →
    create. Mutation 'drop-subscribe' (register never subscribes) breaks
    this."""
    alerts_bridge.register_alert_notification_listeners()
    await get_message_bus().publish_system(
        SystemEvent(event_type="alert", data={"alert_type": "channel_disconnect"})
    )
    assert patched_create.call_count == 2
    assert patched_create.call_args.kwargs["kind"] == "alert_channel_disconnect"


# ---------------------------------------------------------------------------
# Cross-tenant safety — real Beanie path (mongomock)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_only_first_workspace_admins_notified(patched_create):
    """Multi-workspace instance: instance-scoped alerts go to the FIRST
    (operator) workspace's admins only — never to other tenants. Mutation
    'notify-all-workspaces' and mutation 'admins-to-members' break this."""
    from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
    from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings

    ws1 = Workspace(name="Operator", slug="operator", owner="op", settings=WorkspaceSettings())
    await ws1.insert()
    ws2 = Workspace(name="Tenant B", slug="tenant-b", owner="tb", settings=WorkspaceSettings())
    await ws2.insert()

    async def _user(email: str, ws_id: str, role: str) -> User:
        u = User(
            email=email,
            hashed_password="x",
            workspaces=[WorkspaceMembership(workspace=ws_id, role=role)],
        )
        await u.insert()
        return u

    op_owner = await _user("owner@op.example", str(ws1.id), "owner")
    op_admin = await _user("admin@op.example", str(ws1.id), "admin")
    await _user("member@op.example", str(ws1.id), "member")
    await _user("owner@b.example", str(ws2.id), "owner")
    await _user("admin@b.example", str(ws2.id), "admin")

    await alerts_bridge._on_system_event(
        SystemEvent(event_type="alert", data={"alert_type": "budget_exhausted"})
    )

    recipients = {c.kwargs["recipient"] for c in patched_create.call_args_list}
    assert recipients == {str(op_owner.id), str(op_admin.id)}
    for c in patched_create.call_args_list:
        assert c.kwargs["workspace_id"] == str(ws1.id)


@pytest.mark.usefixtures("mongo_db")
async def test_get_default_workspace_id_orders_by_creation():
    from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    assert await workspace_service.get_default_workspace_id() is None

    first = Workspace(name="First", slug="first", owner="a", settings=WorkspaceSettings())
    await first.insert()
    second = Workspace(name="Second", slug="second", owner="b", settings=WorkspaceSettings())
    await second.insert()

    assert await workspace_service.get_default_workspace_id() == str(first.id)


@pytest.mark.usefixtures("mongo_db")
async def test_get_default_workspace_id_skips_soft_deleted():
    """A soft-deleted first workspace must NOT swallow instance alerts —
    the next-oldest LIVE workspace's admins get them. Mutation
    'workspace: default workspace ignores soft-delete' breaks this."""
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    first = Workspace(name="First", slug="first", owner="a", settings=WorkspaceSettings())
    await first.insert()
    second = Workspace(name="Second", slug="second", owner="b", settings=WorkspaceSettings())
    await second.insert()

    first.deleted_at = datetime.now(UTC)
    await first.save()

    assert await workspace_service.get_default_workspace_id() == str(second.id)

    # Every workspace deleted → no default at all.
    second.deleted_at = datetime.now(UTC)
    await second.save()
    assert await workspace_service.get_default_workspace_id() is None


# ---------------------------------------------------------------------------
# Push deep link — alerts carry NO url
# ---------------------------------------------------------------------------


def test_alert_push_has_no_deep_link():
    """source_type="alert" has no room and no alerts route, so push's
    ``_target_url`` must return None instead of the default
    /chat/<alert_type> (a room that cannot exist). Mutation
    'push: remove the alert arm from _target_url' breaks this."""
    from pocketpaw_ee.cloud.push.listeners import _target_url

    assert _target_url({"source_type": "alert", "source_id": "budget_exhausted"}) is None
