"""Specialist-internal tool wrappers — workspace closure, schema, return shape."""

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.agent.pocket_specialist.tools import (
    make_list_pockets_tool,
    make_persist_pocket_tool,
    make_validate_spec_tool,
)


class TestListPocketsTool:
    @pytest.mark.asyncio
    async def test_closes_over_workspace_and_user(self):
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._agent_list_pockets",
            new=AsyncMock(return_value=[{"id": "p1", "name": "X"}]),
        ) as mocked:
            tool = make_list_pockets_tool(workspace_id="ws-1", user_id="user-A")
            result = await tool.ainvoke({})
            mocked.assert_awaited_once_with("ws-1", "user-A")
            assert result == [{"id": "p1", "name": "X"}]


class TestValidateSpecTool:
    @pytest.mark.asyncio
    async def test_returns_warnings_list(self):
        # Fake manifest declaring `timeline` with `events`/`maxItems` but
        # NOT `maxItem`, so a `timeline` with `maxItem` triggers an
        # unknown-prop warning.
        fake_manifest = {
            "schema": "ripple.manifest/v1",
            "widgets": [
                {"type": "timeline", "props": {"events": {}, "maxItems": {}}},
                {"type": "text", "props": {"value": {}}},
            ],
        }
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._get_manifest",
            new=AsyncMock(return_value=fake_manifest),
        ):
            tool = make_validate_spec_tool()
            bad_spec = {
                "version": "1.0",
                "ui": {
                    "type": "timeline",
                    "props": {"events": [], "maxItem": 5},
                },
            }
            result = await tool.ainvoke({"spec": bad_spec})
            assert result["ok"] is False
            assert len(result["warnings"]) > 0

    @pytest.mark.asyncio
    async def test_clean_spec_returns_ok(self):
        fake_manifest = {
            "schema": "ripple.manifest/v1",
            "widgets": [
                {"type": "input", "props": {"value": {}}},
                {"type": "text", "props": {"value": {}}},
            ],
        }
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._get_manifest",
            new=AsyncMock(return_value=fake_manifest),
        ):
            tool = make_validate_spec_tool()
            good_spec = {
                "version": "1.0",
                "state": {"name": ""},
                "ui": {"type": "input", "props": {"value": "{state.name}"}},
            }
            result = await tool.ainvoke({"spec": good_spec})
            assert result["ok"] is True
            assert result["warnings"] == []


class TestPersistPocketTool:
    @pytest.mark.asyncio
    async def test_create_path(self):
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._agent_create",
            new=AsyncMock(return_value=({"id": "new-1", "name": "Created"}, "new-1", None)),
        ) as mocked:
            tool = make_persist_pocket_tool(workspace_id="ws-1", user_id="user-A")
            result = await tool.ainvoke(
                {
                    "name": "Created",
                    "ripple_spec": {"version": "1.0", "ui": {"type": "text"}},
                }
            )
            mocked.assert_awaited_once()
            kwargs = mocked.await_args.kwargs
            assert kwargs["workspace_id"] == "ws-1"
            assert kwargs["owner_id"] == "user-A"
            assert kwargs["name"] == "Created"
            assert result["id"] == "new-1"

    @pytest.mark.asyncio
    async def test_update_path(self):
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._agent_update",
            new=AsyncMock(return_value=({"id": "p1", "name": "Updated"}, None)),
        ) as mocked:
            tool = make_persist_pocket_tool(workspace_id="ws-1", user_id="user-A")
            result = await tool.ainvoke(
                {
                    "target_pocket_id": "p1",
                    "ripple_spec": {"version": "1.0", "ui": {"type": "text"}},
                }
            )
            mocked.assert_awaited_once()
            call = mocked.await_args
            assert call.kwargs.get("pocket_id") == "p1"
            assert result["id"] == "p1"


