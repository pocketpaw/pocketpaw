"""Tests for ripple $source resolver — walker behavior, no real sources."""

from __future__ import annotations

import pytest

from ee.cloud.ripple_resolver import ResolveCtx, resolve_ripple_spec


@pytest.fixture
def ctx() -> ResolveCtx:
    return ResolveCtx(workspace_id="w1", user_id="u1", pocket_id="p1")


async def test_empty_spec_returns_empty(ctx: ResolveCtx) -> None:
    assert await resolve_ripple_spec({}, ctx) == {}


async def test_spec_without_sources_is_identity(ctx: ResolveCtx) -> None:
    spec = {
        "state": {"draft": "", "next_id": 3, "tasks": [{"id": "t1", "title": "x"}]},
        "ui": {"type": "flex", "props": {"direction": "column"}, "children": []},
    }
    assert await resolve_ripple_spec(spec, ctx) == spec


async def test_resolver_does_not_mutate_input(ctx: ResolveCtx) -> None:
    spec = {"state": {"a": [1, 2, 3]}, "ui": {"type": "stat"}}
    snapshot = {"state": {"a": [1, 2, 3]}, "ui": {"type": "stat"}}
    await resolve_ripple_spec(spec, ctx)
    assert spec == snapshot
