# Pocket tools — CreatePocketTool, AddWidgetTool, RemoveWidgetTool.
# Updated: Added multi-pane pocket support. CreatePocketTool now accepts an
# optional 'panes' parameter for per-pane UISpec trees in multi-pane layouts
# (quad, workspace, split). When 'panes' + 'layout' are provided, each pane
# gets its own UISpec node tree and 'ui'/'widgets' are ignored.
# Also supports 'ui' parameter for single-pane UISpec v1.0 nested component
# trees and flat 'widgets' array for legacy UniversalSpec v2.0 dashboards.

import json
import logging
from datetime import UTC, datetime
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Widget type mapping: old display.type -> Ripple widget type
# ---------------------------------------------------------------------------
_DISPLAY_TYPE_TO_RIPPLE = {
    "stats": "metric",
    "chart": "chart",
    "table": "table",
    "activity": "feed",
    "feed": "feed",
    "metric": "metric",
    "terminal": "terminal",
}

# Map old col-span to Ripple size
_SPAN_TO_SIZE = {
    "col-span-1": "sm",
    "col-span-2": "md",
    "col-span-3": "lg",
}


def _convert_legacy_widget(widget: dict[str, Any], widget_id: str) -> list[dict[str, Any]]:
    """Convert a legacy widget dict to one or more Ripple widget dicts.

    A stats widget with multiple stats becomes multiple metric widgets.
    Everything else maps 1:1.
    """
    display = widget.get("display", {})
    display_type = display.get("type", "metric")
    ripple_type = _DISPLAY_TYPE_TO_RIPPLE.get(display_type, display_type)
    size = _SPAN_TO_SIZE.get(widget.get("span", "col-span-1"), "sm")
    title = widget.get("name", widget.get("title", "Widget"))

    if display_type == "stats":
        # Each stat becomes its own metric widget
        stats = display.get("stats", [])
        if not stats:
            return [{"id": widget_id, "type": "metric", "title": title, "size": size, "data": {}}]
        widgets = []
        for j, stat in enumerate(stats):
            wid = f"{widget_id}-s{j}" if len(stats) > 1 else widget_id
            widgets.append(
                {
                    "id": wid,
                    "type": "metric",
                    "title": stat.get("label", title),
                    "size": "sm",
                    "data": {
                        "value": stat.get("value", ""),
                        "label": stat.get("label", ""),
                        "trend": stat.get("trend", ""),
                    },
                }
            )
        return widgets

    if display_type == "chart":
        data = display.get("bars", display.get("data", []))
        chart_type = display.get("chartType", display.get("type", "bar"))
        if chart_type == "chart":
            chart_type = "bar"
        return [
            {
                "id": widget_id,
                "type": "chart",
                "title": title,
                "size": size,
                "data": [{"label": d.get("label", ""), "value": d.get("value", 0)} for d in data]
                if isinstance(data, list)
                else data,
                "props": {"type": chart_type, "height": 200},
            }
        ]

    if display_type == "table":
        headers = display.get("headers", [])
        rows = display.get("rows", [])
        return [
            {
                "id": widget_id,
                "type": "table",
                "title": title,
                "size": size,
                "data": {
                    "columns": headers,
                    "data": [r.get("cells", r) if isinstance(r, dict) else r for r in rows],
                },
            }
        ]

    if display_type in ("feed", "activity"):
        items = display.get("feedItems", display.get("items", []))
        return [
            {
                "id": widget_id,
                "type": "feed",
                "title": title,
                "size": size,
                "data": {"items": items},
            }
        ]

    if display_type == "metric":
        metric = display.get("metric", {})
        return [
            {
                "id": widget_id,
                "type": "metric",
                "title": metric.get("label", title),
                "size": size,
                "data": {
                    "value": metric.get("value", ""),
                    "label": metric.get("label", ""),
                    "trend": metric.get("trend", ""),
                    "description": metric.get("description", ""),
                },
            }
        ]

    if display_type == "terminal":
        return [
            {
                "id": widget_id,
                "type": "terminal",
                "title": display.get("termTitle", title),
                "size": size,
                "data": {"lines": display.get("termLines", display.get("lines", []))},
                "props": {
                    "title": display.get("termTitle", title),
                    "interactive": display.get("interactive", False),
                },
            }
        ]

    # Fallback — pass through as-is
    return [
        {
            "id": widget_id,
            "type": ripple_type,
            "title": title,
            "size": size,
            "data": display,
        }
    ]


