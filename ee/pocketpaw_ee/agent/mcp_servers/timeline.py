# ee/agent/mcp_servers/timeline.py — in-process MCP server: /studio/editor
# timeline edits.
#
# Created: 2026-09-08 (feat/agentic-studio-editor). Clones the media.py /
# sites.py shape: SDK import guard, SERVER_NAME + *_TOOL_ID allowlist constants,
# ContextVar-sourced state, _error_response / _success_response helpers.
#
# THREE tools, not thirty. The 17-tool pocket edit surface was collapsed to one
# skill + one merge endpoint for a reason, and a tool per store method would put
# ten round-trips between "arrange these three clips" and a result. `ops` takes
# a BATCH instead, applied atomically as one undo step. `add_motion_graphic` is
# the one way to CREATE footage here: the agent writes a HyperFrames composition
# (one self-contained HTML file), this validates it, and the browser renders it
# to an MP4 and places it: at ``start_ms``, into a ``replace_range`` cut out of
# the main video, or, given ``replace_asset_id``, in place of an earlier render.
# Placement rides on this call because the rendered asset only exists next turn.
# Same one-call dispatch shape as the other two.
#
# There is deliberately no read tool. The document lives in the browser, so the
# server has nothing to read; the /studio/editor preamble carries the timeline
# summary instead — fresher than a tool call and free.
#
# WHAT "ok" MEANS HERE. These tools VALIDATE and DISPATCH. The apply happens in
# the browser tab that holds the document, after this returns. That is the same
# gap that silently dropped agent-built flows when build_studio_flow relied on
# the frontend re-PUTting after the canvas rendered — so it is closed on both
# ends: hard validation before dispatch (invented ids and unknown verbs fail
# here, with a suggestion), and the apply report riding back on the NEXT turn's
# surface_meta so a refusal cannot pass unnoticed. The tool text says
# "dispatched", never "applied", and the preamble forbids claiming otherwise.

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_timeline"

EDIT_TIMELINE_TOOL_ID = f"mcp__{SERVER_NAME}__edit_timeline"
EXPORT_TIMELINE_TOOL_ID = f"mcp__{SERVER_NAME}__export_timeline"
ADD_MOTION_GRAPHIC_TOOL_ID = f"mcp__{SERVER_NAME}__add_motion_graphic"

TIMELINE_TOOL_IDS = (EDIT_TIMELINE_TOOL_ID, EXPORT_TIMELINE_TOOL_ID, ADD_MOTION_GRAPHIC_TOOL_ID)

MOTION_GRAPHIC_MAX_CHARS = 200_000
MOTION_GRAPHIC_MAX_DURATION_S = 120
MOTION_GRAPHIC_FPS = (24, 25, 30, 60)

_ROOT_TAG_RE = re.compile(r"<[a-zA-Z][^>]*\bdata-composition-id\b[^>]*>", re.IGNORECASE)
_ASSET_TAG_RE = re.compile(r"<(?:script|link|img|source|video|audio)\b[^>]*>", re.IGNORECASE)
_URL_ATTR_RE = re.compile(
    r"\b(?:src|href)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.IGNORECASE
)
_TIMELINES_RE = re.compile(r"window\.__timelines\s*\[")
_ABSOLUTE_PREFIXES = ("http://", "https://", "data:", "#", "blob:")

# Mirrors PLATFORM_PRESETS in editor/platform-presets.ts. Closed for the same
# reason the op vocabulary is — an unknown preset would reach the client and
# no-op. Absent means "render at the project's current frame size and fps".
EXPORT_PRESETS = (
    "youtube-1080p",
    "youtube-shorts",
    "tiktok",
    "instagram-reel",
    "meta-feed-portrait",
    "square",
    "x-video",
)

# editor/export-presets.ts. mp4 (H.264/AAC) is the safe default everywhere.
EXPORT_FORMATS = ("mp4", "webm")


def _error_response(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "is_error": True}


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(body, separators=(",", ":"), default=str)}]
    }


