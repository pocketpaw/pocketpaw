# Pocket chat router — dedicated endpoint for pocket creation.
# Updated: 2026-04-01 — _prepare_pocket_spec now handles UISpec v1.0, multi-pane specs,
#   and flat widget specs. System prompt teaches all three formats with UISpec as default.
#   Debug logging added to trace pocket event pipeline.

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.chat import ChatRequest

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Pockets"],
    dependencies=[Depends(require_scope("chat"))],
)

_WS_PREFIX = "websocket_"

# Match the JSON arg passed to create_pocket in a Bash command (legacy fallback)
_CREATE_POCKET_RE = re.compile(r"create_pocket\s+'(.*?)'", re.DOTALL)

_POCKET_SYSTEM_CONTEXT = """\
<pocket-creation-context>
You are running inside PocketPaw OS, a desktop workspace app.
The user wants a "pocket" — a themed workspace with data widgets.

HOW TO USE POCKET TOOLS:
Invoke pocket tools by piping JSON via stdin (prevents bash from mangling $ signs in prices):
  echo '<JSON>' | python -m pocketpaw.tools.cli create_pocket
  echo '<JSON>' | python -m pocketpaw.tools.cli add_widget
  echo '<JSON>' | python -m pocketpaw.tools.cli remove_widget

CRITICAL: Always use echo with SINGLE QUOTES, never double quotes!
Double quotes cause bash to expand $74.30 → 4.30 (bash eats the $ and next digit).

Two formats for create_pocket:

FORMAT 1 — UISpec v1.0 (PREFERRED for rich layouts):
Pass a 'ui' parameter with a nested component tree. Each node: {type, props, children?, style?}
Node types: flex, grid, heading, text, badge, metric, chart, table, feed, workflow, image, card,
tabs, callout, sources-bar, citation, source-card, discover-card, follow-up, container, button,
input, select, checkbox, switch, avatar, progress.

Example (UISpec):
  echo '{"title":"Revenue Report","description":"Q4 analysis","category":"business","ui":{"type":"flex","props":{"direction":"column","gap":"16px"},"children":[{"type":"heading","props":{"text":"Revenue Report","level":3}},{"type":"grid","props":{"columns":3,"gap":"8px"},"children":[{"type":"metric","props":{"label":"Revenue","value":"$10B","trend":"+15%"}},{"type":"metric","props":{"label":"Users","value":"2.4M","trend":"+8%"}},{"type":"metric","props":{"label":"NPS","value":"72","trend":"+5"}}]},{"type":"chart","props":{"type":"area","height":200,"data":[{"label":"Q1","value":2400},{"label":"Q2","value":3100},{"label":"Q3","value":3800},{"label":"Q4","value":4500}]}}]}}' | python -m pocketpaw.tools.cli create_pocket

FORMAT 2 — Flat widgets (simple dashboards):
Pass a 'widgets' array for simple grid dashboards.
Widget types: metric, chart, table, feed, terminal, text, workflow.
Widget sizes: "sm" (1 col), "md" (2 cols), "lg" (full width).

Example (flat widgets):
  echo '{"title":"My Pocket","description":"Demo","category":"research","widgets":[{"type":"metric","title":"Users","size":"sm","data":{"value":"10K","label":"Total Users","trend":"+5%"}}]}' | python -m pocketpaw.tools.cli create_pocket

FORMAT 3 — Multi-Pane UISpec (distinct content per pane):
Pass 'panes' dict + 'layout'. Keys are pane IDs for the layout preset.
quad pane IDs: tl (top-left), tr (top-right), bl (bottom-left), br (bottom-right).
workspace pane IDs: left, right. split pane IDs: top, bottom.
Each value is a UISpec node tree.

Example (SOC multi-pane):
  echo '{"title":"SOC Overview","description":"Security ops","category":"mission","layout":"quad","panes":{"tl":{"type":"flex","props":{"direction":"column"},"children":[{"type":"heading","props":{"text":"Alerts","level":4}},{"type":"feed","props":{"items":[{"text":"Brute-force detected","type":"error"},{"text":"New IP flagged","type":"warning"}]}}]},"tr":{"type":"chart","props":{"type":"donut","data":[{"label":"Critical","value":3},{"label":"High","value":12},{"label":"Medium","value":45}]}},"bl":{"type":"table","props":{"columns":["IP","Country","Hits"],"data":[["1.2.3.4","CN","892"],["5.6.7.8","RU","341"]]}},"br":{"type":"flex","props":{},"children":[{"type":"metric","props":{"label":"Uptime","value":"99.97%","trend":"+0.02%"}},{"type":"metric","props":{"label":"Blocked","value":"1,247","trend":"+89"}}]}}}' | python -m pocketpaw.tools.cli create_pocket

POCKET LAYOUT SYSTEM:
Layouts control how the canvas arranges content:
- dashboard: full-screen widget grid (default for flat widgets)
- workspace: page/article left + widgets right (good for UISpec research)
- split: widgets top + data/detail bottom
- quad: 2×2 grid, each pane independent (requires 'panes' parameter)

How to choose:
- UISpec (ui): rich layouts, articles, reports, research pages, anything narrative.
- Flat widgets: when user asks for 'widgets', 'KPIs', 'dashboard grid', or a simple set of cards.
- Multi-pane (panes): when user wants split/quad with DIFFERENT content per pane.
- If unsure, default to UISpec. But RESPECT explicit widget requests.

COMPOSING RICH POCKETS:
Use UISpec for multi-section layouts. Nest flex/grid for structure, then leaf nodes for content.
Common patterns:
  - Header + metrics row + chart: flex(column) > [heading, grid(3) > [metric, metric, metric], chart]
  - Article with sidebar: grid(2, "2fr 1fr") > [flex(column) > [...content], flex(column) > [...sidebar]]
  - Research page: flex(column) > [heading, sources-bar, text, chart, callout, follow-up]


Example (add widget to existing pocket):
  echo '{"pocket_id":"ai-abc123","widget":{"type":"chart","title":"Growth","size":"md","data":[{"label":"Q1","value":100},{"label":"Q2","value":200}],"props":{"type":"line"}}}' | python -m pocketpaw.tools.cli add_widget

Example (remove widget):
  echo '{"pocket_id":"ai-abc123","widget_id":"ai-abc123-w2"}' | python -m pocketpaw.tools.cli remove_widget

RULES:
1. Do in-depth research FIRST using a MULTI-AGENT approach:
   - Spawn PARALLEL web_search calls for different aspects of the topic.
   - For a company: run separate searches for financials, products, leadership, news, competitors.
   - For a topic: run separate searches for stats, trends, key players, recent events, forecasts.
   - Aim for 4-6 parallel searches covering distinct angles. Do NOT do one search at a time.
   - After initial results, do follow-up searches to fill gaps or verify numbers.
2. Use ONLY the CLI bridge above for pocket operations. Always pipe JSON via echo with SINGLE QUOTES.
3. NEVER use curl, fetch, HTTP requests, or REST API calls to manage pockets.
   NEVER try to access /api/v1/pockets or any HTTP endpoints. Use the CLI bridge above.
4. NEVER create HTML files or write files to disk.
5. Every widget/metric MUST have real, concrete data values — never leave data empty, null,
   or with placeholder text like "N/A", "TBD", or "...". If you cannot find a specific
   number, use your best estimate and note it with "~" prefix (e.g. "~$5B").
6. Charts MUST have at least 3 data points with numeric values > 0.
7. Tables MUST have at least 2 rows of real data.
8. Feeds MUST have at least 3 items with actual text content.

Logo: When creating a pocket for a known company or brand, include a "logo" field with their
icon URL from https://cdn.simpleicons.org/{brand-slug}/{color-hex-without-hash}
(e.g. "https://cdn.simpleicons.org/stripe/white", "https://cdn.simpleicons.org/slack/white").
For generic/non-brand pockets, omit the logo field.

Colors: #30D158 (green), #FF453A (red), #FF9F0A (orange), #0A84FF (blue), #BF5AF2 (purple), #5E5CE6 (indigo).

MODIFYING EXISTING POCKETS:
When a <current-pocket> tag is present in the user message, you are editing that pocket.
- To ADD a widget: echo '{"pocket_id":"<id>","widget":{...}}' | python -m pocketpaw.tools.cli add_widget
- To REMOVE a widget: echo '{"pocket_id":"<id>","widget_id":"<wid>"}' | python -m pocketpaw.tools.cli remove_widget
- To RECREATE the entire pocket: echo '{"title":...}' | python -m pocketpaw.tools.cli create_pocket
  (use 'ui' for UISpec, 'widgets' for flat dashboard, 'panes'+'layout' for multi-pane)
- The pocket id and widget ids are provided in the <current-pocket> tag.
- Do NOT use HTTP/curl/fetch — only use the CLI bridge commands above.
</pocket-creation-context>

"""


