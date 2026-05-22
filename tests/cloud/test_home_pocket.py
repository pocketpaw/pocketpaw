# tests/cloud/test_home_pocket.py — Home-as-Pocket backend foundation.
# Created: 2026-05-21 — TDD coverage for the home-pocket migration:
#   1. ``ensure_home_pocket`` provisions an empty ``type="home"`` pocket,
#      persists its id onto the user's ``home_pocket_id`` setting, and
#      reports ``created=True``.
#   2. ``ensure_home_pocket`` is idempotent — a second call returns the same
#      pocket with ``created=False``, never double-provisions.
#   3. A stale ``home_pocket_id`` (pocket deleted) re-provisions cleanly and
#      reports ``created=True`` again.
#   4. The ``"home"`` pocket type round-trips through create + read.
#   5. A ``type="native"`` widget round-trips through ``add_widget`` and
#      ``agent_add_widget`` — persisted and read back without manifest
#      rejection (native widgets carry no rippleSpec to validate).
#   6. Concurrent first-login ``ensure_home_pocket`` calls resolve to a
#      single home pocket — the atomic CAS closes the provision race.
#
# Updated: 2026-05-21 — ``ensure_home_pocket`` now returns a
# ``(pocket_dict, created)`` tuple; tests unpack it and assert the flag.
# Updated: 2026-05-21 — added the provision-race coverage; ``ensure_home_pocket``
# persists the new id via an atomic compare-and-swap.
# Updated: 2026-05-22 — a Ripple-spec widget (``type="chart"`` with a real
# ``data`` series) round-trips through ``add_widget`` / ``agent_add_widget``:
# the widget's ``spec`` rippleSpec subtree is stored and read back so the
# home grid can render the tile.
#
# Uses the shared ``mongo_db`` fixture so the service exercises real Beanie
# reads/writes against an isolated mongomock-motor DB.

from __future__ import annotations

import asyncio

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

    pocket, created = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    # First call on a fresh user provisions a brand-new home pocket.
    assert created is True
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

    first, first_created = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)
    second, second_created = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    assert first["_id"] == second["_id"]
    # Only the first call provisioned — the second returned the existing one.
    assert first_created is True
    assert second_created is False
    # Exactly one home pocket exists for the user — no double-provision.
    pockets = await pockets_service.list_pockets(WORKSPACE, user_id)
    home_pockets = [p for p in pockets if p["type"] == "home"]
    assert len(home_pockets) == 1


async def test_ensure_home_pocket_reprovisions_when_setting_is_stale() -> None:
    user_id = await _seed_user()

    first, first_created = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)
    # Pocket is deleted out from under the user, setting now dangles.
    await pockets_service.delete(first["_id"], user_id)

    second, second_created = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    assert second["_id"] != first["_id"]
    assert second["type"] == "home"
    # A stale setting re-provisions — created is True on both genuine creates.
    assert first_created is True
    assert second_created is True
    assert await auth_service.get_home_pocket_id(user_id) == second["_id"]


# ---------------------------------------------------------------------------
# "home" pocket type accepted as an ordinary private pocket
# ---------------------------------------------------------------------------


async def test_home_type_pocket_round_trips() -> None:
    user_id = await _seed_user()
    pocket, _ = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    fetched = await pockets_service.get(pocket["_id"], user_id)
    assert fetched["type"] == "home"
    assert fetched["visibility"] == "private"


# ---------------------------------------------------------------------------
# native widget round-trip — add_widget + agent_add_widget
# ---------------------------------------------------------------------------


async def test_native_widget_round_trips_through_add_widget() -> None:
    user_id = await _seed_user()
    pocket, _ = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

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
    pocket, _ = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

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


# ---------------------------------------------------------------------------
# Ripple-spec widget round-trip — the home agent's add_widget path
# ---------------------------------------------------------------------------


def _chart_widget_payload() -> dict:
    """A chart widget entry with a populated rippleSpec ``spec`` subtree —
    the shape the home agent's ``add_widget`` MCP tool produces."""
    return {
        "name": "7-day sales",
        "type": "chart",
        "icon": "trending-up",
        "color": "#0A84FF",
        "spec": {
            "type": "chart",
            "props": {
                "variant": "bar",
                "data": [
                    {"label": "Mon", "value": 1200},
                    {"label": "Tue", "value": 1850},
                    {"label": "Wed", "value": 1400},
                    {"label": "Thu", "value": 2100},
                    {"label": "Fri", "value": 2600},
                    {"label": "Sat", "value": 900},
                    {"label": "Sun", "value": 700},
                ],
            },
        },
    }


