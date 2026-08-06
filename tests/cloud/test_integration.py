"""Integration smoke test — verify all cloud routes mount correctly."""

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


def test_lead_to_prospect_bridge_subscribed_at_mount():
    """mount_cloud must WIRE the leads → growth bridge too.

    Same blind spot as the notification bridge above, and the same cost if it
    goes unnoticed: the bridge is a bus subscriber with no route, its own tests
    self-register through a fixture, so deleting the production
    ``register_growth_lead_listeners()`` call leaves the suite green while every
    captured lead quietly stops reaching /growth.

    Asserts the handler by identity rather than counting subscribers, so it
    still means something when a third listener joins the topic.
    """
    from pocketpaw_ee.cloud.growth.bridges.leads import _on_lead_captured
    from pocketpaw_ee.cloud.shared.events import event_bus

    saved = list(event_bus._handlers["lead.captured"])
    event_bus._handlers["lead.captured"].clear()
    try:
        app = FastAPI()
        mount_cloud(app)
        assert _on_lead_captured in event_bus._handlers["lead.captured"]
    finally:
        event_bus._handlers["lead.captured"] = saved