class CreatePocketTool(BaseTool):
    """Create a pocket workspace that outputs a Ripple UniversalSpec.

    The agent calls this tool after gathering information via web_search,
    browser, or other research tools. The tool returns a UniversalSpec
    JSON (v2.0, intent=dashboard) that the frontend renders with
    <Ripple spec={...} />.
    """

    @property
    def name(self) -> str:
        return "create_pocket"

    @property
    def description(self) -> str:
        return (
            "Create a pocket workspace. Two formats available:\n\n"
            "FORMAT 1 — UISpec v1.0 (PREFERRED for rich layouts):\n"
            "Pass a 'ui' parameter with a nested component tree. Supports "
            "flex, grid, heading, text, badge, metric, chart, table, feed, "
            "workflow, image, card, tabs, callout, sources-bar, citation, "
            "source-card, discover-card, follow-up, and more.\n"
            "Each node: {type, props, children?, style?}\n\n"
            "Example UISpec:\n"
            '{"ui":{"type":"flex","props":{"direction":"column","gap":"16px"},'
            '"children":['
            '{"type":"heading","props":{"text":"Revenue Report","level":3}},'
            '{"type":"grid","props":{"columns":3,"gap":"8px"},"children":['
            '{"type":"metric","props":{"label":"Revenue","value":"$10B","trend":"+15%"}},'
            '{"type":"metric","props":{"label":"Users","value":"2.4M","trend":"+8%"}},'
            '{"type":"metric","props":{"label":"NPS","value":"72","trend":"+5"}}'
            "]},"
            '{"type":"chart","props":{"type":"area","height":200,"data":['
            '{"label":"Q1","value":2400},{"label":"Q2","value":3100},'
            '{"label":"Q3","value":3800},{"label":"Q4","value":4500}]}},'
            '{"type":"workflow","props":{"title":"Deploy Pipeline","nodes":['
            '{"id":"t1","type":"trigger","label":"Push"},'
            '{"id":"a1","type":"action","label":"Build"},'
            '{"id":"o1","type":"output","label":"Deploy"}'
            '],"edges":[{"from":"t1","to":"a1"},{"from":"a1","to":"o1"}]}}'
            "]}}\n\n"
            "UISpec node types:\n"
            "- layout: flex, grid, card, tabs, container\n"
            "- display: heading, text, image, badge, metric, avatar, progress, feed\n"
            "- data: chart (sparkline/bar/line/area/pie/donut/candlestick), table\n"
            "- input: button, input, select, checkbox, switch\n"
            "- workflow: node-based DAG with nodes + edges in props\n"
            "- research: source-card, citation, sources-bar, discover-card, "
            "news-card, callout, follow-up\n\n"
            "FORMAT 2 — Flat widgets (simple dashboards):\n"
            "Pass a 'widgets' array for simple grid dashboards.\n"
            "Widget types: metric, chart, table, feed, terminal, text, workflow.\n"
            "Widget sizes: 'sm' (1 col), 'md' (2 cols), 'lg' (full width).\n\n"
            "FORMAT 3 — Multi-Pane UISpec (distinct content per pane):\n"
            "Pass 'panes' dict + 'layout'. Keys are pane IDs for the layout preset.\n"
            "quad pane IDs: tl (top-left), tr (top-right), bl (bottom-left), br (bottom-right).\n"
            "workspace pane IDs: left, right. split pane IDs: top, bottom.\n"
            "Each value is a UISpec node tree.\n\n"
            "WHEN TO USE WHICH:\n"
            "- UISpec (ui): rich layouts, articles, reports, research pages, anything narrative.\n"
            "- Flat widgets: when user asks for 'widgets', 'KPIs', 'dashboard grid', or "
            "a simple set of cards. Use FORMAT 2 with the widgets array.\n"
            "- Multi-pane (panes): when user wants split/quad with DIFFERENT content per pane.\n"
            "- If unsure, default to UISpec. But RESPECT explicit widget requests.\n\n"
            "Workflow nodes: trigger (blue), action (green), condition (orange), "
            "approval (amber), connector (purple), output (teal).\n\n"
            "Colors: #30D158 (green), #FF453A (red), #FF9F0A (orange), "
            "#0A84FF (blue), #BF5AF2 (purple), #5E5CE6 (indigo)."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Pocket title (e.g. 'Vercel Analysis')",
                },
                "description": {
                    "type": "string",
                    "description": "Brief description of the pocket's purpose",
                },
                "category": {
                    "type": "string",
                    "description": "Category: research, business, data, mission, deep-work, custom",
                    "enum": [
                        "research",
                        "business",
                        "data",
                        "mission",
                        "deep-work",
                        "custom",
                        "hospitality",
                    ],
                },
                "layout": {
                    "type": "string",
                    "description": (
                        "Canvas layout preset: dashboard (full screen), "
                        "workspace (page left + widgets right), "
                        "split (widgets top + data bottom), "
                        "quad (2×2 grid). Auto-detected if omitted."
                    ),
                    "enum": ["dashboard", "workspace", "split", "quad"],
                },
                "panes": {
                    "type": "object",
                    "description": (
                        "Per-pane UISpec trees for multi-pane layouts. "
                        "Keys are pane IDs: quad → tl, tr, bl, br; "
                        "workspace → left, right; split → top, bottom. "
                        "Each value is a UISpec node ({type, props, children}). "
                        "Requires 'layout' field. When provided, 'ui' and 'widgets' are ignored."
                    ),
                },
                "ui": {
                    "type": "object",
                    "description": (
                        "UISpec v1.0 nested component tree (PREFERRED). "
                        "Root node with type, props, children. "
                        "Example: {type:'flex', props:{direction:'column', gap:'16px'}, "
                        "children:[{type:'heading', props:{text:'Title', level:3}}, ...]}"
                    ),
                },
                "color": {
                    "type": "string",
                    "description": "Accent color for the pocket (hex, e.g. '#0A84FF')",
                },
                "columns": {
                    "type": "integer",
                    "description": "Grid columns (default 3)",
                },
                "widgets": {
                    "type": "array",
                    "description": "List of Ripple widgets",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": "Widget type: metric, chart, table, "
                                "feed, terminal, text",
                                "enum": [
                                    "metric",
                                    "chart",
                                    "table",
                                    "feed",
                                    "terminal",
                                    "text",
                                    "workflow",
                                ],
                            },
                            "title": {
                                "type": "string",
                                "description": "Widget title",
                            },
                            "size": {
                                "type": "string",
                                "description": "Widget size: sm, md, lg",
                                "enum": ["sm", "md", "lg"],
                            },
                            "data": {
                                "type": "object",
                                "description": "Widget data (shape depends on type)",
                            },
                            "props": {
                                "type": "object",
                                "description": "Optional rendering props (e.g. chart type, height)",
                            },
                        },
                        "required": ["type", "title", "data"],
                    },
                },
            },
            "required": ["title", "description", "category"],
        }

    async def execute(
        self,
        title: str = "",
        description: str = "",
        category: str = "research",
        ui: dict[str, Any] | None = None,
        panes: dict[str, Any] | None = None,
        widgets: list[dict[str, Any]] | None = None,
        layout: str = "",
        color: str = "#0A84FF",
        columns: int = 3,
        name: str = "",
        **kwargs: Any,
    ) -> str:
        """Build and return a pocket spec as JSON."""
        import uuid

        pocket_id = f"ai-{uuid.uuid4().hex[:8]}"
        pocket_title = title or name or "Untitled Pocket"

        metadata = {
            "category": category,
            "color": color,
            "created_at": datetime.now(UTC).isoformat(),
            "pocket_version": "2.0",
        }

        # ── Multi-pane path: per-pane UISpec trees ──
        if panes and isinstance(panes, dict) and layout:
            valid_panes = {k: v for k, v in panes.items() if isinstance(v, dict) and v.get("type")}
            if valid_panes:
                spec: dict[str, Any] = {
                    "version": "1.0",
                    "lifecycle": {"type": "persistent", "id": pocket_id},
                    "title": pocket_title,
                    "description": description,
                    "layout": layout,
                    "panes": valid_panes,
                    "metadata": metadata,
                }
                event_payload = json.dumps({"pocket_event": "created", "spec": spec})
                msg = f"Created pocket **{pocket_title}** ({len(valid_panes)} panes)."
                return f"{event_payload}\n\n{msg}"

        # ── UISpec v1.0 path: nested component tree ──
        if ui and isinstance(ui, dict) and ui.get("type"):
            spec: dict[str, Any] = {
                "version": "1.0",
                "lifecycle": {"type": "persistent", "id": pocket_id},
                "title": pocket_title,
                "description": description,
                "ui": ui,
                "metadata": metadata,
            }
            if layout:
                spec["layout"] = layout
            event_payload = json.dumps({"pocket_event": "created", "spec": spec})
            msg = f"Created pocket **{pocket_title}** (UISpec)."
            return f"{event_payload}\n\n{msg}"

        # ── Flat widgets path: UniversalSpec v2.0 dashboard ──
        widgets = widgets or []

        # Build widget list with IDs
        built_widgets: list[dict[str, Any]] = []
        for i, w in enumerate(widgets):
            wid = f"{pocket_id}-w{i}"

            # If widget already has Ripple 'type' field, use it directly
            if "type" in w and w["type"] in (
                "metric",
                "chart",
                "table",
                "feed",
                "terminal",
                "text",
                "workflow",
            ):
                widget = {
                    "id": w.get("id", wid),
                    "type": w["type"],
                    "title": w.get("title", f"Widget {i + 1}"),
                    "size": w.get("size", "sm"),
                    "data": w.get("data", {}),
                }
                if w.get("props"):
                    widget["props"] = w["props"]
                built_widgets.append(widget)
            elif "display" in w:
                # Legacy format — convert
                converted = _convert_legacy_widget(w, wid)
                built_widgets.extend(converted)
            else:
                # Minimal widget
                built_widgets.append(
                    {
                        "id": wid,
                        "type": w.get("type", "text"),
                        "title": w.get("title", w.get("name", f"Widget {i + 1}")),
                        "size": w.get("size", "sm"),
                        "data": w.get("data", {}),
                    }
                )

        spec = {
            "version": "2.0",
            "intent": "dashboard",
            "lifecycle": {"type": "persistent", "id": pocket_id},
            "title": pocket_title,
            "description": description,
            "display": {"columns": columns},
            "widgets": built_widgets,
            "dashboard_layout": {"type": "grid", "columns": columns, "gap": 10},
            "metadata": metadata,
        }
        if layout:
            spec["layout"] = layout

        # Return structured JSON (first block) + human message (second block).
        # The AgentLoop detects the pocket_event key and publishes a dedicated
        # SystemEvent so the SSE handler receives it without regex/markers.
        event_payload = json.dumps({"pocket_event": "created", "spec": spec})
        msg = f"Created pocket **{pocket_title}** with {len(built_widgets)} widgets."
        return f"{event_payload}\n\n{msg}"


