# tests/cloud/pockets/test_home_widget_sources.py
# Created: 2026-05-31 (feat/home-pocket-sources-authoring) — coverage for the
# add_widget path authoring a pocket-level RFC-04 source alongside a tile.
#
# Feature: "live data on the home page." The home page is a per-user pocket
# whose grid renders ``pocket.widgets[]`` tiles; RFC-04 live data sources live
# at the pocket's top-level ``rippleSpec.sources`` (a dict keyed by source
# name) and the ``source_executor`` runs them via ``POST /pockets/{id}/
# sources/run``. Before this change the home agent's ``add_widget`` could add a
# tile but not the source that feeds it, so the tile stayed static.
#
# What's covered:
#   - ``add_widget_for_agent`` with a ``sources`` block merges the binding
#     into the pocket's top-level ``rippleSpec.sources`` AND adds the tile.
#   - ``agent_add_widget`` (service layer) is the same on the explicit path.
#   - A ``sources`` block merges ALONGSIDE an existing ``rippleSpec.sources``
#     dict (additive, doesn't clobber a sibling source).
#   - Omitting ``sources`` still adds the tile with no ``sources`` key
#     materialised (no regression for legacy ``add_widget`` calls).
#   - An invalid source binding is handled per the LOGGED (drift-allowed)
#     gate: the tile still lands, the bad source is skipped, a valid sibling
#     in the same batch is persisted.
#   - The authored binding matches the RFC-04 ``SourceBinding`` schema
#     (``method`` / ``path`` / ``bind`` / ``refresh``) so the executor runs
#     it unchanged; ``refresh`` defaults to ``["pocket_open"]``.

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.pockets import agent_context  # noqa: E402
from pocketpaw_ee.cloud.pockets import service as pocket_service  # noqa: E402

# ---------------------------------------------------------------------------
# Fake doc + patch helper (mirrors test_pocket_sources_ops.py)
# ---------------------------------------------------------------------------


class _FakeDoc:
    """Minimum surface to stand in for a ``_PocketDoc`` in these tests.

    Mutations operate on ``self.rippleSpec`` / ``self.widgets`` in place,
    like a real Beanie doc. ``save()`` is an async no-op; calls are counted
    via ``saves``.
    """

    def __init__(self, pocket_id: str, ripple_spec: dict[str, Any] | None):
        self.id = pocket_id
        self.workspace = "w1"
        self.name = "Home"
        self.description = ""
        self.type = "home"
        self.icon = ""
        self.color = ""
        self.owner = "u1"
        self.visibility = "workspace"
        self.team: list[str] = []
        self.agents: list[str] = []
        self.widgets: list[Any] = []
        self.rippleSpec = ripple_spec
        self.share_link_token = None
        self.share_link_access = "view"
        self.shared_with: list[str] = []
        self.tool_specs: list[Any] = []
        self.saves = 0

    async def save(self) -> None:
        self.saves += 1

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "_id": self.id,
            "workspace": self.workspace,
            "rippleSpec": self.rippleSpec,
            "owner": self.owner,
            "widgets": [
                w.model_dump(by_alias=True) if hasattr(w, "model_dump") else w for w in self.widgets
            ],
        }


@pytest.fixture
def home_doc() -> _FakeDoc:
    """A persisted home pocket with a UI tree but NO sources and NO tiles."""
    return _FakeDoc(
        "507f1f77bcf86cd799439011",
        {
            "version": "1.0",
            "state": {"revenue": 0},
            "ui": {"id": "n_root0000", "type": "flex", "children": []},
        },
    )


