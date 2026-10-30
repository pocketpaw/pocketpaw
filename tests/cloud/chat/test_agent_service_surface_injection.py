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
#
# Rewritten: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — guarantee (1) is
# GONE, deliberately, and this file now holds the opposite. The preamble is a
# prompt LAYER now (``pocketpaw.prompt.surface``), assembled under the agent's
# identity and above the per-turn material. "Lands first in the dynamic
# context" never actually meant "the agent sees it first": the dynamic context
# is wrapped in "## Your Knowledge Base" and appended at the BOTTOM of the
# prompt, so the preamble was landing last, framed as reference data. It also
# could not carry its cache key from in there, which is what the surface layer
# is for.
#
# What is held here now: ``build_dynamic_context`` emits its three legacy tags
# and NOTHING surface-shaped, for every ctx, so the preamble cannot be rendered
# twice — once by the layer and once inside the knowledge wrapper. The
# threading that replaces it is pinned in ``tests/cloud/runs/`` (into
# ``pool.run``) and ``tests/test_prompt_surface_layer.py`` (into the prompt).

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


async def test_build_dynamic_context_no_longer_carries_the_surface_preamble() -> None:
    """The preamble rides its own prompt layer, so it must NOT also appear
    here. Two copies in one prompt is the failure this guards: the layer would
    render it high, the knowledge wrapper would repeat it at the bottom, and
    nothing would look broken until someone read the assembled prompt."""
    surface = SurfaceContext(
        workspace_id="w1",
        user_id="u1",
        kind=SurfaceKind.HOME,
        meta=SurfaceMeta(),
        preamble=(
            '<surface kind="home" route="/" />\n<pinned-widgets count="0">(empty)</pinned-widgets>'
        ),
        preamble_cache_key="home:s:abc123",
    )
    ctx = _scope_ctx(surface_context=surface)

    rendered = build_dynamic_context(ctx)

    assert "<surface" not in rendered
    assert "<pinned-widgets" not in rendered
    # The three legacy tags are untouched, in their original order.
    assert rendered.splitlines() == [
        "<scope>pocket p1</scope>",
        "<participants>u1, agent-1</participants>",
        '<current-pocket id="p1" />',
    ]


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


async def test_build_dynamic_context_renders_one_shape_for_every_ctx() -> None:
    """A surface-stamped ctx and a bare one now produce the SAME block.

    Stated as its own test because it is the property that makes the layer
    safe: whatever the surface is, this block no longer varies with it, so
    there is exactly one place the preamble can come from.
    """
    surfaced = build_dynamic_context(
        _scope_ctx(
            surface_context=SurfaceContext(
                workspace_id="w1",
                user_id="u1",
                kind=SurfaceKind.GENERIC,
                meta=SurfaceMeta(),
                preamble='<surface kind="generic" route="/x" />',
                preamble_cache_key="generic:/x",
            )
        )
    )
    bare = build_dynamic_context(_scope_ctx(surface_context=None))

    assert surfaced == bare