class AddWidgetTool(BaseTool):
    """Add a widget to an existing pocket spec."""

    @property
    def name(self) -> str:
        return "add_widget"

    @property
    def description(self) -> str:
        return (
            "Add a widget to an existing pocket. Provide the pocket_id and the widget spec. "
            "Returns a mutation instruction the frontend applies to the live spec."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "ID of the pocket to add the widget to",
                },
                "widget": {
                    "type": "object",
                    "description": "Widget spec: {type, title, size?, data, props?}",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "metric",
                                "chart",
                                "table",
                                "feed",
                                "terminal",
                                "text",
                                "workflow",
                            ],
                        },
                        "title": {"type": "string"},
                        "size": {"type": "string", "enum": ["sm", "md", "lg"]},
                        "data": {"type": "object"},
                        "props": {"type": "object"},
                    },
                    "required": ["type", "title", "data"],
                },
                "position": {
                    "type": "integer",
                    "description": "Insert position (0-indexed). Omit to append at end.",
                },
            },
            "required": ["pocket_id", "widget"],
        }

    async def execute(
        self,
        pocket_id: str,
        widget: dict[str, Any],
        position: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Return a mutation instruction for adding a widget."""
        import uuid

        widget_id = widget.get("id", f"{pocket_id}-w{uuid.uuid4().hex[:6]}")
        built_widget = {
            "id": widget_id,
            "type": widget.get("type", "text"),
            "title": widget.get("title", "New Widget"),
            "size": widget.get("size", "sm"),
            "data": widget.get("data", {}),
            "props": widget.get("props") or {},
        }

        # Normalize table data: LLM produces {columns:[str], data:[[...]]}
        # but the Ripple Table component needs props.columns=[{accessorKey,header}]
        # and data=[{col: val, ...}] (object rows).
        if built_widget["type"] == "table":
            data = built_widget["data"]
            if isinstance(data, dict) and "columns" in data and "data" in data:
                cols = data["columns"]
                rows = data["data"]
                built_widget["props"]["columns"] = [{"accessorKey": c, "header": c} for c in cols]
                built_widget["data"] = [
                    {cols[ci]: cell for ci, cell in enumerate(row) if ci < len(cols)}
                    for row in rows
                    if isinstance(row, list)
                ]

        mutation = {
            "action": "add_widget",
            "pocket_id": pocket_id,
            "widget": built_widget,
        }
        if position is not None:
            mutation["position"] = position

        event_payload = json.dumps({"pocket_event": "mutation", "mutation": mutation})
        msg = f"Added widget **{built_widget['title']}** to pocket `{pocket_id}`."
        return f"{event_payload}\n\n{msg}"


class RemoveWidgetTool(BaseTool):
    """Remove a widget from an existing pocket spec."""

    @property
    def name(self) -> str:
        return "remove_widget"

    @property
    def description(self) -> str:
        return (
            "Remove a widget from an existing pocket by widget ID. "
            "Returns a mutation instruction the frontend applies."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "ID of the pocket",
                },
                "widget_id": {
                    "type": "string",
                    "description": "ID of the widget to remove",
                },
            },
            "required": ["pocket_id", "widget_id"],
        }

    async def execute(
        self,
        pocket_id: str,
        widget_id: str,
        **kwargs: Any,
    ) -> str:
        """Return a mutation instruction for removing a widget."""
        mutation = {
            "action": "remove_widget",
            "pocket_id": pocket_id,
            "widget_id": widget_id,
        }

        event_payload = json.dumps({"pocket_event": "mutation", "mutation": mutation})
        msg = f"Removed widget `{widget_id}` from pocket `{pocket_id}`."
        return f"{event_payload}\n\n{msg}"
