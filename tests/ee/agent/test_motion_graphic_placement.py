# tests/ee/agent/test_motion_graphic_placement.py — add_motion_graphic placement.
# start_ms and replace_range are validated (exclusivity, from < to, whole ms, a
# data-duration that fills the cut) and reach the SSE frame as startMs and
# replaceRange {fromMs, toMs}; the reply still says dispatched, never done.

from __future__ import annotations

import json

import pytest
from pocketpaw_ee.agent.mcp_servers.timeline import (
    _add_motion_graphic_handler,
    validate_placement,
)
from pocketpaw_ee.cloud.chat.agent_service import bind_timeline, unbind_timeline
from pocketpaw_ee.cloud.chat.runs.run_core import _timeline_payload

HTML = """<!doctype html><html><head>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script></head><body>
<div id="root" data-composition-id="mg" data-start="0" data-duration="4"
     data-width="1920" data-height="1080"><section class="clip" data-start="0" data-duration="4">
<h1>42%</h1></section></div>
<script>const tl=gsap.timeline({paused:true});window.__timelines["mg"]=tl;</script>
</body></html>"""


@pytest.fixture
def open_timeline():
    token = bind_timeline(
        {"project_id": "p", "name": "Cut", "assets": [{"id": "ast_V1StGXR8Z5jdHi6B0"}]}
    )
    try:
        yield
    finally:
        unbind_timeline(token)


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def test_no_placement_adds_no_keys() -> None:
    assert validate_placement({}, 4.0) == ({}, None)


def test_start_ms_becomes_start_ms_camel() -> None:
    assert validate_placement({"start_ms": 10_000}, 4.0) == ({"startMs": 10_000}, None)


def test_replace_range_becomes_camel_case() -> None:
    keys, error = validate_placement({"replace_range": {"from_ms": 10_000, "to_ms": 14_000}}, 4.0)
    assert error is None
    assert keys == {"replaceRange": {"fromMs": 10_000, "toMs": 14_000}}


def test_replace_range_accepts_a_json_string() -> None:
    keys, error = validate_placement({"replace_range": '{"from_ms": 0, "to_ms": 4000}'}, 4.0)
    assert error is None and keys == {"replaceRange": {"fromMs": 0, "toMs": 4000}}


@pytest.mark.parametrize(
    "args",
    [
        {"start_ms": 0, "replace_range": {"from_ms": 0, "to_ms": 4000}},
        {"start_ms": 0, "replace_asset_id": "ast_x"},
        {"replace_range": {"from_ms": 0, "to_ms": 4000}, "replace_asset_id": "ast_x"},
    ],
)
def test_placements_are_mutually_exclusive(args: dict) -> None:
    keys, error = validate_placement(args, 4.0)
    assert keys is None
    assert "only one of" in error


def test_from_must_be_before_to() -> None:
    keys, error = validate_placement({"replace_range": {"from_ms": 5000, "to_ms": 5000}}, 4.0)
    assert keys is None and "less than to_ms" in error


@pytest.mark.parametrize(
    "args",
    [
        {"start_ms": 1.5},
        {"start_ms": "1000"},
        {"start_ms": True},
        {"start_ms": -1},
        {"replace_range": {"from_ms": 0.5, "to_ms": 4000}},
        {"replace_range": {"from_ms": "0", "to_ms": 4000}},
        {"replace_range": {"from_ms": 0}},
        {"replace_range": [0, 4000]},
    ],
)
def test_non_integer_milliseconds_are_refused(args: dict) -> None:
    keys, error = validate_placement(args, 4.0)
    assert keys is None and error


def test_a_duration_that_does_not_fill_the_cut_names_the_exact_value() -> None:
    keys, error = validate_placement({"replace_range": {"from_ms": 10_000, "to_ms": 13_500}}, 4.0)
    assert keys is None
    assert 'data-duration="3.5"' in error
    assert "4s" in error


def test_a_duration_within_tolerance_is_accepted() -> None:
    keys, error = validate_placement({"replace_range": {"from_ms": 0, "to_ms": 4040}}, 4.0)
    assert error is None and keys["replaceRange"]["toMs"] == 4040


async def test_replace_range_reaches_the_sse_frame(open_timeline) -> None:
    result = await _add_motion_graphic_handler(
        {"html": HTML, "replace_range": {"from_ms": 10_000, "to_ms": 14_000}}
    )
    assert result.get("is_error") is not True
    payload = _timeline_payload(_text(result), "motion_graphic")
    assert payload["replaceRange"] == {"fromMs": 10_000, "toMs": 14_000}
    assert "startMs" not in payload and "replaceAssetId" not in payload
    note = json.loads(_text(result))["note"].lower()
    assert "dispatched" in note and "next turn" in note


async def test_start_ms_reaches_the_sse_frame(open_timeline) -> None:
    result = await _add_motion_graphic_handler({"html": HTML, "start_ms": 2500})
    payload = _timeline_payload(_text(result), "motion_graphic")
    assert payload["startMs"] == 2500
    assert "replaceRange" not in payload


async def test_a_mismatched_cut_is_a_tool_error(open_timeline) -> None:
    result = await _add_motion_graphic_handler(
        {"html": HTML, "replace_range": {"from_ms": 0, "to_ms": 6000}}
    )
    assert result.get("is_error") is True
    assert 'data-duration="6"' in _text(result)
    assert _timeline_payload(_text(result), "motion_graphic") is None
