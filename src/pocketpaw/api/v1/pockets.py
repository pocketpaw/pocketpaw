# Pocket chat router — dedicated endpoint for pocket creation.

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ee.ripple import POCKET_CREATION_PROMPT, POCKET_INTERACTION_PROMPT
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
        pocket_id = (
            spec.get("id")
            or spec.get("lifecycle", {}).get("id")
            or f"pocket-{uuid.uuid4().hex[:8]}"
        )
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
        pocket_id = (
            spec.get("id")
            or spec.get("lifecycle", {}).get("id")
            or f"pocket-{uuid.uuid4().hex[:8]}"
        )
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

    # Pick the right instructions based on whether the user is creating a
    # new pocket or interacting with an existing one. Sending creation docs
    # while the user is asking questions inside a live pocket is what caused
    # the agent to respond with "the pocket is empty" and to attempt to
    # re-create pockets from scratch.
    is_interaction = bool(body.pocket_context and body.pocket_context.id)
    pocket_ctx = POCKET_INTERACTION_PROMPT if is_interaction else POCKET_CREATION_PROMPT

    meta: dict = {
        "source": "pocket_chat",
        "pocket_system_context": pocket_ctx,
    }
    if body.pocket_context:
        # Only the small descriptor travels through metadata — the agent
        # retrieves the full pocket document on demand via the in-process
        # `get_pocket` MCP tool (see agents/sdk_mcp_pocket.py). This keeps
        # the system prompt well below the Windows CLI arg limit regardless
        # of how large rippleSpec.ui is.
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
                        "pocket_created event received: title=%r,"
                        " has_ui=%s, has_widgets=%s, has_panes=%s",
                        spec.get("title"),
                        "ui" in spec,
                        "widgets" in spec,
                        "panes" in spec,
                    )
                    spec = _prepare_pocket_spec(spec)
                    if spec:
                        pocket_emitted = True
                        fmt = (
                            "UISpec"
                            if "ui" in spec
                            else f"{len(spec.get('panes', {}))} panes"
                            if "panes" in spec
                            else f"{len(spec.get('widgets', []))} widgets"
                        )
                        logger.info("Pocket created: %s (%s)", spec.get("title", "?"), fmt)
                        pocket_cloud_id = edata.get("pocket_cloud_id")
                        payload = json.dumps(
                            {
                                "spec": spec,
                                "session_id": safe_key,
                                "pocket_cloud_id": pocket_cloud_id,
                            }
                        )
                        yield (f"event: pocket_created\ndata: {payload}\n\n")
                    else:
                        logger.warning(
                            "pocket_created event dropped —"
                            " _prepare_pocket_spec returned"
                            " None for title=%r",
                            edata.get("spec", {}).get("title"),
                        )
                    continue

                if etype == "pocket_mutation":
                    mutation = edata.get("mutation", {})
                    if mutation:
                        yield (f"event: pocket_mutation\ndata: {json.dumps(mutation)}\n\n")
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
                            payload = json.dumps(
                                {
                                    "spec": spec,
                                    "session_id": safe_key,
                                }
                            )
                            yield (f"event: pocket_created\ndata: {payload}\n\n")

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
