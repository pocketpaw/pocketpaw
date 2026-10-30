"""Integration smoke test — verify all cloud routes mount correctly.

Updated 2026-08-06 (feat/coupling-alerts-to-bell, T-10): added
test_alert_bridge_registered_on_mount — pins the OSS-alert → notification
bridge registration inside mount_cloud().
"""

from __future__ import annotations

from fastapi import FastAPI
from pocketpaw_ee.cloud import mount_cloud


def _get_route_paths(app: FastAPI) -> list[str]:
    """Extract all route paths from a FastAPI app."""
    paths = []
    for route in app.routes:
        if hasattr(route, "path"):
            paths.append(route.path)
    return paths


def test_mount_cloud_succeeds():
    """mount_cloud() should not raise."""
    app = FastAPI()
    mount_cloud(app)


def test_auth_routes_mounted():
    app = FastAPI()
    mount_cloud(app)
    paths = _get_route_paths(app)
    # fastapi-users mounts at /api/v1/auth/*
    assert any("/auth" in p for p in paths)


def test_workspace_routes_mounted():
    app = FastAPI()
    mount_cloud(app)
    paths = _get_route_paths(app)
    assert any("/workspaces" in p for p in paths)


def test_agents_routes_mounted():
    app = FastAPI()
    mount_cloud(app)
    paths = _get_route_paths(app)
    assert any("/agents" in p for p in paths)


def test_chat_routes_mounted():
    app = FastAPI()
    mount_cloud(app)
    paths = _get_route_paths(app)
    assert any("/chat" in p for p in paths)


def test_pockets_routes_mounted():
    app = FastAPI()
    mount_cloud(app)
    paths = _get_route_paths(app)
    assert any("/pockets" in p for p in paths)


def test_sessions_routes_mounted():
    app = FastAPI()
    mount_cloud(app)
    paths = _get_route_paths(app)
    assert any("/sessions" in p for p in paths)


def test_websocket_endpoint_mounted():
    app = FastAPI()
    mount_cloud(app)
    paths = _get_route_paths(app)
    assert any("ws/cloud" in p for p in paths)


def test_license_endpoint_mounted():
    app = FastAPI()
    mount_cloud(app)
    paths = _get_route_paths(app)
    assert "/api/v1/license" in paths


def test_cloud_error_handler_registered():
    """CloudError exception handler should be registered."""
    from pocketpaw_ee.cloud.shared.errors import CloudError

    app = FastAPI()
    mount_cloud(app)
    assert CloudError in app.exception_handlers


def test_alert_bridge_registered_on_mount():
    """mount_cloud() must subscribe the OSS-alert → notification bridge on
    the OSS MessageBus (T-10). Deleting the register call in mount_cloud
    fails this — the workspace bell would silently go dark for
    budget_exhausted / error_spike / channel_disconnect."""
    from pocketpaw.bus import get_message_bus
    from pocketpaw.lifecycle import reset_all

    reset_all()  # fresh bus so a prior test's registration can't mask a lost one
    app = FastAPI()
    mount_cloud(app)

    from pocketpaw_ee.cloud.notifications.bridges.alerts import _on_system_event

    assert get_message_bus()._system_subscribers.count(_on_system_event) == 1


def test_total_route_count():
    """Sanity check — we should have a good number of routes."""
    app = FastAPI()
    mount_cloud(app)
    paths = _get_route_paths(app)
    # We have ~50+ endpoints across 6 domains + license + ws
    assert len(paths) >= 40, f"Only {len(paths)} routes mounted — expected 40+"


def test_lead_captured_bridge_subscribed_at_mount():
    """mount_cloud must WIRE the leads → notifications bridge, not just be able
    to.

    Route assertions can't see this one: the bridge has no endpoint, it is a bus
    subscriber. Without this test the production ``register_lead_notification_
    listeners()`` call can be deleted and the whole suite stays green, because
    the bridge's own e2e tests self-register through a fixture. Then a captured
    lead silently stops ringing the workspace in the deployed app while CI says
    everything is fine.

    Restores the subscriber list afterwards so mounting here doesn't leave a
    handler behind for tests that run later in the session.
    """
    from pocketpaw_ee.cloud.leads.bridges.notifications import _on_lead_captured
    from pocketpaw_ee.cloud.shared.events import event_bus

    saved = list(event_bus._handlers["lead.captured"])
    event_bus._handlers["lead.captured"].clear()
    try:
        app = FastAPI()
        mount_cloud(app)
        assert _on_lead_captured in event_bus._handlers["lead.captured"]
    finally:
        event_bus._handlers["lead.captured"] = saved