def _patches(doc: _FakeDoc, *, allowed_types: list[str] | None = None):
    """Patch the doc fetch + emit/push seams + identity ContextVars + the
    catalog allow-list. ``allowed_types=None`` simulates a manifest outage so
    the widget-spec gate is a no-op (keeps these tests focused on the sources
    merge, not on the catalog walk). Returns ``(ExitStack, push_calls)``.
    """
    push_calls: list[dict[str, Any]] = []

    def _capture(payload: dict[str, Any]) -> None:
        push_calls.append(payload)

    stack = ExitStack()
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._PocketDoc.get",
            new=AsyncMock(return_value=doc),
        )
    )
    stack.enter_context(patch("pocketpaw_ee.cloud.pockets.service.emit", new=AsyncMock()))
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._pocket_event_payload",
            new=AsyncMock(return_value={"pocket_id": doc.id}),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.push_pocket_mutation",
            new=MagicMock(side_effect=_capture),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service.normalize_ripple_spec",
            new=lambda s: s,
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            new=MagicMock(return_value=doc.workspace),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            new=MagicMock(return_value=doc.owner),
        )
    )

    async def _allowed() -> list[str] | None:
        return allowed_types

    stack.enter_context(patch.object(pocket_service, "_catalog_allowed_types", _allowed))
    return stack, push_calls


# A tile bound to ``state.revenue`` — the source below hydrates that path.
_REVENUE_TILE = {
    "name": "Live revenue",
    "type": "stat",
    "spec": {"type": "stat", "props": {"label": "Revenue", "value": "{state.revenue}"}},
}

# The RFC-04 source that feeds the tile. method/path/bind/refresh — the exact
# SourceBinding field names so the executor runs it unchanged.
_REVENUE_SOURCE = {
    "method": "GET",
    "path": "/revenue/today",
    "bind": "state.revenue",
    "refresh": ["pocket_open", "manual"],
}


# ---------------------------------------------------------------------------
# Step 1 — the red test: add_widget authors a top-level source.
# ---------------------------------------------------------------------------


async def test_add_widget_for_agent_authors_source_on_home_pocket(home_doc):
    """The core feature: calling ``add_widget_for_agent`` with a ``sources``
    block persists the binding at the pocket's top-level
    ``rippleSpec.sources`` AND adds the tile. Without this the agent could
    add a tile but it stayed static."""
    widget = {**_REVENUE_TILE, "sources": {"revenue": dict(_REVENUE_SOURCE)}}
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        result = await agent_context.add_widget_for_agent(home_doc.id, widget)

    assert result["ok"] is True
    # The source landed at the pocket's top-level rippleSpec.sources.
    assert home_doc.rippleSpec["sources"]["revenue"]["path"] == "/revenue/today"
    assert home_doc.rippleSpec["sources"]["revenue"]["bind"] == "state.revenue"
    assert home_doc.rippleSpec["sources"]["revenue"]["method"] == "GET"
    # The tile landed too.
    assert len(home_doc.widgets) == 1
    assert home_doc.widgets[0].name == "Live revenue"
    assert home_doc.saves == 1


async def test_agent_add_widget_service_authors_source(home_doc):
    """Same merge on the service-layer entry point ``agent_add_widget`` with
    an explicit ``sources`` kwarg."""
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        view, err = await pocket_service.agent_add_widget(
            home_doc.id, dict(_REVENUE_TILE), sources={"revenue": dict(_REVENUE_SOURCE)}
        )
    assert err is None
    assert view is not None
    assert home_doc.rippleSpec["sources"]["revenue"]["bind"] == "state.revenue"
    assert len(home_doc.widgets) == 1


async def test_authored_source_defaults_refresh(home_doc):
    """A source that omits ``refresh`` gets ``["pocket_open"]`` via the
    SourceBinding model — proves the binding is normalized through the same
    RFC-04 schema the executor parses."""
    source = {"method": "GET", "path": "/revenue/today", "bind": "state.revenue"}
    widget = {**_REVENUE_TILE, "sources": {"revenue": source}}
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        await agent_context.add_widget_for_agent(home_doc.id, widget)
    assert home_doc.rippleSpec["sources"]["revenue"]["refresh"] == ["pocket_open"]