def _extract_chat_id(session_id: str | None) -> str:
    """Extract raw chat_id from a client-supplied session_id.

    Reuses the same logic as chat.py so session IDs are compatible.
    """
    from pocketpaw.api.v1.chat import _extract_chat_id as _chat_extract
    return _chat_extract(session_id)


def _to_safe_key(chat_id: str) -> str:
    from pocketpaw.api.v1.chat import _to_safe_key as _chat_safe_key
    return _chat_safe_key(chat_id)


def _try_extract_pocket_from_bash(command: str) -> dict | None:
    """Extract pocket spec JSON from a create_pocket Bash command."""
    match = _CREATE_POCKET_RE.search(command)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return None


def _prepare_widget(w: dict, pocket_id: str, index: int, color: str = "#0A84FF") -> dict | None:
    """Transform a single raw widget dict into Ripple-ready format.

    Returns the transformed widget or None if the widget is unusable.
    """
    if not isinstance(w, dict):
        return None

    wtype = w.get("type", "text")
    data = w.get("data")
    title = w.get("title") or w.get("name") or f"Widget {index + 1}"
    wid = w.get("id") or f"{pocket_id}-w{index}"

    if data is None:
        logger.warning("Dropping widget %r: no data", title)
        return None

    rw: dict = {
        "id": wid,
        "type": wtype,
        "title": title,
        "size": w.get("size", "sm"),
        "props": {**(w.get("props") or {}), "color": w.get("color") or color},
    }

    if wtype == "metric":
        if not isinstance(data, dict) or not data.get("value"):
            logger.warning("Dropping metric %r: missing value", title)
            return None
        rw["data"] = data

    elif wtype == "chart":
        if not isinstance(data, list) or len(data) == 0:
            logger.warning("Dropping chart %r: empty data", title)
            return None
        cleaned = []
        for pt in data:
            if isinstance(pt, dict) and pt.get("label") and pt.get("value") is not None:
                try:
                    cleaned.append({"label": pt["label"], "value": float(pt["value"])})
                except (ValueError, TypeError):
                    pass
        if len(cleaned) < 2:
            logger.warning("Dropping chart %r: <2 valid points", title)
            return None
        rw["data"] = cleaned

    elif wtype == "table":
        if not isinstance(data, dict):
            logger.warning("Dropping table %r: data not object", title)
            return None
        cols = data.get("columns", [])
        rows = data.get("data", [])
        if not cols or not rows:
            logger.warning("Dropping table %r: empty columns/rows", title)
            return None
        rw["props"]["columns"] = [{"key": c, "label": c} for c in cols]
        rw["data"] = [
            {cols[ci]: cell for ci, cell in enumerate(row) if ci < len(cols)}
            for row in rows
            if isinstance(row, list)
        ]

    elif wtype == "feed":
        if isinstance(data, dict):
            items = data.get("items", [])
        elif isinstance(data, list):
            items = data
        else:
            logger.warning("Dropping feed %r: bad data shape", title)
            return None
        items = [it for it in items if isinstance(it, dict) and it.get("text")]
        if not items:
            logger.warning("Dropping feed %r: no items", title)
            return None
        rw["data"] = items

    elif wtype == "text":
        if isinstance(data, dict) and "content" in data:
            rw["data"] = str(data["content"])
        else:
            rw["data"] = str(data) if data else ""

    elif wtype == "terminal":
        if isinstance(data, dict) and "lines" in data:
            rw["data"] = data["lines"]
        else:
            rw["data"] = data

    else:
        rw["data"] = data

    return rw


