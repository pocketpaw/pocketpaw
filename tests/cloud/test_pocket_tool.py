# Tests for pocket tools — CreatePocketTool, AddWidgetTool, RemoveWidgetTool.
# Updated: 2026-04-01 — Added UISpec v1.0, multi-pane, and required-fields tests.
# Validates all three pocket formats: UISpec, multi-pane, and flat widgets.

import json

import pytest

from pocketpaw.tools.builtin.pocket import (
    AddWidgetTool,
    CreatePocketTool,
    RemoveWidgetTool,
    _convert_legacy_widget,
)


@pytest.fixture
def create_tool():
    return CreatePocketTool()


@pytest.fixture
def add_tool():
    return AddWidgetTool()


@pytest.fixture
def remove_tool():
    return RemoveWidgetTool()


def _extract_spec(result: str) -> dict:
    """Extract the JSON spec from the tool result.

    Tools return: ``{json_with_pocket_event}\\n\\nhuman message``.
    """
    json_part = result.split("\n\n", 1)[0]
    data = json.loads(json_part)
    assert data.get("pocket_event") == "created", f"Expected pocket_event=created, got: {data}"
    return data["spec"]


def _extract_mutation(result: str) -> dict:
    """Extract the JSON mutation from the tool result."""
    json_part = result.split("\n\n", 1)[0]
    data = json.loads(json_part)
    assert data.get("pocket_event") == "mutation", f"Expected pocket_event=mutation, got: {data}"
    return data["mutation"]


# ---------------------------------------------------------------------------
# CreatePocketTool tests
# ---------------------------------------------------------------------------


class TestCreatePocketTool:
    async def test_returns_universal_spec(self, create_tool):
        result = await create_tool.execute(
            title="Test Dashboard",
            description="A test pocket",
            category="research",
            widgets=[
                {
                    "type": "metric",
                    "title": "Users",
                    "size": "sm",
                    "data": {"value": "1000", "label": "Users"},
                },
            ],
        )
        spec = _extract_spec(result)

        assert spec["version"] == "2.0"
        assert spec["intent"] == "dashboard"
        assert spec["title"] == "Test Dashboard"
        assert spec["description"] == "A test pocket"

    async def test_has_lifecycle(self, create_tool):
        result = await create_tool.execute(
            title="Lifecycle Test",
            description="desc",
            category="data",
            widgets=[],
        )
        spec = _extract_spec(result)

        assert "lifecycle" in spec
        assert spec["lifecycle"]["type"] == "persistent"
        assert spec["lifecycle"]["id"].startswith("ai-")

    async def test_has_metadata(self, create_tool):
        result = await create_tool.execute(
            title="Meta Test",
            description="desc",
            category="business",
            color="#FF453A",
            widgets=[],
        )
        spec = _extract_spec(result)

        assert spec["metadata"]["category"] == "business"
        assert spec["metadata"]["color"] == "#FF453A"
        assert spec["metadata"]["pocket_version"] == "2.0"
        assert "created_at" in spec["metadata"]

    async def test_has_dashboard_layout(self, create_tool):
        result = await create_tool.execute(
            title="Layout Test",
            description="desc",
            category="research",
            columns=4,
            widgets=[],
        )
        spec = _extract_spec(result)

        assert spec["display"]["columns"] == 4
        assert spec["dashboard_layout"]["type"] == "grid"
        assert spec["dashboard_layout"]["columns"] == 4
        assert spec["dashboard_layout"]["gap"] == 10

    async def test_metric_widget(self, create_tool):
        result = await create_tool.execute(
            title="Metrics",
            description="desc",
            category="data",
            widgets=[
                {
                    "type": "metric",
                    "title": "Revenue",
                    "size": "sm",
                    "data": {"value": "$10B", "label": "Revenue", "trend": "+15%"},
                },
            ],
        )
        spec = _extract_spec(result)

        assert len(spec["widgets"]) == 1
        w = spec["widgets"][0]
        assert w["type"] == "metric"
        assert w["title"] == "Revenue"
        assert w["size"] == "sm"
        assert w["data"]["value"] == "$10B"
        assert w["data"]["trend"] == "+15%"

    async def test_chart_widget(self, create_tool):
        result = await create_tool.execute(
            title="Charts",
            description="desc",
            category="data",
            widgets=[
                {
                    "type": "chart",
                    "title": "Sales",
                    "size": "md",
                    "data": [{"label": "Jan", "value": 100}, {"label": "Feb", "value": 200}],
                    "props": {"type": "bar", "height": 200},
                },
            ],
        )
        spec = _extract_spec(result)

        w = spec["widgets"][0]
        assert w["type"] == "chart"
        assert w["props"]["type"] == "bar"
        assert len(w["data"]) == 2

    async def test_table_widget(self, create_tool):
        result = await create_tool.execute(
            title="Tables",
            description="desc",
            category="data",
            widgets=[
                {
                    "type": "table",
                    "title": "Orders",
                    "size": "lg",
                    "data": {"columns": ["ID", "Amount"], "data": [["1", "$50"], ["2", "$75"]]},
                },
            ],
        )
        spec = _extract_spec(result)

        w = spec["widgets"][0]
        assert w["type"] == "table"
        assert w["data"]["columns"] == ["ID", "Amount"]
        assert len(w["data"]["data"]) == 2

    async def test_widget_ids_generated(self, create_tool):
        result = await create_tool.execute(
            title="IDs Test",
            description="desc",
            category="research",
            widgets=[
                {"type": "metric", "title": "A", "data": {"value": "1"}},
                {"type": "metric", "title": "B", "data": {"value": "2"}},
            ],
        )
        spec = _extract_spec(result)

        ids = [w["id"] for w in spec["widgets"]]
        assert len(set(ids)) == 2  # unique IDs
        assert all(id.startswith("ai-") for id in ids)

    async def test_legacy_name_param(self, create_tool):
        """Backward compat: 'name' param maps to 'title'."""
        result = await create_tool.execute(
            name="Legacy Name",
            description="desc",
            category="research",
            widgets=[],
        )
        spec = _extract_spec(result)
        assert spec["title"] == "Legacy Name"

    async def test_multiple_widget_types(self, create_tool):
        result = await create_tool.execute(
            title="Multi",
            description="desc",
            category="research",
            widgets=[
                {"type": "metric", "title": "KPI", "data": {"value": "99%"}},
                {"type": "chart", "title": "Trend", "data": [{"label": "A", "value": 1}]},
                {"type": "table", "title": "Data", "data": {"columns": ["X"], "data": [["y"]]}},
                {"type": "feed", "title": "News", "data": {"items": [{"text": "hello"}]}},
            ],
        )
        spec = _extract_spec(result)
        assert len(spec["widgets"]) == 4
        types = [w["type"] for w in spec["widgets"]]
        assert types == ["metric", "chart", "table", "feed"]

    async def test_result_contains_message(self, create_tool):
        result = await create_tool.execute(
            title="Msg Test",
            description="desc",
            category="research",
            widgets=[
                {"type": "metric", "title": "X", "data": {"value": "1"}},
            ],
        )
        assert "Created pocket **Msg Test** with 1 widgets" in result


