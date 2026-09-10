# test_timeline_mcp_server.py — the edit_timeline / export_timeline handlers.
#
# Created: 2026-09-08 (feat/agentic-studio-editor).
#
# The vocabulary itself is covered in test_timeline_ops.py. What is covered here
# is the tool BOUNDARY: that the handler reads the timeline from the per-stream
# ContextVar, returns errors in the SDK's is_error envelope, and — the one that
# matters most — never tells the agent an edit was applied, because at this point
# nothing has been.

from __future__ import annotations

import json
from typing import Any

import pytest
from pocketpaw_ee.agent.mcp_servers.timeline import (
    EDIT_TIMELINE_TOOL_ID,
    EXPORT_TIMELINE_TOOL_ID,
    TIMELINE_TOOL_IDS,
    _edit_timeline_handler,
    _export_timeline_handler,
)
from pocketpaw_ee.cloud.chat.agent_service import bind_timeline, unbind_timeline

from pocketpaw.prompt.entity import short_id

TIMELINE: dict[str, Any] = {
    "project_id": "proj_1",
    "name": "Launch cut",
    "clips": [{"id": "clp_aQw2sX3eRf4Tg5Yh1"}, {"id": "clp_zZx1cC2vBn3Mm4Kj2"}],
    "assets": [{"id": "ast_V1StGXR8Z5jdHi6B0"}, {"id": "ast_bHk3mQp9Xy2Lda4C7"}],
    "tracks": [{"id": "trk_video000000000001"}, {"id": "trk_audio000000000002"}],
}


@pytest.fixture
def open_timeline():
    """Bind a timeline the way run_core does for a /studio/editor turn."""
    token = bind_timeline(TIMELINE)
    try:
        yield TIMELINE
    finally:
        unbind_timeline(token)


# The tail the agent actually sees. Derived with the same helper the preamble
# renders through, so a change to ID_TAIL_CHARS cannot make these tests lie.
CLIP_TAIL = short_id("clp_aQw2sX3eRf4Tg5Yh1")
ASSET_TAIL = short_id("ast_V1StGXR8Z5jdHi6B0")