def _root_attr(tag: str, name: str) -> float | None:
    match = re.search(rf"\b{name}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", tag, re.IGNORECASE)
    if match is None:
        return None
    raw = next(g for g in match.groups() if g is not None).strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def validate_motion_graphic(html: Any, fps: Any) -> tuple[dict | None, str | None]:
    """Check a HyperFrames composition renders in the browser; return its shape.

    Returns ``({durationS, width, height, fps}, None)`` or ``(None, error)``,
    where the error says what to change.
    """
    if not isinstance(html, str) or not html.strip():
        return None, "`html` is empty. Pass the whole composition as one HTML document."
    if len(html) > MOTION_GRAPHIC_MAX_CHARS:
        return None, (
            f"`html` is {len(html):,} characters; the limit is {MOTION_GRAPHIC_MAX_CHARS:,}. "
            "Inline less: drop embedded images or fonts, or load them from absolute URLs."
        )

    try:
        fps_value = 30 if fps is None or fps == "" else int(float(str(fps).strip()))
    except (ValueError, OverflowError):
        fps_value = -1
    if fps_value not in MOTION_GRAPHIC_FPS:
        return None, (
            f"fps {fps!r} is not supported. Use one of "
            f"{', '.join(str(f) for f in MOTION_GRAPHIC_FPS)}, or omit it for 30."
        )

    root = _ROOT_TAG_RE.search(html)
    if root is None:
        return None, (
            "No element carries `data-composition-id`. Put it on the root element, "
            'e.g. <div id="root" data-composition-id="main" data-start="0" '
            'data-duration="5" data-width="1920" data-height="1080">.'
        )
    tag = root.group(0)
    dims: dict[str, float] = {}
    for attr in ("data-duration", "data-width", "data-height"):
        value = _root_attr(tag, attr)
        if value is None:
            return None, (
                f"The root element (the first one with `data-composition-id`) needs a "
                f"positive numeric `{attr}`. Duration is in seconds; width and height "
                "are pixels, e.g. 1920 and 1080."
            )
        dims[attr] = value
    duration = dims["data-duration"]
    if duration > MOTION_GRAPHIC_MAX_DURATION_S:
        return None, (
            f"data-duration is {duration:g}s; a motion graphic can run at most "
            f"{MOTION_GRAPHIC_MAX_DURATION_S}s. Shorten it or split it into several."
        )

    if not _TIMELINES_RE.search(html):
        return None, (
            "No `window.__timelines[...]` assignment found. Build a paused GSAP timeline "
            "and register it: window.__timelines = window.__timelines || {}; "
            'window.__timelines["<composition-id>"] = tl;'
        )

    if re.search(r"<audio\b", html, re.IGNORECASE):
        return None, (
            "<audio> is not supported in motion graphics. Leave audio out of the "
            "composition and lay sound in with place_audio via edit_timeline instead."
        )

    for asset_tag in _ASSET_TAG_RE.finditer(html):
        for attr in _URL_ATTR_RE.finditer(asset_tag.group(0)):
            url = next(g for g in attr.groups() if g is not None).strip()
            if not url.lower().startswith(_ABSOLUTE_PREFIXES):
                return None, (
                    f"Relative URL {url!r} in {asset_tag.group(0)[:80]!r}. The composition "
                    "renders with no base URL, so every asset must be inline or absolute: "
                    "use an https:// URL (e.g. a pinned CDN) or a data: URL."
                )

    width, height = dims["data-width"], dims["data-height"]
    return {
        "durationS": duration,
        "width": int(width) if width.is_integer() else width,
        "height": int(height) if height.is_integer() else height,
        "fps": fps_value,
    }, None


def _current_summary():
    """The open timeline, from the per-stream ContextVar run_core binds.

    Same seam the pawbar server reads its action registry from. Returns a
    TimelineSummary that reports ``has_timeline=False`` when no editor is open,
    which is what turns "there is nothing to edit" into a clear refusal rather
    than a confident no-op.
    """
    from pocketpaw_ee.agent.mcp_servers.timeline_ops import TimelineSummary

    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_timeline
    except ImportError:  # pragma: no cover - cloud chat not installed
        return TimelineSummary(has_timeline=False)
    return TimelineSummary.from_meta({"timeline": current_timeline()})


