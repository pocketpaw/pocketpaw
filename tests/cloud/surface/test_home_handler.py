# tests/cloud/surface/test_home_handler.py — Home surface handler.
#
# Created: 2026-05-24 — Drives the home handler against a seeded
# home pocket (via the real ``pockets_service.ensure_home_pocket``
# path + ``add_widget``) so we exercise the same code path the chat
# router would hit. Three guarantees:
#   1. The pinned-widgets block lists every widget with native/spec
#      markers so the agent can quote what's already on the grid.
#   2. A ``type=spec`` widget with no spec subtree is marked BROKEN —
#      this is the failure mode that previously caused the agent to
#      re-add the same broken row indefinitely.
#   3. An empty workspace (no widgets pinned yet) still produces a
#      usable preamble naming surface=home — the home dashboard is
#      always present, even before the user adds anything.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import AddWidgetRequest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import home as home_handler

pytestmark = pytest.mark.usefixtures("mongo_db")

WORKSPACE = "ws-surface-home"


async def _seed_user(email: str = "owner@surface.test") -> str:
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


async def _seed_home_with_widgets(user_id: str, widgets: list[dict]) -> str:
    """Provision the home pocket and stamp `widgets` onto it via add_widget."""
    pocket, _ = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)
    pocket_id = pocket["_id"]
    for w in widgets:
        # AddWidgetRequest is flat (name/type/spec on the body itself),
        # not wrapped in a `widget` envelope. **w expands the test dict
        # onto the request fields.
        await pockets_service.add_widget(pocket_id, user_id, AddWidgetRequest(**w))
    return pocket_id


async def test_home_handler_lists_pinned_widgets() -> None:
    """Seeded widgets (1 native + 2 spec) appear in the preamble with markers."""
    user_id = await _seed_user()
    await _seed_home_with_widgets(
        user_id,
        [
            {"name": "Active agents", "type": "native"},
            {
                "name": "7-day sales",
                "type": "chart",
                "spec": {
                    "type": "chart",
                    "props": {
                        "variant": "bar",
                        "data": [{"label": "Mon", "value": 1}],
                    },
                },
            },
            {
                "name": "Tasks",
                "type": "list",
                "spec": {"type": "list", "props": {"items": []}},
            },
        ],
    )

    preamble = await home_handler.build_preamble(WORKSPACE, user_id, SurfaceMeta())

    assert '<surface kind="home"' in preamble
    assert "<pinned-widgets" in preamble
    # All three widget names round-tripped.
    assert "Active agents" in preamble
    assert "7-day sales" in preamble
    assert "Tasks" in preamble
    # Native marker is recognised on its row.
    assert "native" in preamble
    # The tools row mentions WebSearch (always on).
    assert "WebSearch" in preamble


async def test_home_handler_marks_broken_spec_widget() -> None:
    """A `type=spec` widget without a `spec` payload is flagged as BROKEN.

    This is the failure mode that previously caused the agent to re-add
    the same broken row — without a marker, it had no way to tell the
    existing tile was already broken.
    """
    user_id = await _seed_user("owner-broken@surface.test")
    await _seed_home_with_widgets(
        user_id,
        [
            # `type=spec` deliberately omits the `spec` payload.
            {"name": "Broken tile", "type": "spec"},
        ],
    )

    preamble = await home_handler.build_preamble(WORKSPACE, user_id, SurfaceMeta())

    assert "Broken tile" in preamble
    assert "BROKEN" in preamble


async def test_home_handler_empty_workspace_returns_minimal_preamble() -> None:
    """An empty workspace gets a usable preamble naming surface=home."""
    user_id = await _seed_user("owner-empty@surface.test")
    # No widgets seeded — ensure_home_pocket provisions an empty pocket.

    preamble = await home_handler.build_preamble(WORKSPACE, user_id, SurfaceMeta())

    assert '<surface kind="home"' in preamble
    # The pinned-widgets block exists with count=0 and an empty marker.
    assert '<pinned-widgets count="0"' in preamble
    assert "empty" in preamble.lower()
