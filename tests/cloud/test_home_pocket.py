# tests/cloud/test_home_pocket.py — Home-as-Pocket backend foundation.
# Created: 2026-05-21 — TDD coverage for the home-pocket migration:
#   1. ``ensure_home_pocket`` provisions an empty ``type="home"`` pocket and
#      persists its id onto the user's ``home_pocket_id`` setting.
#   2. ``ensure_home_pocket`` is idempotent — a second call returns the same
#      pocket, never double-provisions.
#   3. A stale ``home_pocket_id`` (pocket deleted) re-provisions cleanly.
#   4. The ``"home"`` pocket type round-trips through create + read.
#   5. A ``type="native"`` widget round-trips through ``add_widget`` and
#      ``agent_add_widget`` — persisted and read back without manifest
#      rejection (native widgets carry no rippleSpec to validate).
#
# Uses the shared ``mongo_db`` fixture so the service exercises real Beanie
# reads/writes against an isolated mongomock-motor DB.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.auth import service as auth_service
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import AddWidgetRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

WORKSPACE = "ws-home"


async def _seed_user(email: str = "owner@home.test") -> str:
    """Insert a User and return its id string."""
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Home Owner",
        active_workspace=WORKSPACE,
    )
    await doc.insert()
    return str(doc.id)


# ---------------------------------------------------------------------------
# ensure_home_pocket — provision + idempotency
# ---------------------------------------------------------------------------


async def test_ensure_home_pocket_provisions_empty_home_pocket() -> None:
    user_id = await _seed_user()

    pocket = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    assert pocket["name"] == "Home"
    assert pocket["type"] == "home"
    assert pocket["visibility"] == "private"
    assert pocket["owner"] == user_id
    # No seed widgets — the client owns default widgets.
    assert pocket["widgets"] == []
    # The new pocket id is persisted back onto the user setting.
    assert await auth_service.get_home_pocket_id(user_id) == pocket["_id"]


async def test_ensure_home_pocket_is_idempotent() -> None:
    user_id = await _seed_user()

    first = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)
    second = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    assert first["_id"] == second["_id"]
    # Exactly one home pocket exists for the user — no double-provision.
    pockets = await pockets_service.list_pockets(WORKSPACE, user_id)
    home_pockets = [p for p in pockets if p["type"] == "home"]
    assert len(home_pockets) == 1


async def test_ensure_home_pocket_reprovisions_when_setting_is_stale() -> None:
    user_id = await _seed_user()

    first = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)
    # Pocket is deleted out from under the user, setting now dangles.
    await pockets_service.delete(first["_id"], user_id)

    second = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    assert second["_id"] != first["_id"]
    assert second["type"] == "home"
    assert await auth_service.get_home_pocket_id(user_id) == second["_id"]


# ---------------------------------------------------------------------------
# "home" pocket type accepted as an ordinary private pocket
# ---------------------------------------------------------------------------


async def test_home_type_pocket_round_trips() -> None:
    user_id = await _seed_user()
    pocket = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    fetched = await pockets_service.get(pocket["_id"], user_id)
    assert fetched["type"] == "home"
    assert fetched["visibility"] == "private"


# ---------------------------------------------------------------------------
# native widget round-trip — add_widget + agent_add_widget
# ---------------------------------------------------------------------------


async def test_native_widget_round_trips_through_add_widget() -> None:
    user_id = await _seed_user()
    pocket = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    result = await pockets_service.add_widget(
        pocket["_id"],
        user_id,
        AddWidgetRequest(
            name="Mission · Tray",
            type="native",
            icon="inbox",
            color="#0A84FF",
        ),
    )

    widgets = result["widgets"]
    assert len(widgets) == 1
    native = widgets[0]
    assert native["type"] == "native"
    # The frontend NATIVE_WIDGETS map keys on the widget name.
    assert native["name"] == "Mission · Tray"
    assert native["icon"] == "inbox"
    assert native["color"] == "#0A84FF"

    # Read back: the native widget survives a fresh fetch unchanged.
    fetched = await pockets_service.get(pocket["_id"], user_id)
    assert fetched["widgets"][0]["type"] == "native"
    assert fetched["widgets"][0]["name"] == "Mission · Tray"


async def test_native_widget_round_trips_through_agent_add_widget() -> None:
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    user_id = await _seed_user()
    pocket = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    # agent_add_widget reads workspace/user from the per-stream ContextVars
    # the cloud SSE chat path sets; bind them so the agent path resolves.
    tokens = attach_agent_identity(workspace_id=WORKSPACE, user_id=user_id)
    try:
        view, err = await pockets_service.agent_add_widget(
            pocket["_id"],
            {
                "name": "Mission · Agents in flight",
                "type": "native",
                "icon": "users",
                "color": "#30D158",
            },
        )
    finally:
        detach_agent_identity(tokens)

    # No manifest rejection — native widgets carry no rippleSpec to validate.
    assert err is None
    assert view is not None

    fetched = await pockets_service.get(pocket["_id"], user_id)
    native = fetched["widgets"][0]
    assert native["type"] == "native"
    assert native["name"] == "Mission · Agents in flight"