async def _edit_timeline_handler(args: dict) -> dict:
    """Validate an op batch and hand it to the editor tab."""
    from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args
    from pocketpaw_ee.agent.mcp_servers.timeline_ops import validate_ops

    args = coerce_json_object_args(args, ("ops",))
    ops = args.get("ops")
    # SDK callers that cannot pass a nested array through a flat signature send
    # it as a JSON string — the same accommodation build_studio_flow makes.
    if isinstance(ops, str):
        try:
            ops = json.loads(ops.strip() or "[]")
        except json.JSONDecodeError:
            return _error_response("`ops` was a string but not valid JSON.")

    clean, error = validate_ops(ops, _current_summary())
    if error is not None:
        # Precise and agent-readable: it names the index, the field and the
        # nearest real id, so the model fixes the batch and retries this turn.
        return _error_response(error)
    assert clean is not None

    return _success_response(
        {
            "ok": True,
            "dispatched": len(clean),
            "timeline_edit": {"ops": clean},
            "note": (
                "Validated and sent to the editor. The browser applies it as one "
                "undo step; the result appears in the timeline summary on your "
                "next turn. Do not tell the user it is 'applied' — say what you "
                "arranged."
            ),
        }
    )


async def _export_timeline_handler(args: dict) -> dict:
    """Ask the editor tab to render the timeline."""
    summary = _current_summary()
    if not summary.has_timeline:
        return _error_response(
            "No timeline is open, so there is nothing to export. Ask the user to "
            "open a project in the editor."
        )

    import difflib

    preset = str(args.get("preset") or "").strip().lower()
    if preset and preset not in EXPORT_PRESETS:
        close = difflib.get_close_matches(preset, EXPORT_PRESETS, n=1, cutoff=0.6)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        return _error_response(
            f"{preset!r} is not a platform preset.{hint} "
            f"Valid presets: {', '.join(EXPORT_PRESETS)}. "
            "Omit it to render at the project's current frame size and fps."
        )

    fmt = str(args.get("format") or "mp4").strip().lower()
    if fmt not in EXPORT_FORMATS:
        return _error_response(
            f"{fmt!r} is not an export format. Valid formats: {', '.join(EXPORT_FORMATS)}."
        )

    return _success_response(
        {
            "ok": True,
            "timeline_export": {"preset": preset or None, "format": fmt},
            "note": (
                "Export started in the browser. Rendering runs on the user's "
                "machine and takes a while on a long timeline — tell them it is "
                "rendering, and do not claim a finished file."
            ),
        }
    )


def _resolve_replace_asset(raw: str, known: set[str]) -> tuple[str | None, str | None]:
    """Resolve ``replace_asset_id`` (a full id or the tail the prompt showed)."""
    from pocketpaw_ee.agent.mcp_servers.timeline_ops import _suggest
    from pocketpaw_ee.cloud.pockets.id_resolve import AmbiguousId, resolve_id

    try:
        return resolve_id(raw, [{"id": i} for i in known]), None
    except AmbiguousId:
        return None, (
            f"replace_asset_id {raw!r} matches more than one asset on the media rail, "
            "so it is not safe to guess which. Ask the user which motion graphic they mean."
        )
    except KeyError:
        return None, (
            f"replace_asset_id {raw!r} is not an asset on the media rail.{_suggest(raw, known)} "
            "Copy the id from the MOTION GRAPHICS block, or omit it to add a new one."
        )