async def test_authored_source_merges_alongside_existing_sources():
    """A new source merges INTO an existing ``rippleSpec.sources`` dict
    without clobbering a sibling — additive merge."""
    doc = _FakeDoc(
        "507f1f77bcf86cd799439022",
        {
            "version": "1.0",
            "state": {"revenue": 0, "prs": []},
            "ui": {"id": "n_root0000", "type": "flex"},
            "sources": {
                "prs": {"method": "GET", "path": "/pulls", "bind": "state.prs"},
            },
        },
    )
    widget = {**_REVENUE_TILE, "sources": {"revenue": dict(_REVENUE_SOURCE)}}
    ctx, _ = _patches(doc, allowed_types=None)
    with ctx:
        result = await agent_context.add_widget_for_agent(doc.id, widget)
    assert result["ok"] is True
    assert set(doc.rippleSpec["sources"]) == {"prs", "revenue"}
    assert doc.rippleSpec["sources"]["prs"]["path"] == "/pulls"
    assert doc.rippleSpec["sources"]["revenue"]["path"] == "/revenue/today"


# ---------------------------------------------------------------------------
# No regression — omitting sources still adds the tile.
# ---------------------------------------------------------------------------


async def test_add_widget_without_sources_still_works(home_doc):
    """A legacy ``add_widget`` call that omits ``sources`` adds the tile and
    never materialises a ``sources`` key."""
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        result = await agent_context.add_widget_for_agent(home_doc.id, dict(_REVENUE_TILE))
    assert result["ok"] is True
    assert len(home_doc.widgets) == 1
    assert "sources" not in home_doc.rippleSpec
    assert home_doc.saves == 1


# ---------------------------------------------------------------------------
# Invalid source handled per the LOGGED (drift-allowed) gate.
# ---------------------------------------------------------------------------


async def test_invalid_source_is_skipped_logged_not_blocking(home_doc):
    """An invalid source binding (missing required ``path``/``bind``) is
    handled per the logged gate: it is skipped, the tile still lands, and a
    valid sibling source in the same batch is persisted. The widget-add is
    NOT blocked the way the strict agent-create gate would block it."""
    widget = {
        **_REVENUE_TILE,
        "sources": {
            "revenue": dict(_REVENUE_SOURCE),
            "broken": {"method": "GET"},  # no path / bind — invalid
        },
    }
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        result = await agent_context.add_widget_for_agent(home_doc.id, widget)

    assert result["ok"] is True
    # The tile landed.
    assert len(home_doc.widgets) == 1
    # The valid source persisted; the invalid one was dropped (logged).
    assert "revenue" in home_doc.rippleSpec["sources"]
    assert "broken" not in home_doc.rippleSpec["sources"]
    assert home_doc.saves == 1


# ---------------------------------------------------------------------------
# REFINE path — update_widget can author sources (feat/home-agent-source-
# authoring). Mirrors the add_widget tests above on the in-place refresh path.
# ---------------------------------------------------------------------------


def _seed_widget(doc: _FakeDoc, widget_id: str, name: str) -> None:
    """Append a minimal widget the update path can target by id."""
    doc.widgets.append(
        SimpleNamespace(
            id=widget_id,
            name=name,
            type="stat",
            icon="",
            color="",
            span=None,
            data=None,
            spec={"type": "stat", "props": {"label": "Revenue", "value": "0"}},
            assignedAgent=None,
            config={},
            props={},
            dataSourceType=None,
        )
    )