# A manifest mirroring the marketing/scaffold widgets a Paw Site uses. None
# declare id/name/href in per-widget props (matches the live manifest) — those
# are universal node-level props the renderer wraps on.
_SITE_MANIFEST = {
    "schema": "ripple.manifest/v1",
    "widgets": [
        {"type": "section", "props": {"title": {}}},
        {"type": "card", "props": {"title": {}}},
        {"type": "input", "props": {"label": {}, "type": {}}},
        {"type": "textarea", "props": {"label": {}}},
        {"type": "button", "props": {"label": {}, "type": {}}},
        {"type": "navbar", "props": {"brand": {}, "links": {}, "cta": {}, "ctaHref": {}}},
        {"type": "feature-grid", "props": {"columns": {}, "features": {}}},
        {"type": "testimonial", "props": {"quote": {}, "author": {}, "role": {}}},
        {"type": "cta", "props": {"headline": {}, "button": {}, "href": {}}},
        {"type": "footer", "props": {"columns": {}, "copyright": {}}},
    ],
}

# A spliced landing skeleton (trimmed) carrying the SSR-essential props that
# the default manifest validator flags: section.id (anchors), input/textarea
# .name (native POST), card.id (anchor). This is the exact shape that was
# being downgraded to generics.
_SITE_SPEC = {
    "version": "1.0",
    "ui": {
        "type": "flex",
        "children": [
            {"type": "navbar", "props": {"brand": "X", "cta": "Book", "ctaHref": "#book"}},
            {
                "type": "section",
                "props": {"id": "services"},
                "children": [{"type": "feature-grid", "props": {"columns": 3, "features": []}}],
            },
            {"type": "testimonial", "props": {"quote": "q", "author": "a", "role": "r"}},
            {"type": "cta", "props": {"headline": "Go", "button": "Book", "href": "#book"}},
            {
                "type": "card",
                "props": {"id": "book", "title": "Book"},
                "children": [
                    {"type": "input", "props": {"name": "email", "type": "email"}},
                    {"type": "textarea", "props": {"name": "message"}},
                    {"type": "button", "props": {"label": "Send", "type": "submit"}},
                ],
            },
            {"type": "footer", "props": {"columns": [], "copyright": "c"}},
        ],
    },
}


class TestPersistPocketSiteAware:
    """The site-aware trust path: a spliced marketing skeleton (type='site' /
    pattern='landing') with renderer-honored SSR props must NOT trip the
    redraft loop. In dashboard mode the same props DO trip it — proving the
    relaxation is gated on site mode and doesn't weaken non-site validation."""

    @pytest.mark.asyncio
    async def test_site_skeleton_persists_without_redraft(self):
        """type='site' + the SSR props → persists (no redraft short-circuit).
        This is the regression fix: the marketing widgets survive."""
        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.tools._get_manifest",
                new=AsyncMock(return_value=_SITE_MANIFEST),
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.tools._agent_create",
                new=AsyncMock(return_value=({"id": "site-1", "name": "Landing"}, "site-1", None)),
            ) as created,
        ):
            capture: dict = {}
            tool = make_persist_pocket_tool(workspace_id="ws-1", user_id="user-A", capture=capture)
            result = await tool.ainvoke(
                {
                    "name": "Landing",
                    "type": "site",
                    "pattern": "landing",
                    "ripple_spec": _SITE_SPEC,
                }
            )
            # Persisted — not a redraft short-circuit.
            created.assert_awaited_once()
            assert result.get("redraft_required") is not True
            assert result["id"] == "site-1"
            # The SSR props produced no blocking warnings.
            assert capture.get("warnings") == []

    @pytest.mark.asyncio
    async def test_dashboard_spec_with_ssr_props_still_redrafts(self):
        """Same SSR props, but WITHOUT site mode → the validator flags them and
        the tool short-circuits to redraft on attempt 1 (unchanged behavior).
        This proves the relaxation is gated and dashboards are unaffected."""
        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.tools._get_manifest",
                new=AsyncMock(return_value=_SITE_MANIFEST),
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.tools._agent_create",
                new=AsyncMock(return_value=({"id": "x", "name": "x"}, "x", None)),
            ) as created,
        ):
            capture: dict = {}
            tool = make_persist_pocket_tool(workspace_id="ws-1", user_id="user-A", capture=capture)
            result = await tool.ainvoke(
                {
                    "name": "Dash",
                    "ripple_spec": _SITE_SPEC,  # no type/pattern → dashboard path
                }
            )
            # Short-circuited to redraft — NOT persisted.
            created.assert_not_awaited()
            assert result.get("redraft_required") is True
            assert result["warnings"], "dashboard mode must still surface the prop warnings"
