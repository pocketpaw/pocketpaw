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


def test_task_calendar_bridge_registered_at_mount():
    """mount_cloud must wire the Task → Calendar bridge onto the realtime
    bus. Pinned here because the bridge's own tests self-register on a
    fixture bus — they'd stay green even if mount_cloud silently dropped
    the production registration (self-registering e2e fixtures have
    hidden exactly this before).

    Mutation that breaks this: remove the register_task_calendar_listeners()
    call from mount_cloud ('drop the mount registration' in
    tests/mutations/tasks_calendar_bridge.json).
    """
    from pocketpaw_ee.cloud._core.realtime.bus import get_bus
    from pocketpaw_ee.cloud.tasks.bridges import calendar as task_cal_bridge

    app = FastAPI()
    mount_cloud(app)  # init_realtime installs a real InProcessBus

    handlers = get_bus()._handlers  # type: ignore[attr-defined]
    assert task_cal_bridge._on_task_proposed in handlers.get("task.proposed", [])
    assert task_cal_bridge._on_task_updated in handlers.get("task.updated", [])
    assert task_cal_bridge._on_task_resolved in handlers.get("task.resolved", [])
