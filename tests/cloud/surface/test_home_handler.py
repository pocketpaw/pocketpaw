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
#
# Updated: 2026-08-11 (fix/sites-refine-preamble-engine-fork) — repaired guarantee
# 3, which had been RED on dev. ``ensure_home_pocket`` stopped provisioning an
# empty pocket when built-in home widgets moved to the DB
# (``feat(pockets): built-in home widgets from DB``): it now auto-seeds every
# ``auto_seed=True`` builtin, so a fresh user has "Intent of the Day" pinned and the
# ``count="0"`` assertion was measuring that change rather than the handler. The
# empty branch is still reachable — unpin what was seeded — so the test unpins
# instead of dropping the assertion, and a second test covers the grid a real new
# user gets, which nothing had asserted.
#
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import AddWidgetRequest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import home as home_handler

from pocketpaw.prompt.entity import ID_TAIL_CHARS

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

    preamble = (await home_handler.build_preamble(WORKSPACE, user_id, SurfaceMeta())).text

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

    preamble = (await home_handler.build_preamble(WORKSPACE, user_id, SurfaceMeta())).text

    assert "Broken tile" in preamble
    assert "BROKEN" in preamble


async def test_home_handler_empty_workspace_returns_minimal_preamble() -> None:
    """A home grid with nothing pinned gets a usable preamble naming surface=home.

    ``ensure_home_pocket`` no longer provisions an EMPTY pocket: since
    ``feat(pockets): built-in home widgets from DB`` it auto-seeds every
    ``auto_seed=True`` builtin, which today is "Intent of the Day"
    (``tests/cloud/test_home_pocket.py`` pins that as intended). The empty branch
    is still reachable — a user can unpin what was seeded — so this test now
    UNPINS it rather than asserting a zero that provisioning stopped producing.
    Without that the assertion was measuring the seeding change, not the handler.
    """
    user_id = await _seed_user("owner-empty@surface.test")
    pocket, _ = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)
    # ``_id`` is the widget wire key emitted by ``dto._widget_to_wire``.
    for widget in pocket["widgets"]:
        await pockets_service.remove_widget(pocket["_id"], widget["_id"], user_id)

    preamble = (await home_handler.build_preamble(WORKSPACE, user_id, SurfaceMeta())).text

    assert '<surface kind="home"' in preamble
    # The pinned-widgets block exists with count=0 and an empty marker.
    assert '<pinned-widgets count="0"' in preamble
    assert "empty" in preamble.lower()


async def test_home_handler_lists_the_auto_seeded_builtin() -> None:
    """A new user's auto-seeded widgets include the IDs required by update_widget."""
    user_id = await _seed_user("owner-seeded@surface.test")
    pocket, created = await pockets_service.ensure_home_pocket(WORKSPACE, user_id)
    assert created is True

    preamble = (await home_handler.build_preamble(WORKSPACE, user_id, SurfaceMeta())).text

    assert '<surface kind="home"' in preamble
    assert f'<pinned-widgets count="{len(pocket["widgets"])}"' in preamble
    for widget in pocket["widgets"]:
        assert widget["name"] in preamble
        assert widget["_id"][-ID_TAIL_CHARS:] in preamble
    assert "id=?" not in preamble
