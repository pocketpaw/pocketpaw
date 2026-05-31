# tests/cloud/surface/test_pocket_handler.py — Pocket surface handler.
#
# Created: 2026-05-24 — Two guarantees:
#   1. An existing pocket's name, widgets, and (when present) backend
#      summary surface in the preamble.
#   2. An unknown ``pocket_id`` falls back gracefully — the agent still
#      gets a surface tag but the snapshot is marked unavailable; the
#      chat path never breaks because the client passed a stale id.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import pocket as pocket_handler

pytestmark = pytest.mark.usefixtures("mongo_db")

WORKSPACE = "ws-surface-pocket"


async def _seed_user(email: str = "owner@pocket.test") -> str:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Pocket Owner",
        active_workspace=WORKSPACE,
    )
    await doc.insert()
    return str(doc.id)


async def test_pocket_handler_summarizes_existing_pocket() -> None:
    """An existing pocket appears in the preamble with name + counts."""
    user_id = await _seed_user()
    pocket = await pockets_service.create(
        WORKSPACE,
        user_id,
        CreatePocketRequest(name="Sales Pipeline"),
    )

    preamble = await pocket_handler.build_preamble(
        WORKSPACE, user_id, SurfaceMeta(pocket_id=pocket["_id"])
    )

    assert '<surface kind="pocket"' in preamble
    assert "Sales Pipeline" in preamble
    assert pocket["_id"] in preamble
    # The current-pocket tag carries the widget count.
    assert "widgets=" in preamble


async def test_pocket_handler_unknown_pocket_id_falls_back() -> None:
    """A stale / non-existent pocket id returns a minimal preamble.

    No exception should escape; the chat router gets a usable preamble
    even when the client sent a deleted pocket's id.
    """
    user_id = await _seed_user("owner-bad@pocket.test")
    # Mongo ObjectIds are 24-hex-chars; supply one that points at nothing.
    bad_id = "ffffffffffffffffffffffff"

    preamble = await pocket_handler.build_preamble(
        WORKSPACE, user_id, SurfaceMeta(pocket_id=bad_id)
    )

    # Surface tag still present — agent knows it's on a pocket route.
    assert '<surface kind="pocket"' in preamble
    assert bad_id in preamble
    # And the snapshot is flagged as unavailable rather than empty.
    assert "unavailable" in preamble.lower()
