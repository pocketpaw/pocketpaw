# tests/cloud/chat/test_agent_service_surface_injection.py — Wire test.
#
# Created: 2026-05-24 — Verifies the two end-to-end guarantees of the
# chat agent wiring:
#   1. When a ``surface_context`` is attached to the ScopeContext, its
#      preamble lands FIRST in the dynamic-context block (before
#      scope/participants/current-pocket).
#   2. When no surface_context is attached (older clients that don't
#      send the new fields), the dynamic-context block keeps its
#      legacy three-line shape — no regression for unmigrated callers.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    build_dynamic_context,
)
from pocketpaw_ee.cloud.surface import SurfaceContext, SurfaceKind, SurfaceMeta

pytestmark = pytest.mark.asyncio


def _scope_ctx(**overrides) -> ScopeContext:
    """Minimal ScopeContext for build_dynamic_context tests."""
    base = {
        "kind": ScopeKind.POCKET,
        "scope_id": "p1",
        "workspace_id": "w1",
        "user_id": "u1",
        "members": ["u1", "agent-1"],
        "target_agent_id": "agent-1",
        "pocket_id": "p1",
    }
    base.update(overrides)
    return ScopeContext(**base)


async def test_build_dynamic_context_prepends_surface_preamble_when_present() -> None:
    """Surface preamble lands before scope/participants/current-pocket tags."""
    surface = SurfaceContext(
        workspace_id="w1",
        user_id="u1",
        kind=SurfaceKind.HOME,
        meta=SurfaceMeta(),
        preamble=(
            '<surface kind="home" route="/" />\n<pinned-widgets count="0">(empty)</pinned-widgets>'
        ),
    )
    ctx = _scope_ctx(surface_context=surface)

    rendered = build_dynamic_context(ctx)
    lines = rendered.splitlines()

    # Surface block comes first.
    assert lines[0].startswith('<surface kind="home"')
    # Pinned-widgets next, before scope.
    assert "<pinned-widgets" in lines[1]
    # Legacy scope/participants/current-pocket still follow.
    assert any("<scope>pocket p1</scope>" in line for line in lines)
    assert any("<participants>" in line for line in lines)
    assert any('<current-pocket id="p1"' in line for line in lines)


async def test_build_dynamic_context_falls_back_to_old_shape_when_surface_context_is_none() -> None:
    """No surface_context = legacy three-line shape — back-compat guarantee."""
    ctx = _scope_ctx(surface_context=None)

    rendered = build_dynamic_context(ctx)

    # No surface tag at all — preserves the pre-surface-context wire shape.
    assert "<surface" not in rendered
    assert "<pinned-widgets" not in rendered
    # Legacy tags exactly as before.
    assert "<scope>pocket p1</scope>" in rendered
    assert "<participants>u1, agent-1</participants>" in rendered
    assert '<current-pocket id="p1" />' in rendered


async def test_build_dynamic_context_skips_empty_preamble() -> None:
    """A surface_context with an empty preamble (handler fell back) is skipped.

    The empty-preamble path is the GENERIC fall-back the resolver uses
    when validation fails or a handler raises. The dynamic-context
    block must not emit an empty leading newline in that case.
    """
    surface = SurfaceContext(
        workspace_id="w1",
        user_id="u1",
        kind=SurfaceKind.GENERIC,
        meta=SurfaceMeta(),
        preamble="",
    )
    ctx = _scope_ctx(surface_context=surface)

    rendered = build_dynamic_context(ctx)
    lines = rendered.splitlines()

    # First line must be the scope tag — no blank leader from a "" preamble.
    assert lines[0].startswith("<scope>")
