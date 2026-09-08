# ee/agent/mcp_servers/timeline.py — in-process MCP server: /studio/editor
# timeline edits.
#
# Created: 2026-09-08 (feat/agentic-studio-editor). Clones the media.py /
# sites.py shape: SDK import guard, SERVER_NAME + *_TOOL_ID allowlist constants,
# ContextVar-sourced state, _error_response / _success_response helpers.
#
# TWO tools, not thirty. The 17-tool pocket edit surface was collapsed to one
# skill + one merge endpoint for a reason, and a tool per store method would put
# ten round-trips between "arrange these three clips" and a result. `ops` takes
# a BATCH instead, applied atomically as one undo step.
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
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_timeline"

EDIT_TIMELINE_TOOL_ID = f"mcp__{SERVER_NAME}__edit_timeline"
EXPORT_TIMELINE_TOOL_ID = f"mcp__{SERVER_NAME}__export_timeline"

TIMELINE_TOOL_IDS = (EDIT_TIMELINE_TOOL_ID, EXPORT_TIMELINE_TOOL_ID)

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


def _edit_timeline_parameters() -> dict[str, Any]:
    return {
        "ops": {
            "type": "array",
            "description": (
                "Operations to apply, in order. Each is an object with an `op` "
                "field: place_clip {assetId, atMs|after, track?, inMs?, outMs?}; "
                "move_clip {clipId, atMs|after, track?}; trim_clip {clipId, inMs?, "
                "outMs?}; split_clip {clipId, atMs}; remove_clip {clipId}; "
                "set_transition {clipId, kind, durationMs?, direction?, color?, "
                "softness?}; add_text {text, fromMs, toMs, presetId?, fontSize?, "
                "color?, align?}; add_caption {text, fromMs, toMs, anchorClip?, "
                "style?}; place_audio {assetId, atMs|after, track?, volume?, "
                "muted?}; set_volume {target, volume}; set_project {name?, "
                "aspectRatio?, fps?, fit?, background?}. To arrange NEW clips end to "
                "end, omit both atMs and after — each appends after the last on "
                "its lane. `after` anchors to a clip ALREADY on the timeline; a "
                "clip created in this same batch has no id yet."
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

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[edit_timeline, export_timeline],
    )
    return SERVER_NAME, server


__all__ = [
    "EDIT_TIMELINE_TOOL_ID",
    "EXPORT_FORMATS",
    "EXPORT_PRESETS",
    "EXPORT_TIMELINE_TOOL_ID",
    "SERVER_NAME",
    "TIMELINE_TOOL_IDS",
    "build_timeline_server",
]