def _int_ms(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def validate_placement(args: dict, duration_s: float) -> tuple[dict | None, str | None]:
    """Check ``start_ms`` / ``replace_range``; return the camelCase payload keys.

    Returns ``({}, None)`` when neither is given, ``(keys, None)`` when valid, or
    ``(None, error)`` where the error says what to change.
    """
    start = args.get("start_ms")
    span = args.get("replace_range")
    if isinstance(span, str) and span.strip():
        try:
            span = json.loads(span)
        except json.JSONDecodeError:
            return None, '`replace_range` is not valid JSON. Pass {"from_ms": int, "to_ms": int}.'
    given = [
        k
        for k, v in (
            ("start_ms", start),
            ("replace_range", span),
            ("replace_asset_id", str(args.get("replace_asset_id") or "").strip()),
        )
        if v not in (None, "", {})
    ]
    if len(given) > 1:
        return None, (
            "Pass only one of start_ms, replace_range and replace_asset_id; "
            f"got {', '.join(given)}. replace_asset_id re-renders a graphic where it "
            "already sits."
        )

    if start is not None and start != "":
        if _int_ms(start) is None:
            return (
                None,
                f"`start_ms` must be a whole number of milliseconds, 0 or more; got {start!r}.",
            )
        return {"startMs": start}, None

    if span in (None, "", {}):
        return {}, None
    if not isinstance(span, dict):
        return None, '`replace_range` must be an object: {"from_ms": int, "to_ms": int}.'
    lo, hi = _int_ms(span.get("from_ms")), _int_ms(span.get("to_ms"))
    if lo is None or hi is None:
        return None, (
            "`replace_range` needs whole-millisecond `from_ms` and `to_ms`, both 0 or more; "
            f"got {span!r}."
        )
    if lo >= hi:
        return None, f"`replace_range` from_ms ({lo}) must be less than to_ms ({hi})."
    want = (hi - lo) / 1000
    if abs(duration_s - want) > 0.05:
        return None, (
            f"data-duration is {duration_s:g}s but replace_range spans {want:g}s, so the "
            f'graphic would not fill the gap. Set data-duration="{want:g}" on the root and '
            "the clip (and D in the script), then call again."
        )
    return {"replaceRange": {"fromMs": lo, "toMs": hi}}, None


async def _add_motion_graphic_handler(args: dict) -> dict:
    """Validate a HyperFrames composition and hand it to the editor tab to render."""
    summary = _current_summary()
    if not summary.has_timeline:
        return _error_response(
            "No timeline is open, so there is nowhere to put a motion graphic. Ask the "
            "user to open a project in the editor."
        )

    html = args.get("html")
    shape, error = validate_motion_graphic(html, args.get("fps"))
    if error is not None:
        return _error_response(error)
    assert shape is not None

    placement, error = validate_placement(args, shape["durationS"])
    if error is not None:
        return _error_response(error)
    assert placement is not None

    replace_raw = str(args.get("replace_asset_id") or "").strip()
    replace_id = None
    if replace_raw:
        replace_id, error = _resolve_replace_asset(replace_raw, summary.asset_ids)
        if error is not None:
            return _error_response(error)

    name = str(args.get("name") or "").strip() or "Motion graphic"
    motion_graphic: dict[str, Any] = {
        "html": html,
        "name": name,
        "fps": shape["fps"],
        "durationS": shape["durationS"],
        "width": shape["width"],
        "height": shape["height"],
        **placement,
    }
    if replace_id:
        motion_graphic["replaceAssetId"] = replace_id
        note = (
            "Re-rendering in the user's browser; the new render replaces the old one "
            "in place on the timeline. Do not claim it is finished; say it is rendering."
        )
    elif "replaceRange" in placement:
        span = placement["replaceRange"]
        note = (
            "Rendering in the user's browser; when it finishes the editor cuts "
            f"{span['fromMs']}-{span['toMs']} ms out of the main video and puts the graphic "
            "there. That is dispatched, not done: say it is rendering. If the editor "
            "refuses the cut, it says so next turn."
        )
    else:
        note = (
            "Rendering in the user's browser, then it lands on the timeline. Do "
            "not claim it is finished; say it is rendering."
        )
    return _success_response({"ok": True, "motion_graphic": motion_graphic, "note": note})


EDIT_TIMELINE_DESCRIPTION = """\
Change the timeline the user has open in the /studio/editor canvas.

Takes a BATCH of operations applied together as ONE undo step — arranging three
clips, captioning them and dropping a music bed under them is a single call, not
six. Clip, asset and track ids come from the timeline summary in your context;
copy them exactly. The operation list is closed and an unknown verb is rejected.

Returns once the batch is validated and dispatched to the editor, NOT once the
browser has applied it. Report what you arranged, never that it is 'done'."""

EXPORT_TIMELINE_DESCRIPTION = """\
Render the open /studio/editor timeline to a video file.

Runs in the user's browser and can take minutes. Never call this in the same
turn as an edit — it would render a half-built timeline."""

ADD_MOTION_GRAPHIC_DESCRIPTION = """\
Create a motion graphic — title card, kinetic type, animated stat, logo sting
— and put it on the open /studio/editor timeline. It renders full frame and
opaque, so overlays like lower thirds are not possible yet.

Author it as a HyperFrames composition per the `hyperframes-core` skill: ONE
self-contained HTML file whose root element carries data-composition-id,
data-duration (seconds, max 120), data-width and data-height, and whose inline
script registers a paused GSAP timeline on window.__timelines["<id>"].

It renders with no base URL, so every asset is inline or absolute:
- GSAP from a pinned CDN URL, e.g.
  https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js
- CSS inline in a <style> block
- fonts via absolute Google Fonts URLs or data: URLs
- no audio (use place_audio via edit_timeline), no WebGL / three.js, no
  backdrop-filter

To EDIT an existing motion graphic, rewrite its source from the MOTION GRAPHICS
block and pass its id as replace_asset_id; never add a second one.

To PLACE a new one, pass at most one of:
- start_ms: put it at that timeline time, with nothing cut.
- replace_range {from_ms, to_ms}: cut that span out of the main video and put
  the graphic in its place, so the total length is unchanged. data-duration must
  equal (to_ms - from_ms) / 1000.
Neither combines with replace_asset_id.

Returns once the composition is validated and dispatched, NOT once it has
rendered. Tell the user it is rendering, never that it is done."""


def _edit_timeline_parameters() -> dict[str, Any]:
    return {
        "ops": {
            "type": "array",
            "description": (
                "Operations to apply, in order. Each is an object with an `op` "
                "field. Placement: place_clip {assetId, atMs|after, track?, inMs?, "
                "outMs?}; move_clip {clipId, atMs|after, track?}; trim_clip "
                "{clipId, inMs?, outMs?}; split_clip {clipId, atMs}; remove_clip "
                "{clipId}; set_transition {clipId, kind, durationMs?, direction?, "
                "color?, softness?} where kind is none|crossfade|dip|slide|push|"
                "whip|flip|wipe|iris|spin|zoom|blur, direction (left|right|up|"
                "down) applies to slide/push/whip/wipe/flip, color to dip and "
                "softness to blur; place_audio {assetId, atMs|after, track?, "
                "volume?, muted?}; set_volume {target, volume}; set_project "
                "{name?, aspectRatio?, fps?, fit?, background?}. "
                "Text: add_text {text, fromMs, toMs, ...style}; style_text "
                "{clipId, text?, ...style} to restyle one that exists; add_caption "
                "{text, fromMs, toMs, anchorClip?, style?}; style_captions "
                "{style?, fontSize?, y?} for the whole cue set. Style fields for "
                "add_text/style_text: presetId (clean|pop|boxed|subtitle|neon|"
                "typewriter|impact|sticker|editorial), fontSize, color, align, "
                "animIn/animOut (none|fade|pop|slide|typewriter|bounce), "
                "animDurationMs, animDirection — a preset sets the whole look and "
                "any field passed with it wins. Caption styles are a DIFFERENT set "
                "(plain|boxed|outlined). "
                "Look: set_transform {clipId, x?, y?, scale?, rotation?, opacity?} "
                "where x/y are offsets from frame CENTRE; add_keyframe {clipId, "
                "prop, atMs, value?, ease?} and clear_keyframes {clipId, prop, "
                "atMs?} where prop is x|y|scale|rotation|opacity|volume and atMs is "
                "TIMELINE time inside the clip — pin two values to animate. "
                "zoom_clip {clipId, focusX?, focusY?, scale?, atMs?, inMs?, holdMs?, "
                "outMs?, ease?} pushes in on a point and (when outMs > 0) comes back "
                "out; focusX/focusY are fractions of the frame (0.5,0.5 = centre, "
                "0,0 = top-left) and the whole in+hold+out window must fit inside "
                "the clip. Use it rather than hand-keying scale — it keeps the point "
                "centred as the frame grows. "
                "To arrange NEW clips end to end, omit both atMs and after — each "
                "appends after the last on its lane. `after` anchors to a clip "
                "ALREADY on the timeline; a clip created in this same batch has no "
                "id yet."
            ),
        }
    }


def _export_timeline_parameters() -> dict[str, Any]:
    return {
        "preset": {
            "type": "string",
            "description": (
                "Optional platform preset, which sets the frame size and fps before "
                "rendering: " + ", ".join(EXPORT_PRESETS) + ". Omit to render at the "
                "project's current settings. Note that 'instagram-reel' is 9:16 and "
                "'meta-feed-portrait' is 4:5 — they are different shapes."
            ),
        },
        "format": {
            "type": "string",
            "description": "Container: mp4 (default, H.264/AAC) or webm.",
        },
    }


def _add_motion_graphic_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "html": {
                "type": "string",
                "description": (
                    "The whole HyperFrames composition as one self-contained HTML document."
                ),
            },
            "name": {
                "type": "string",
                "description": "Optional short label for the clip, e.g. 'Intro title'.",
            },
            "fps": {
                "type": "integer",
                "enum": list(MOTION_GRAPHIC_FPS),
                "description": "Optional render frame rate: 24, 25, 30 (default) or 60.",
            },
            "replace_asset_id": {
                "type": "string",
                "description": (
                    "To EDIT an existing motion graphic, pass its asset id from the "
                    "MOTION GRAPHICS block; the new render replaces it in place on the "
                    "timeline. Omit to add a new one."
                ),
            },
            "start_ms": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Optional timeline time in ms to place a new graphic at, with nothing cut."
                ),
            },
            "replace_range": {
                "type": "object",
                "properties": {
                    "from_ms": {"type": "integer", "minimum": 0},
                    "to_ms": {"type": "integer", "minimum": 0},
                },
                "required": ["from_ms", "to_ms"],
                "description": (
                    "Optional span of the main video to cut out and replace with this "
                    "graphic. data-duration must equal (to_ms - from_ms) / 1000."
                ),
            },
        },
        "required": ["html"],
    }


