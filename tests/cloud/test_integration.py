"""Integration smoke test — verify all cloud routes mount correctly.

Updated 2026-08-06 (feat/coupling-alerts-to-bell, T-10): added
test_alert_bridge_registered_on_mount — pins the OSS-alert → notification
bridge registration inside mount_cloud().

Updated 2026-08-06 (integration/coupling-sprint): the bridge pins below no
longer hand-roll their own snapshot/restore. Each restored only its OWN topic
while ``mount_cloud()`` registers every bridge, so every pin left the other
bridges subscribed on the module-singleton buses — this file mounts 15 times
and left 15 copies of each handler behind, which failed four tests in
tests/cloud/test_instinct_approvals_governance.py when it ran after this file.
Restoration now belongs to the autouse ``_isolated_bus_subscriptions`` fixture
in conftest; the pins opt into ``clean_bus_slate``, which is what makes them
BITE — an assertion that mount subscribed a handler proves nothing unless the
handler was absent before the mount.
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


def test_alert_bridge_registered_on_mount(clean_bus_slate):
    """mount_cloud() must subscribe the OSS-alert → notification bridge on
    the OSS MessageBus (T-10). Deleting the register call in mount_cloud
    fails this — the workspace bell would silently go dark for
    budget_exhausted / error_spike / channel_disconnect.

    ``clean_bus_slate`` empties ``_system_subscribers`` so a prior test's
    registration can't mask a lost one. It replaces a ``lifecycle.reset_all()``
    call that did the job by nuking every registered singleton — and, because
    ``reset_all`` also CLEARS the lifecycle registry, silently disarmed
    ``reset_all()`` for every test that ran after this one.
    """
    from pocketpaw.bus import get_message_bus

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


def test_lead_captured_bridge_subscribed_at_mount(clean_bus_slate):
    """mount_cloud must WIRE the leads → notifications bridge, not just be able
    to.

    Route assertions can't see this one: the bridge has no endpoint, it is a bus
    subscriber. Without this test the production ``register_lead_notification_
    listeners()`` call can be deleted and the whole suite stays green, because
    the bridge's own e2e tests self-register through a fixture. Then a captured
    lead silently stops ringing the workspace in the deployed app while CI says
    everything is fine.

    ``clean_bus_slate`` empties the topic first — that is what makes the
    assertion a pin rather than a coincidence. Putting the subscribers back
    afterwards is the autouse ``_isolated_bus_subscriptions`` fixture's job,
    and covers every bridge this mount registers rather than just this one.
    """
    from pocketpaw_ee.cloud.leads.bridges.notifications import _on_lead_captured
    from pocketpaw_ee.cloud.shared.events import event_bus

    app = FastAPI()
    mount_cloud(app)

    assert _on_lead_captured in event_bus._handlers["lead.captured"]


def test_lead_to_prospect_bridge_subscribed_at_mount(clean_bus_slate):
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

    app = FastAPI()
    mount_cloud(app)

    assert _on_lead_captured in event_bus._handlers["lead.captured"]


def test_task_calendar_bridge_registered_at_mount():
    """mount_cloud must wire the Task → Calendar bridge onto the realtime
    bus. Pinned here because the bridge's own tests self-register on a
    fixture bus — they'd stay green even if mount_cloud silently dropped
    the production registration (self-registering e2e fixtures have
    hidden exactly this before).

    Mutation that breaks this: remove the register_task_calendar_listeners()
    call from mount_cloud ('drop the mount registration' in
    tests/mutations/tasks_calendar_bridge.json).

    Needs no ``clean_bus_slate``, unlike the bridge pins above: this one reads
    the realtime bus, and ``init_realtime()`` constructs a BRAND-NEW
    ``InProcessBus`` on every mount, so the registry is blank by construction
    and no earlier test's handler can be mistaken for this mount's.
    """
    from pocketpaw_ee.cloud._core.realtime.bus import get_bus
    from pocketpaw_ee.cloud.tasks.bridges import calendar as task_cal_bridge

    app = FastAPI()
    mount_cloud(app)  # init_realtime installs a real InProcessBus

    handlers = get_bus()._handlers  # type: ignore[attr-defined]
    assert task_cal_bridge._on_task_proposed in handlers.get("task.proposed", [])
    assert task_cal_bridge._on_task_updated in handlers.get("task.updated", [])
    assert task_cal_bridge._on_task_resolved in handlers.get("task.resolved", [])