def _prepare_pocket_spec(spec: dict) -> dict | None:
    """Validate AI spec and transform into a render-ready format.

    Handles three formats:
    1. Multi-pane UISpec (panes dict)
    2. UISpec v1.0 (ui tree)
    3. Flat widgets (UniversalSpec v2.0 dashboard)

    Returns a complete, render-ready spec or None if the spec is unusable.
    """
    if not isinstance(spec, dict):
        return None

    name = spec.get("title") or spec.get("name")
    if not name:
        return None

    # ── Multi-pane path: per-pane UISpec trees ──
    panes = spec.get("panes")
    if isinstance(panes, dict) and panes:
        pocket_id = spec.get("id") or spec.get("lifecycle", {}).get("id") or f"pocket-{uuid.uuid4().hex[:8]}"
        meta = spec.get("metadata") or {}
        color = spec.get("color") or meta.get("color", "#0A84FF")
        result: dict[str, Any] = {
            "version": "1.0",
            "lifecycle": {"type": "persistent", "id": pocket_id},
            "title": name,
            "name": name,
            "description": spec.get("description", ""),
            "category": spec.get("category") or meta.get("category", "custom"),
            "color": color,
            "logo": spec.get("logo") or meta.get("logo"),
            "layout": spec.get("layout", "quad"),
            "panes": panes,
            "metadata": {
                "category": spec.get("category") or meta.get("category", "custom"),
                "color": color,
                "logo": spec.get("logo") or meta.get("logo"),
            },
        }
        logger.info("Pocket multi-pane prepared: %s (%d panes)", name, len(panes))
        return result

    # ── UISpec v1.0 path: nested component tree ──
    ui_tree = spec.get("ui")
    if isinstance(ui_tree, dict) and ui_tree.get("type"):
        pocket_id = spec.get("id") or spec.get("lifecycle", {}).get("id") or f"pocket-{uuid.uuid4().hex[:8]}"
        meta = spec.get("metadata") or {}
        color = spec.get("color") or meta.get("color", "#0A84FF")
        result: dict[str, Any] = {
            "version": "1.0",
            "lifecycle": {"type": "persistent", "id": pocket_id},
            "title": name,
            "name": name,
            "description": spec.get("description", ""),
            "category": spec.get("category") or meta.get("category", "custom"),
            "color": color,
            "logo": spec.get("logo") or meta.get("logo"),
            "ui": ui_tree,
            "metadata": {
                "category": spec.get("category") or meta.get("category", "custom"),
                "color": color,
                "logo": spec.get("logo") or meta.get("logo"),
            },
        }
        if spec.get("layout"):
            result["layout"] = spec["layout"]
        logger.info("Pocket UISpec prepared: %s", name)
        return result

    # ── Flat widgets path: UniversalSpec v2.0 dashboard ──
    raw_widgets = spec.get("widgets")
    if not isinstance(raw_widgets, list) or len(raw_widgets) == 0:
        return None

    pocket_id = spec.get("id") or f"pocket-{uuid.uuid4().hex[:8]}"
    color = spec.get("color") or "#0A84FF"
    meta = spec.get("metadata") or {}

    widgets = []
    for i, w in enumerate(raw_widgets):
        if not isinstance(w, dict):
            continue

        wtype = w.get("type", "text")
        data = w.get("data")
        title = w.get("title") or w.get("name") or f"Widget {i + 1}"
        wid = w.get("id") or f"{pocket_id}-w{i}"

        # Skip widgets with no data
        if data is None:
            logger.warning("Dropping widget %r: no data", title)
            continue

        # Build the Ripple-ready widget
        rw: dict = {
            "id": wid,
            "type": wtype,
            "title": title,
            "size": w.get("size", "sm"),
            "props": {**(w.get("props") or {}), "color": w.get("color") or color},
        }

        # ── Transform data per type into exactly what Ripple components expect ──

        if wtype == "metric":
            if not isinstance(data, dict) or not data.get("value"):
                logger.warning("Dropping metric %r: missing value", title)
                continue
            rw["data"] = data

        elif wtype == "chart":
            if not isinstance(data, list) or len(data) == 0:
                logger.warning("Dropping chart %r: empty data", title)
                continue
            cleaned = []
            for pt in data:
                if isinstance(pt, dict) and pt.get("label") and pt.get("value") is not None:
                    try:
                        cleaned.append({"label": pt["label"], "value": float(pt["value"])})
                    except (ValueError, TypeError):
                        pass
            if len(cleaned) < 2:
                logger.warning("Dropping chart %r: <2 valid points", title)
                continue
            rw["data"] = cleaned

        elif wtype == "table":
            if not isinstance(data, dict):
                logger.warning("Dropping table %r: data not object", title)
                continue
            cols = data.get("columns", [])
            rows = data.get("data", [])
            if not cols or not rows:
                logger.warning("Dropping table %r: empty columns/rows", title)
                continue
            rw["props"]["columns"] = [{"accessorKey": c, "header": c} for c in cols]
            # Rows may be lists (LLM-generated) or dicts (from data sources like MongoDB)
            processed_rows = []
            for row in rows:
                if isinstance(row, list):
                    processed_rows.append(
                        {cols[ci]: cell for ci, cell in enumerate(row) if ci < len(cols)}
                    )
                elif isinstance(row, dict):
                    processed_rows.append(row)
            rw["data"] = processed_rows

        elif wtype == "feed":
            if isinstance(data, dict):
                items = data.get("items", [])
            elif isinstance(data, list):
                items = data
            else:
                logger.warning("Dropping feed %r: bad data shape", title)
                continue
            items = [it for it in items if isinstance(it, dict) and it.get("text")]
            if not items:
                logger.warning("Dropping feed %r: no items", title)
                continue
            rw["data"] = items

        elif wtype == "text":
            if isinstance(data, dict) and "content" in data:
                rw["data"] = str(data["content"])
            else:
                rw["data"] = str(data) if data else ""

        elif wtype == "terminal":
            if isinstance(data, dict) and "lines" in data:
                rw["data"] = data["lines"]
            else:
                rw["data"] = data

        else:
            rw["data"] = data

        widgets.append(rw)

    if not widgets:
        logger.warning("Pocket %r: no valid widgets", name)
        return None

    # Build the complete Ripple UniversalSpec v2.0
    result_spec: dict[str, Any] = {
        "version": "2.0",
        "intent": "dashboard",
        "lifecycle": {"type": "persistent", "id": pocket_id},
        "title": name,
        "name": name,
        "description": spec.get("description", ""),
        "category": spec.get("category") or meta.get("category", "custom"),
        "color": color,
        "logo": spec.get("logo") or meta.get("logo"),
        "display": {"columns": spec.get("columns", 3)},
        "widgets": widgets,
        "dashboard_layout": {
            "type": "masonry",
            "columns": spec.get("columns", 3),
            "gap": 10,
        },
        "metadata": {
            "category": spec.get("category") or meta.get("category", "custom"),
            "color": color,
            "logo": spec.get("logo") or meta.get("logo"),
        },
    }
    if spec.get("layout"):
        result_spec["layout"] = spec["layout"]
    return result_spec