async def test_update_widget_for_agent_authors_source(home_doc):
    """The refine fix: ``update_widget_for_agent`` with a ``sources`` block on
    its fields merges the binding into the pocket's top-level
    ``rippleSpec.sources`` AND patches the tile — so refining a static tile can
    make it live."""
    _seed_widget(home_doc, "w_rev", "Live revenue")
    fields = {
        "spec": {"type": "stat", "props": {"label": "Revenue", "value": "{state.revenue}"}},
        "sources": {"revenue": dict(_REVENUE_SOURCE)},
    }
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        result = await agent_context.update_widget_for_agent(home_doc.id, "w_rev", fields)

    assert result["ok"] is True
    assert home_doc.rippleSpec["sources"]["revenue"]["path"] == "/revenue/today"
    assert home_doc.rippleSpec["sources"]["revenue"]["bind"] == "state.revenue"
    # The tile spec was patched too.
    assert home_doc.widgets[0].spec["props"]["value"] == "{state.revenue}"
    assert home_doc.saves == 1


async def test_agent_update_widget_service_authors_source(home_doc):
    """Same merge on the service-layer entry point ``agent_update_widget`` with
    an explicit ``sources`` kwarg."""
    _seed_widget(home_doc, "w_rev", "Live revenue")
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        view, err = await pocket_service.agent_update_widget(
            home_doc.id,
            "w_rev",
            {"name": "Live revenue"},
            sources={"revenue": dict(_REVENUE_SOURCE)},
        )
    assert err is None
    assert view is not None
    assert home_doc.rippleSpec["sources"]["revenue"]["bind"] == "state.revenue"


async def test_update_widget_without_sources_is_noop_on_sources(home_doc):
    """Omitting ``sources`` on update_widget never materialises a sources key —
    no regression for legacy refresh calls."""
    _seed_widget(home_doc, "w_rev", "Live revenue")
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        result = await agent_context.update_widget_for_agent(
            home_doc.id, "w_rev", {"name": "Renamed"}
        )
    assert result["ok"] is True
    assert "sources" not in home_doc.rippleSpec
    assert home_doc.widgets[0].name == "Renamed"


# ---------------------------------------------------------------------------
# Honest tool result — the result reflects what actually persisted re: sources
# so the agent can't report "source authored" when nothing was written.
# ---------------------------------------------------------------------------


async def test_add_widget_result_reports_authored_source_keys(home_doc):
    """``add_widget_for_agent`` returns the authored source keys so the agent
    can honestly confirm the source landed."""
    widget = {**_REVENUE_TILE, "sources": {"revenue": dict(_REVENUE_SOURCE)}}
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        result = await agent_context.add_widget_for_agent(home_doc.id, widget)
    assert result["ok"] is True
    assert result.get("authored_sources") == ["revenue"]
    assert result.get("skipped_sources") == []


async def test_add_widget_result_reports_skipped_invalid_source(home_doc):
    """An invalid binding shows up in ``skipped_sources`` (and NOT in
    ``authored_sources``) so the agent can't claim a source it didn't write."""
    widget = {
        **_REVENUE_TILE,
        "sources": {
            "revenue": dict(_REVENUE_SOURCE),
            "broken": {"method": "GET"},  # no path / bind — invalid
        },
    }
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        result = await agent_context.add_widget_for_agent(home_doc.id, widget)
    assert result["ok"] is True
    assert result.get("authored_sources") == ["revenue"]
    assert result.get("skipped_sources") == ["broken"]


async def test_update_widget_result_reports_authored_source_keys(home_doc):
    """The honest-result fields are present on the refine path too."""
    _seed_widget(home_doc, "w_rev", "Live revenue")
    fields = {"sources": {"revenue": dict(_REVENUE_SOURCE)}}
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        result = await agent_context.update_widget_for_agent(home_doc.id, "w_rev", fields)
    assert result["ok"] is True
    assert result.get("authored_sources") == ["revenue"]


async def test_add_widget_without_sources_omits_honest_result_keys(home_doc):
    """When no ``sources`` are supplied, the honest-result keys are absent (or
    empty) — they only appear when sources were actually in play."""
    ctx, _ = _patches(home_doc, allowed_types=None)
    with ctx:
        result = await agent_context.add_widget_for_agent(home_doc.id, dict(_REVENUE_TILE))
    assert result["ok"] is True
    assert "authored_sources" not in result