# ---------------------------------------------------------------------------
# Legacy widget conversion tests
# ---------------------------------------------------------------------------


class TestLegacyWidgetConversion:
    async def test_legacy_stats_to_metrics(self, create_tool):
        """Legacy stats display with multiple stats should become multiple metric widgets."""
        result = await create_tool.execute(
            title="Legacy Stats",
            description="desc",
            category="research",
            widgets=[
                {
                    "name": "Overview",
                    "span": "col-span-2",
                    "display": {
                        "type": "stats",
                        "stats": [
                            {"label": "Revenue", "value": "$10B", "trend": "+15%"},
                            {"label": "Users", "value": "50K"},
                        ],
                    },
                },
            ],
        )
        spec = _extract_spec(result)
        # 2 stats → 2 metric widgets
        assert len(spec["widgets"]) == 2
        assert all(w["type"] == "metric" for w in spec["widgets"])
        assert spec["widgets"][0]["data"]["value"] == "$10B"

    async def test_legacy_chart_to_chart(self, create_tool):
        result = await create_tool.execute(
            title="Legacy Chart",
            description="desc",
            category="data",
            widgets=[
                {
                    "name": "Revenue",
                    "display": {
                        "type": "chart",
                        "bars": [{"label": "Q1", "value": 100}],
                        "chartType": "bar",
                    },
                },
            ],
        )
        spec = _extract_spec(result)
        assert len(spec["widgets"]) == 1
        assert spec["widgets"][0]["type"] == "chart"
        assert spec["widgets"][0]["props"]["type"] == "bar"

    async def test_legacy_table_to_table(self, create_tool):
        result = await create_tool.execute(
            title="Legacy Table",
            description="desc",
            category="data",
            widgets=[
                {
                    "name": "People",
                    "display": {
                        "type": "table",
                        "headers": ["Name", "Role"],
                        "rows": [{"cells": ["Alice", "CEO"]}],
                    },
                },
            ],
        )
        spec = _extract_spec(result)
        w = spec["widgets"][0]
        assert w["type"] == "table"
        assert w["data"]["columns"] == ["Name", "Role"]

    async def test_legacy_feed_to_feed(self, create_tool):
        result = await create_tool.execute(
            title="Legacy Feed",
            description="desc",
            category="research",
            widgets=[
                {
                    "name": "News",
                    "display": {
                        "type": "feed",
                        "feedItems": [{"text": "Breaking news", "type": "info"}],
                    },
                },
            ],
        )
        spec = _extract_spec(result)
        w = spec["widgets"][0]
        assert w["type"] == "feed"
        assert w["data"]["items"][0]["text"] == "Breaking news"