def body(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def text(result: dict) -> str:
    return result["content"][0]["text"]


def test_tool_ids_are_namespaced_for_the_allowlist() -> None:
    """Claude Code matches allowlist entries on this exact form."""
    assert EDIT_TIMELINE_TOOL_ID == "mcp__pocketpaw_timeline__edit_timeline"
    assert EXPORT_TIMELINE_TOOL_ID == "mcp__pocketpaw_timeline__export_timeline"
    assert set(TIMELINE_TOOL_IDS) == {EDIT_TIMELINE_TOOL_ID, EXPORT_TIMELINE_TOOL_ID}


async def test_edit_returns_the_validated_batch(open_timeline) -> None:
    result = await _edit_timeline_handler({"ops": [{"op": "place_clip", "assetId": ASSET_TAIL}]})
    assert "is_error" not in result
    out = body(result)
    assert out["ok"] is True
    assert out["dispatched"] == 1
    # Full id, not the tail the agent sent — the client's store looks up by it.
    assert out["timeline_edit"]["ops"][0]["assetId"] == "ast_V1StGXR8Z5jdHi6B0"


async def test_edit_never_claims_the_edit_was_applied(open_timeline) -> None:
    """The apply happens in a browser tab after this returns. Saying "applied"
    here is how build_studio_flow's dropped builds went unnoticed."""
    out = body(
        await _edit_timeline_handler(
            {"ops": [{"op": "set_volume", "target": "master", "volume": 1}]}
        )
    )
    note = out["note"].lower()
    assert "sent to the editor" in note
    assert "do not tell the user it is 'applied'" in note


async def test_edit_accepts_ops_as_a_json_string(open_timeline) -> None:
    """SDK callers that cannot pass a nested array through a flat signature send
    it as a string — the same accommodation build_studio_flow makes."""
    result = await _edit_timeline_handler(
        {"ops": json.dumps([{"op": "remove_clip", "clipId": CLIP_TAIL}])}
    )
    assert "is_error" not in result
    assert body(result)["dispatched"] == 1


async def test_edit_rejects_a_malformed_json_string(open_timeline) -> None:
    result = await _edit_timeline_handler({"ops": "{not json"})
    assert result["is_error"] is True
    assert "not valid JSON" in text(result)


async def test_edit_surfaces_a_validation_error_as_a_tool_error(open_timeline) -> None:
    """ok=false must be an is_error envelope, not a cheerful success body —
    pocketpaw#1190's lesson: a silent bad edit is the expensive kind."""
    result = await _edit_timeline_handler({"ops": [{"op": "remove_clip", "clipId": "ghost"}]})
    assert result["is_error"] is True
    assert "ghost" in text(result)


async def test_edit_refuses_when_no_editor_is_open() -> None:
    """No ContextVar bound = no timeline. Must refuse, not no-op."""
    result = await _edit_timeline_handler(
        {"ops": [{"op": "set_volume", "target": "master", "volume": 1}]}
    )
    assert result["is_error"] is True
    assert "No timeline is open" in text(result)


async def test_export_defaults_to_the_projects_current_shape(open_timeline) -> None:
    """Omitting the preset must render what the user is looking at, never
    silently reframe their cut."""
    out = body(await _export_timeline_handler({}))
    assert out["ok"] is True
    assert out["timeline_export"] == {"preset": None, "format": "mp4"}


async def test_export_rejects_an_unknown_preset(open_timeline) -> None:
    result = await _export_timeline_handler({"preset": "instagram-reels"})
    assert result["is_error"] is True
    assert "Did you mean 'instagram-reel'?" in text(result)


async def test_export_rejects_an_unknown_format(open_timeline) -> None:
    result = await _export_timeline_handler({"format": "mov"})
    assert result["is_error"] is True
    assert "mp4" in text(result)


async def test_export_refuses_when_no_editor_is_open() -> None:
    result = await _export_timeline_handler({"preset": "tiktok"})
    assert result["is_error"] is True
    assert "nothing to export" in text(result)


async def test_export_does_not_claim_a_finished_file(open_timeline) -> None:
    out = body(await _export_timeline_handler({"preset": "tiktok"}))
    assert "do not claim a finished file" in out["note"].lower()


# ── The seam ───────────────────────────────────────────────────────────────
#
# The tool's output only reaches the browser if run_core recognises its marker
# envelope. That is a real boundary between two files that can drift apart
# silently — a renamed key would leave the tool reporting success while the
# timeline never changed, which is exactly the failure this design exists to
# prevent. These tests feed the ACTUAL handler output through the ACTUAL
# extractor rather than a hand-written fixture of what it is assumed to be.


async def test_edit_output_survives_the_run_core_extractor(open_timeline) -> None:
    from pocketpaw_ee.cloud.chat.runs.run_core import _timeline_payload

    result = await _edit_timeline_handler(
        {
            "ops": [
                {"op": "place_clip", "assetId": ASSET_TAIL},
                # Braces inside a JSON string: a find/rfind parse would truncate
                # here, which is why the extractor counts braces.
                {"op": "add_text", "text": "a {brace} in the copy", "fromMs": 0, "toMs": 900},
            ]
        }
    )
    payload = _timeline_payload(text(result), "timeline_edit")
    assert payload is not None, "run_core no longer recognises edit_timeline's envelope"
    assert len(payload["ops"]) == 2
    assert payload["ops"][0]["assetId"] == "ast_V1StGXR8Z5jdHi6B0"
    assert payload["ops"][1]["text"] == "a {brace} in the copy"


async def test_export_output_survives_the_run_core_extractor(open_timeline) -> None:
    from pocketpaw_ee.cloud.chat.runs.run_core import _timeline_payload

    result = await _export_timeline_handler({"preset": "tiktok", "format": "webm"})
    payload = _timeline_payload(text(result), "timeline_export")
    assert payload == {"preset": "tiktok", "format": "webm"}


async def test_an_error_result_carries_no_payload(open_timeline) -> None:
    """A rejected batch must not reach the browser at all."""
    from pocketpaw_ee.cloud.chat.runs.run_core import _timeline_payload

    result = await _edit_timeline_handler({"ops": [{"op": "remove_clip", "clipId": "ghost"}]})
    assert _timeline_payload(text(result), "timeline_edit") is None