def build_timeline_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or None if the SDK is unavailable."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_timeline MCP disabled")
        return None

    @tool("edit_timeline", EDIT_TIMELINE_DESCRIPTION, _edit_timeline_parameters())
    async def edit_timeline(args):  # type: ignore[no-untyped-def]
        return await _edit_timeline_handler(args)

    @tool("export_timeline", EXPORT_TIMELINE_DESCRIPTION, _export_timeline_parameters())
    async def export_timeline(args):  # type: ignore[no-untyped-def]
        return await _export_timeline_handler(args)

    try:
        from claude_agent_sdk import ToolAnnotations

        inline_result = ToolAnnotations(maxResultSizeChars=MOTION_GRAPHIC_MAX_CHARS * 2)
    except ImportError:
        inline_result = None

    @tool(
        "add_motion_graphic",
        ADD_MOTION_GRAPHIC_DESCRIPTION,
        _add_motion_graphic_parameters(),
        annotations=inline_result,
    )
    async def add_motion_graphic(args):  # type: ignore[no-untyped-def]
        return await _add_motion_graphic_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[edit_timeline, export_timeline, add_motion_graphic],
    )
    return SERVER_NAME, server


__all__ = [
    "ADD_MOTION_GRAPHIC_DESCRIPTION",
    "ADD_MOTION_GRAPHIC_TOOL_ID",
    "EDIT_TIMELINE_TOOL_ID",
    "EXPORT_FORMATS",
    "EXPORT_PRESETS",
    "EXPORT_TIMELINE_TOOL_ID",
    "SERVER_NAME",
    "TIMELINE_TOOL_IDS",
    "build_timeline_server",
    "validate_motion_graphic",
    "validate_placement",
]
