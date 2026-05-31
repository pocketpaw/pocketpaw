# tests/cloud/surface/test_knowledge_handler.py — /knowledge surface handler.
#
# Created: 2026-05-24 — Pins three guarantees on the knowledge surface
# preamble once it stopped synthesizing a single ``workspace:<id>`` scope
# and started reading the real scope list through ``kb.service``:
#   1. Happy path — a workspace with one pocket + workspace-level KB
#      articles surfaces both scope strings in the preamble.
#   2. Empty — a fresh workspace with no kb-routed articles emits the
#      ``(no scopes detected)`` snapshot, never a bare ``workspace:<id>``.
#   3. Cross-workspace guard — stamping with another workspace's id
#      surfaces no pocket / agent identifiers belonging to that
#      workspace, only ever that workspace's own scope (if populated).
#
# The kb-go backend isn't installed on the test runner, so every test
# monkeypatches ``kb_service.list_scopes`` with an in-memory shim. The
# tests still exercise the handler-side branching (empty / non-empty
# rendering) end-to-end.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import knowledge as knowledge_handler

WORKSPACE = "ws-knowledge-handler"
OTHER_WORKSPACE = "ws-knowledge-other"
USER = "u-knowledge"


async def test_knowledge_handler_lists_populated_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace + pocket scopes appear in the preamble when both have articles."""
    workspace_scope = f"workspace:{WORKSPACE}"
    pocket_scope = "pocket:p-001"

    async def _fake_list_scopes(workspace_id: str, user_id: str) -> list[str]:
        # Pin the call shape — handler must forward workspace_id + user_id.
        assert workspace_id == WORKSPACE
        assert user_id == USER
        return [workspace_scope, pocket_scope]

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.surface.handlers.knowledge.kb_service.list_scopes",
        _fake_list_scopes,
    )

    preamble = await knowledge_handler.build_preamble(WORKSPACE, USER, SurfaceMeta())

    assert '<surface kind="knowledge"' in preamble
    assert '<knowledge-scopes count="2"' in preamble
    assert f"- {workspace_scope}" in preamble
    assert f"- {pocket_scope}" in preamble
    # The placeholder branch must NOT fire when real scopes are present.
    assert "(no scopes detected)" not in preamble


async def test_knowledge_handler_empty_workspace_renders_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace with no kb-routed articles emits the empty snapshot.

    The synthetic ``workspace:<id>`` fallback the handler used to ship
    is gone — an empty scope list MUST render the ``(no scopes
    detected)`` block instead so the agent knows the kb is empty rather
    than thinking it has one scope to read from.
    """

    async def _empty(workspace_id: str, user_id: str) -> list[str]:  # noqa: ARG001
        return []

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.surface.handlers.knowledge.kb_service.list_scopes",
        _empty,
    )

    preamble = await knowledge_handler.build_preamble(WORKSPACE, USER, SurfaceMeta())

    assert '<surface kind="knowledge"' in preamble
    assert "(no scopes detected)" in preamble
    # The synthetic fallback is gone — workspace_id must not appear in a
    # ``workspace:<id>`` row when the service reported no real scopes.
    assert f"workspace:{WORKSPACE}" not in preamble
    assert "<knowledge-scopes" not in preamble


async def test_knowledge_handler_cross_workspace_stamp_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stamping a foreign workspace id returns the empty snapshot.

    The kb service filters pockets through ``list_pockets`` (workspace
    + visibility gate), so when the caller stamps a workspace id that
    the user isn't part of, the service returns ``[]`` — and the
    handler renders the placeholder. The test simulates that contract
    by having the shim return ``[]`` for the foreign workspace.
    """
    leaked_pocket_scope = "pocket:leaked-from-other-workspace"

    async def _foreign(workspace_id: str, user_id: str) -> list[str]:  # noqa: ARG001
        # The service would return ``[]`` because list_pockets filters
        # on workspace; the shim mirrors that — no leakage, even though
        # this test "knows" the other-workspace pocket exists.
        if workspace_id == OTHER_WORKSPACE:
            return []
        return [f"workspace:{workspace_id}"]

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.surface.handlers.knowledge.kb_service.list_scopes",
        _foreign,
    )

    preamble = await knowledge_handler.build_preamble(OTHER_WORKSPACE, USER, SurfaceMeta())

    assert "(no scopes detected)" in preamble
    assert leaked_pocket_scope not in preamble


async def test_knowledge_handler_isolates_service_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the kb service raises, the handler renders the placeholder.

    Defensive isolation — the surface preamble must never crash the
    chat send. A missing kb binary or a transient subprocess timeout
    has to degrade to the placeholder instead of bubbling up.
    """

    async def _boom(workspace_id: str, user_id: str) -> list[str]:  # noqa: ARG001
        raise RuntimeError("kb binary not found")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.surface.handlers.knowledge.kb_service.list_scopes",
        _boom,
    )

    preamble = await knowledge_handler.build_preamble(WORKSPACE, USER, SurfaceMeta())

    assert '<surface kind="knowledge"' in preamble
    assert "(no scopes detected)" in preamble