async def test_chart_widget_round_trips_through_add_widget() -> None:
    """A Ripple-spec widget carries a ``spec`` rippleSpec subtree. The home
    grid renders the tile from ``widget.spec``, so the field must survive a
    create + read round-trip."""
    user_id = await _seed_user()
    pocket, _ = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    payload = _chart_widget_payload()
    result = await pockets_service.add_widget(
        pocket["_id"],
        user_id,
        AddWidgetRequest(
            name=payload["name"],
            type=payload["type"],
            icon=payload["icon"],
            color=payload["color"],
            spec=payload["spec"],
        ),
    )

    widgets = result["widgets"]
    assert len(widgets) == 1
    chart = widgets[0]
    assert chart["type"] == "chart"
    assert chart["spec"]["type"] == "chart"
    # The chart carries a real 7-point data series — not a bare stat tile.
    assert len(chart["spec"]["props"]["data"]) == 7
    assert chart["spec"]["props"]["data"][0] == {"label": "Mon", "value": 1200}

    # Read back: the spec survives a fresh fetch unchanged.
    fetched = await pockets_service.get(pocket["_id"], user_id)
    assert fetched["widgets"][0]["spec"]["props"]["data"][4]["value"] == 2600


async def test_chart_widget_round_trips_through_agent_add_widget() -> None:
    """The agent path (``agent_add_widget``) stores the chart ``spec`` too —
    the home agent's MCP tool reaches the store through this helper."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    user_id = await _seed_user()
    pocket, _ = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)

    tokens = attach_agent_identity(workspace_id=WORKSPACE, user_id=user_id)
    try:
        view, err = await pockets_service.agent_add_widget(
            pocket["_id"],
            _chart_widget_payload(),
        )
    finally:
        detach_agent_identity(tokens)

    assert err is None
    assert view is not None
    chart = view["widgets"][0]
    assert chart["type"] == "chart"
    assert len(chart["spec"]["props"]["data"]) == 7

    fetched = await pockets_service.get(pocket["_id"], user_id)
    assert fetched["widgets"][0]["spec"]["props"]["data"][3]["label"] == "Thu"


# ---------------------------------------------------------------------------
# claim_home_pocket_id — atomic compare-and-swap
# ---------------------------------------------------------------------------


async def test_claim_home_pocket_id_swaps_when_expected_matches() -> None:
    user_id = await _seed_user()

    # Fresh user: home_pocket_id is None — the None-expecting claim takes.
    claimed = await auth_service.claim_home_pocket_id(user_id, "pkt-1", expected=None)
    assert claimed is True
    assert await auth_service.get_home_pocket_id(user_id) == "pkt-1"


async def test_claim_home_pocket_id_rejects_when_expected_is_stale() -> None:
    user_id = await _seed_user()
    await auth_service.claim_home_pocket_id(user_id, "pkt-1", expected=None)

    # A second claim that still expects None loses — the field already moved.
    claimed = await auth_service.claim_home_pocket_id(user_id, "pkt-2", expected=None)
    assert claimed is False
    # The value of record is unchanged — no clobber.
    assert await auth_service.get_home_pocket_id(user_id) == "pkt-1"


# ---------------------------------------------------------------------------
# first-login provision race — concurrent ensure_home_pocket calls
# ---------------------------------------------------------------------------


async def test_concurrent_ensure_home_pocket_resolves_to_one_pocket() -> None:
    user_id = await _seed_user()

    # Two first-login /home calls race. Both read home_pocket_id == None,
    # both insert a pocket; the atomic CAS lets exactly one commit its id.
    (pocket_a, created_a), (pocket_b, created_b) = await asyncio.gather(
        pockets_service.ensure_home_pocket(WORKSPACE, user_id),
        pockets_service.ensure_home_pocket(WORKSPACE, user_id),
    )

    # Both callers see the same home pocket — the loser adopted the winner's.
    assert pocket_a["_id"] == pocket_b["_id"]
    # Exactly one call reports created=True; the loser reports False.
    assert {created_a, created_b} == {True, False}

    # The user setting points at that one pocket.
    home_id = await auth_service.get_home_pocket_id(user_id)
    assert home_id == pocket_a["_id"]

    # No orphan: exactly one home pocket exists for the user.
    pockets = await pockets_service.list_pockets(WORKSPACE, user_id)
    home_pockets = [p for p in pockets if p["type"] == "home"]
    assert len(home_pockets) == 1
    assert home_pockets[0]["_id"] == home_id


async def test_concurrent_ensure_home_pocket_many_callers_one_pocket() -> None:
    # Stress the race a little harder — five concurrent first-login calls.
    user_id = await _seed_user()

    results = await asyncio.gather(
        *(pockets_service.ensure_home_pocket(WORKSPACE, user_id) for _ in range(5))
    )

    ids = {pocket["_id"] for pocket, _ in results}
    assert len(ids) == 1  # every caller converged on the same pocket

    pockets = await pockets_service.list_pockets(WORKSPACE, user_id)
    home_pockets = [p for p in pockets if p["type"] == "home"]
    assert len(home_pockets) == 1