# ---------------------------------------------------------------------------
# UISpec v1.0 and multi-pane tests
# ---------------------------------------------------------------------------


class TestCreatePocketUISpec:
    async def test_ui_param_produces_v1_spec(self, create_tool):
        result = await create_tool.execute(
            title="UISpec Pocket",
            description="Rich layout",
            category="research",
            ui={
                "type": "flex",
                "props": {"direction": "column", "gap": "16px"},
                "children": [{"type": "heading", "props": {"text": "Title", "level": 3}}],
            },
        )
        spec = _extract_spec(result)
        assert spec["version"] == "1.0"
        assert "ui" in spec
        assert spec["ui"]["type"] == "flex"
        assert "widgets" not in spec

    async def test_ui_takes_precedence_over_widgets(self, create_tool):
        result = await create_tool.execute(
            title="Both",
            description="desc",
            category="research",
            ui={"type": "flex", "props": {}, "children": []},
            widgets=[{"type": "metric", "title": "X", "data": {"value": "1"}}],
        )
        spec = _extract_spec(result)
        assert spec["version"] == "1.0"
        assert "ui" in spec
        assert "widgets" not in spec

    async def test_empty_ui_falls_back_to_widgets(self, create_tool):
        result = await create_tool.execute(
            title="Fallback",
            description="desc",
            category="data",
            ui={},
            widgets=[{"type": "metric", "title": "X", "size": "sm", "data": {"value": "1"}}],
        )
        spec = _extract_spec(result)
        assert spec["version"] == "2.0"
        assert "widgets" in spec

    async def test_multi_pane_spec(self, create_tool):
        result = await create_tool.execute(
            title="Multi Pane",
            description="desc",
            category="data",
            layout="quad",
            panes={
                "tl": {"type": "flex", "props": {}, "children": []},
                "tr": {"type": "heading", "props": {"text": "Charts", "level": 4}},
            },
        )
        spec = _extract_spec(result)
        assert spec["version"] == "1.0"
        assert "panes" in spec
        assert spec["layout"] == "quad"
        assert len(spec["panes"]) == 2
        assert "ui" not in spec
        assert "widgets" not in spec


# ---------------------------------------------------------------------------
# _convert_legacy_widget unit tests
# ---------------------------------------------------------------------------


class TestConvertLegacyWidget:
    def test_stats_single(self):
        widgets = _convert_legacy_widget(
            {"name": "KPI", "display": {"type": "stats", "stats": [{"label": "X", "value": "1"}]}},
            "w0",
        )
        assert len(widgets) == 1
        assert widgets[0]["id"] == "w0"
        assert widgets[0]["type"] == "metric"

    def test_stats_multiple(self):
        widgets = _convert_legacy_widget(
            {
                "name": "KPIs",
                "display": {
                    "type": "stats",
                    "stats": [
                        {"label": "A", "value": "1"},
                        {"label": "B", "value": "2"},
                    ],
                },
            },
            "w0",
        )
        assert len(widgets) == 2
        assert widgets[0]["id"] == "w0-s0"
        assert widgets[1]["id"] == "w0-s1"

    def test_terminal(self):
        widgets = _convert_legacy_widget(
            {
                "name": "Logs",
                "display": {
                    "type": "terminal",
                    "termLines": [{"text": "hello", "type": "stdout"}],
                    "termTitle": "Server Log",
                },
            },
            "w0",
        )
        assert len(widgets) == 1
        assert widgets[0]["type"] == "terminal"
        assert widgets[0]["props"]["title"] == "Server Log"

    def test_metric_single(self):
        widgets = _convert_legacy_widget(
            {
                "name": "KPI",
                "display": {
                    "type": "metric",
                    "metric": {"label": "Revenue", "value": "$10B", "trend": "+5%"},
                },
            },
            "w0",
        )
        assert len(widgets) == 1
        assert widgets[0]["data"]["value"] == "$10B"
        assert widgets[0]["data"]["trend"] == "+5%"

    def test_activity_to_feed(self):
        widgets = _convert_legacy_widget(
            {
                "name": "Activity",
                "display": {"type": "activity", "items": [{"text": "logged in"}]},
            },
            "w0",
        )
        assert len(widgets) == 1
        assert widgets[0]["type"] == "feed"


