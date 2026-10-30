"""Tests for ripple $source resolver — walker behavior, no real sources."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.ripple_resolver import ResolveCtx, register, resolve_ripple_spec


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


# Module-level: register a test-only source. Re-registration overwrites,
# so this is safe across test reloads.
@register("test.echo")
async def _echo(ctx, args):
    return {"workspace_id": ctx.workspace_id, "args": args}


@register("test.boom")
async def _boom(ctx, args):
    raise RuntimeError("source intentionally failed")


async def test_top_level_marker_replaced(ctx: ResolveCtx) -> None:
    spec = {"state": {"hello": {"$source": "test.echo", "n": 5}}}
    out = await resolve_ripple_spec(spec, ctx)
    assert out == {"state": {"hello": {"workspace_id": "w1", "args": {"n": 5}}}}


async def test_nested_marker_replaced(ctx: ResolveCtx) -> None:
    spec = {
        "ui": {
            "type": "kanban",
            "props": {"data": {"$source": "test.echo"}},
        }
    }
    out = await resolve_ripple_spec(spec, ctx)
    assert out["ui"]["props"]["data"] == {"workspace_id": "w1", "args": {}}


async def test_unknown_source_returns_none_does_not_raise(ctx: ResolveCtx) -> None:
    spec = {"state": {"x": {"$source": "does.not.exist"}}}
    out = await resolve_ripple_spec(spec, ctx)
    assert out == {"state": {"x": None}}


async def test_failing_source_returns_none_does_not_raise(ctx: ResolveCtx) -> None:
    spec = {"state": {"x": {"$source": "test.boom"}}}
    out = await resolve_ripple_spec(spec, ctx)
    assert out == {"state": {"x": None}}


async def test_non_string_source_name_returns_none(ctx: ResolveCtx) -> None:
    spec = {"state": {"x": {"$source": 42}}}
    out = await resolve_ripple_spec(spec, ctx)
    assert out == {"state": {"x": None}}


async def test_marker_inside_list_replaced(ctx: ResolveCtx) -> None:
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {"type": "page-header", "props": {"title": "x"}},
                {"$source": "test.echo", "tag": "from-list"},
            ],
        }
    }
    out = await resolve_ripple_spec(spec, ctx)
    assert out["ui"]["children"][0] == {"type": "page-header", "props": {"title": "x"}}
    assert out["ui"]["children"][1] == {"workspace_id": "w1", "args": {"tag": "from-list"}}


async def test_multiple_markers_resolved_independently(ctx: ResolveCtx) -> None:
    spec = {
        "state": {
            "ok": {"$source": "test.echo", "n": 1},
            "boom": {"$source": "test.boom"},
            "missing": {"$source": "does.not.exist"},
        }
    }
    out = await resolve_ripple_spec(spec, ctx)
    assert out["state"]["ok"] == {"workspace_id": "w1", "args": {"n": 1}}
    assert out["state"]["boom"] is None
    assert out["state"]["missing"] is None


class _FakePocketCollection:
    """Stands in for ``_PocketDoc.get_pymongo_collection()``: records every
    ``find(filter, projection)`` and yields raw rows, like the async cursor."""

    def __init__(self, rows):
        self._rows = rows
        self.find_calls: list[tuple] = []

    def find(self, *args, **kwargs):
        self.find_calls.append((args, kwargs))
        rows = self._rows

        async def _cursor():
            for row in rows:
                yield row

        return _cursor()

    @property
    def query(self) -> dict:
        assert len(self.find_calls) == 1, f"expected one find, got {self.find_calls!r}"
        args, kwargs = self.find_calls[0]
        return args[0] if args else kwargs["filter"]


def _patch_pocket_collection(rows):
    coll = _FakePocketCollection(rows)
    return coll, patch(
        "pocketpaw_ee.cloud.ripple_sources._PocketDoc.get_pymongo_collection",
        return_value=coll,
    )


async def test_workspace_pockets_source_returns_metadata_for_workspace(ctx):
    # Importing the sources module triggers @register side-effects.
    import pocketpaw_ee.cloud.ripple_sources  # noqa: F401

    rows = [
        {
            "_id": "p1",
            "name": "Bookings",
            "type": "business",
            "icon": "calendar",
            "color": "#0A84FF",
        },
        {"_id": "p2", "name": "Notes", "type": "deep-work", "icon": "note", "color": "#30D158"},
    ]
    coll, patcher = _patch_pocket_collection(rows)
    with patcher:
        spec = {"state": {"all": {"$source": "workspace.pockets"}}}
        out = await resolve_ripple_spec(spec, ctx)

    assert out["state"]["all"] == [
        {
            "id": "p1",
            "name": "Bookings",
            "type": "business",
            "icon": "calendar",
            "color": "#0A84FF",
        },
        {"id": "p2", "name": "Notes", "type": "deep-work", "icon": "note", "color": "#30D158"},
    ]
    # Tenancy invariant: every find call must scope by workspace.
    query = coll.query
    assert "workspace" in str(query)
    assert "w1" in str(query)


async def test_workspace_pockets_source_strict_workspace_scoping(ctx: ResolveCtx) -> None:
    """Stricter tenancy invariant — assert the find query has workspace=ctx.workspace_id
    as an exact dict key, not just substring-present in str(query). Catches refactors
    that loosen the scoping (e.g. dropping the workspace key, or moving it under $or)
    even though the structural invariant 'every find call is workspace-scoped' must hold."""
    import pocketpaw_ee.cloud.ripple_sources  # noqa: F401

    coll, patcher = _patch_pocket_collection([])
    with patcher:
        spec = {"state": {"all": {"$source": "workspace.pockets"}}}
        await resolve_ripple_spec(spec, ctx)

    query = coll.query
    # The top-level workspace key must be set to the ctx's workspace_id exactly.
    assert isinstance(query, dict), f"expected dict query, got {type(query).__name__}"
    assert query.get("workspace") == "w1", (
        f"workspace key must equal ctx.workspace_id; got query={query!r}"
    )
    # Inside the workspace, only pockets the viewer owns, was shared, or that are
    # workspace-visible (mirrors pockets.service.list_pockets).
    assert query.get("$or") == [
        {"owner": "u1"},
        {"shared_with": "u1"},
        {"visibility": "workspace"},
    ], f"visibility filter drifted; got query={query!r}"


async def test_workspace_pockets_source_other_workspace_ctx_scopes_to_other(
    ctx: ResolveCtx,
) -> None:
    """Cross-workspace tenancy proof — when ctx.workspace_id changes from 'w1' to 'w2',
    the find query's workspace key tracks. Demonstrates the source cannot leak across
    workspace boundaries because the query is built from ctx, not from the spec."""
    import pocketpaw_ee.cloud.ripple_sources  # noqa: F401

    other_ctx = ResolveCtx(workspace_id="w2", user_id="u1", pocket_id=None)

    coll, patcher = _patch_pocket_collection([])
    with patcher:
        spec = {"state": {"all": {"$source": "workspace.pockets"}}}
        await resolve_ripple_spec(spec, other_ctx)

    query = coll.query
    assert query.get("workspace") == "w2", (
        f"workspace key must equal other_ctx.workspace_id 'w2'; got query={query!r}"
    )
    # Crucially, "w1" never appears — proves the source ignores any spec-level
    # workspace value and trusts only the ctx (which is server-built from auth).
    assert "w1" not in str(query)


async def test_workspace_members_source_returns_enriched_member_list(ctx):
    import pocketpaw_ee.cloud.ripple_sources  # noqa: F401

    enriched = [
        {"id": "u1", "name": "Alex", "email": "a@x.com", "avatar": "", "role": "owner"},
        {"id": "u2", "name": "Brit", "email": "b@x.com", "avatar": "", "role": "member"},
    ]
    with patch(
        "pocketpaw_ee.cloud.ripple_sources._list_workspace_members",
        new=AsyncMock(return_value=enriched),
    ):
        spec = {"state": {"team": {"$source": "workspace.members"}}}
        out = await resolve_ripple_spec(spec, ctx)
    # Widgets like people-picker call .split() on name — entries must
    # include name, otherwise the renderer crashes. See ripple_sources.
    assert out["state"]["team"] == enriched
    assert all("name" in m for m in out["state"]["team"])