@router.post("/pockets/chat")
async def pocket_chat_stream(body: ChatRequest):
    """Chat with pocket context — extracts pocket specs."""
    from pocketpaw.api.v1.chat import _APISessionBridge
    from pocketpaw.bus import get_message_bus
    from pocketpaw.bus.events import Channel, InboundMessage

    chat_id = _extract_chat_id(body.session_id)
    safe_key = _to_safe_key(chat_id)

    # Build metadata with pocket context if provided
    meta: dict = {
        "source": "pocket_chat",
        "pocket_system_context": _POCKET_SYSTEM_CONTEXT,
    }
    if body.pocket_context:
        meta["pocket_context"] = body.pocket_context.model_dump(exclude_none=True)

    # Subscribe bridge BEFORE publishing the message — otherwise the agent
    # can process and respond before the bridge is listening, causing chunks
    # and stream_end to be lost (race condition).
    bridge = _APISessionBridge(chat_id)
    await bridge.start()

    msg = InboundMessage(
        channel=Channel.WEBSOCKET,
        sender_id=chat_id,
        chat_id=chat_id,
        content=body.content,
        media=body.media,
        metadata=meta,
    )
    bus = get_message_bus()
    await bus.publish_inbound(msg)

    pocket_emitted = False

    async def _event_generator():
        nonlocal pocket_emitted
        try:
            yield (f"event: stream_start\ndata: {json.dumps({'session_id': safe_key})}\n\n")
            while True:
                try:
                    event = await asyncio.wait_for(bridge.queue.get(), timeout=1.0)
                except TimeoutError:
                    continue

                etype = event["event"]
                edata = event["data"]

                # Pocket events arrive as dedicated event types from the
                # AgentLoop (no regex/marker extraction needed).
                if etype == "pocket_created" and not pocket_emitted:
                    spec = edata.get("spec", {})
                    logger.debug(
                        "pocket_created event received: title=%r, has_ui=%s, has_widgets=%s, has_panes=%s",
                        spec.get("title"),
                        "ui" in spec,
                        "widgets" in spec,
                        "panes" in spec,
                    )
                    spec = _prepare_pocket_spec(spec)
                    if spec:
                        pocket_emitted = True
                        fmt = "UISpec" if "ui" in spec else f"{len(spec.get('panes', {}))} panes" if "panes" in spec else f"{len(spec.get('widgets', []))} widgets"
                        logger.info("Pocket created: %s (%s)", spec.get("title", "?"), fmt)
                        pocket_cloud_id = edata.get("pocket_cloud_id")
                        yield (f"event: pocket_created\ndata: {json.dumps({'spec': spec, 'session_id': safe_key, 'pocket_cloud_id': pocket_cloud_id})}\n\n")
                    else:
                        logger.warning(
                            "pocket_created event dropped — _prepare_pocket_spec returned None for title=%r",
                            edata.get("spec", {}).get("title"),
                        )
                    continue

                if etype == "pocket_mutation":
                    mutation = edata.get("mutation", {})
                    if mutation:
                        yield (
                            f"event: pocket_mutation\n"
                            f"data: {json.dumps(mutation)}\n\n"
                        )
                    continue

                # Legacy fallback: extract pocket spec from Bash tool_start
                if etype == "tool_start" and not pocket_emitted:
                    cmd = ""
                    inp = edata.get("input") or edata.get("params") or {}
                    if isinstance(inp, dict):
                        cmd = inp.get("command", "")
                    elif isinstance(inp, str):
                        cmd = inp

                    # Detect create_pocket
                    if "create_pocket" in cmd and not pocket_emitted:
                        spec = _try_extract_pocket_from_bash(cmd)
                        spec = _prepare_pocket_spec(spec) if spec else None
                        if spec:
                            pocket_emitted = True
                            logger.info(
                                "Pocket extracted from tool_start: %s (%d widgets)",
                                spec.get("title", spec.get("name", "?")),
                                len(spec.get("widgets", [])),
                            )
                            yield (f"event: pocket_created\ndata: {json.dumps({'spec': spec, 'session_id': safe_key})}\n\n")

                # Forward original event
                yield (f"event: {etype}\ndata: {json.dumps(edata)}\n\n")

                if etype in ("stream_end", "error"):
                    break
        finally:
            await bridge.stop()

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