# ---------------------------------------------------------------------------
# AddWidgetTool tests
# ---------------------------------------------------------------------------


class TestAddWidgetTool:
    async def test_add_widget_returns_mutation(self, add_tool):
        result = await add_tool.execute(
            pocket_id="ai-abc12345",
            widget={"type": "metric", "title": "New KPI", "data": {"value": "42"}},
        )
        mutation = _extract_mutation(result)

        assert mutation["action"] == "add_widget"
        assert mutation["pocket_id"] == "ai-abc12345"
        assert mutation["widget"]["type"] == "metric"
        assert mutation["widget"]["title"] == "New KPI"
        assert mutation["widget"]["data"]["value"] == "42"

    async def test_add_widget_with_position(self, add_tool):
        result = await add_tool.execute(
            pocket_id="ai-abc12345",
            widget={"type": "chart", "title": "Sales", "data": [{"label": "A", "value": 1}]},
            position=2,
        )
        mutation = _extract_mutation(result)
        assert mutation["position"] == 2

    async def test_add_widget_generates_id(self, add_tool):
        result = await add_tool.execute(
            pocket_id="ai-abc12345",
            widget={"type": "text", "title": "Note", "data": {"content": "hello"}},
        )
        mutation = _extract_mutation(result)
        assert mutation["widget"]["id"].startswith("ai-abc12345-w")

    async def test_add_widget_message(self, add_tool):
        result = await add_tool.execute(
            pocket_id="ai-abc12345",
            widget={"type": "metric", "title": "Speed", "data": {"value": "fast"}},
        )
        assert "Added widget **Speed**" in result
        assert "ai-abc12345" in result


# ---------------------------------------------------------------------------
# RemoveWidgetTool tests
# ---------------------------------------------------------------------------


class TestRemoveWidgetTool:
    async def test_remove_widget_returns_mutation(self, remove_tool):
        result = await remove_tool.execute(
            pocket_id="ai-abc12345",
            widget_id="ai-abc12345-w2",
        )
        mutation = _extract_mutation(result)

        assert mutation["action"] == "remove_widget"
        assert mutation["pocket_id"] == "ai-abc12345"
        assert mutation["widget_id"] == "ai-abc12345-w2"

    async def test_remove_widget_message(self, remove_tool):
        result = await remove_tool.execute(
            pocket_id="ai-abc12345",
            widget_id="ai-abc12345-w0",
        )
        assert "Removed widget" in result
        assert "ai-abc12345-w0" in result


# ---------------------------------------------------------------------------
# Tool metadata tests
# ---------------------------------------------------------------------------


class TestToolMetadata:
    def test_create_pocket_name(self, create_tool):
        assert create_tool.name == "create_pocket"

    def test_add_widget_name(self, add_tool):
        assert add_tool.name == "add_widget"

    def test_remove_widget_name(self, remove_tool):
        assert remove_tool.name == "remove_widget"

    def test_all_standard_trust(self, create_tool, add_tool, remove_tool):
        assert create_tool.trust_level == "standard"
        assert add_tool.trust_level == "standard"
        assert remove_tool.trust_level == "standard"

    def test_create_pocket_params_required_fields(self, create_tool):
        params = create_tool.parameters
        assert "title" in params["required"]
        assert "description" in params["required"]
        assert "category" in params["required"]
        assert "widgets" not in params["required"]
        assert "ui" in params["properties"]

    def test_add_widget_params(self, add_tool):
        params = add_tool.parameters
        assert "pocket_id" in params["required"]
        assert "widget" in params["required"]

    def test_remove_widget_params(self, remove_tool):
        params = remove_tool.parameters
        assert "pocket_id" in params["required"]
        assert "widget_id" in params["required"]
