# test_timeline_add_media.py — the add_media timeline tool: a file the agent made
# in its own jail (a voiceover it synthesised, a clip it cut) reaches the
# /studio/editor media rail.
#
# The reported failure: an agent generated narration server-side with a pasted
# TTS key, then had no way to put the audio on the timeline. These tests pin the
# tool that closes that gap and the boundaries it must keep: jail-only paths,
# media-only files, and an envelope run_core actually promotes to an SSE frame.

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.agent.mcp_servers import deliver
from pocketpaw_ee.agent.mcp_servers.timeline import (
    ADD_MEDIA_TOOL_ID,
    TIMELINE_TOOL_IDS,
    _add_media_handler,
)
from pocketpaw_ee.cloud.chat.agent_service import bind_timeline, unbind_timeline

TIMELINE: dict[str, Any] = {
    "project_id": "proj_1",
    "clips": [],
    "assets": [],
    "tracks": [{"id": "trk_audio000000000002"}],
}


@pytest.fixture
def jail(tmp_path, monkeypatch):
    """A workspace jail on tmp_path, bound to ws_1 / u_1 with the editor open."""
    monkeypatch.setenv("POCKETPAW_WORKSPACE_JAIL_ROOT", str(tmp_path / "jails"))
    session = tmp_path / "jails" / "ws_1" / "agent" / "s_1"
    session.mkdir(parents=True)
    monkeypatch.setattr(deliver, "_identity", lambda: ("ws_1", "u_1"))
    monkeypatch.setattr("pocketpaw_ee.cloud.agent_jail.resolve_agent_cwd", lambda: str(session))
    uploads: list[dict] = []

    async def fake_upload(path, *, filename, mime, workspace_id, user_id, deliver_max):
        uploads.append({"path": path, "filename": filename, "mime": mime, "ws": workspace_id})
        rec = SimpleNamespace(id="file_123", filename=filename, mime=mime, size=path.stat().st_size)
        return rec, "/api/v1/uploads/file_123"

    monkeypatch.setattr(deliver, "_upload_artifact", fake_upload)
    token = bind_timeline(TIMELINE)
    try:
        yield SimpleNamespace(session=session, root=tmp_path, uploads=uploads)
    finally:
        unbind_timeline(token)


def body(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_the_tool_is_on_the_timeline_server() -> None:
    assert ADD_MEDIA_TOOL_ID == "mcp__pocketpaw_timeline__add_media"
    assert ADD_MEDIA_TOOL_ID in TIMELINE_TOOL_IDS


def test_the_editor_surface_offers_it() -> None:
    from pocketpaw_ee.cloud.surface import service
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta

    profile = service.resolve_profile(SurfaceKind.STUDIO_EDITOR, SurfaceMeta())
    assert ADD_MEDIA_TOOL_ID in (profile.allow_mcp_tool_ids or ())


async def test_a_voiceover_in_the_jail_reaches_the_rail(jail) -> None:
    (jail.session / "out").mkdir()
    (jail.session / "out" / "narration.mp3").write_bytes(b"ID3fake-mp3")

    result = await _add_media_handler(
        {"path": "out/narration.mp3", "name": "Narration", "at_ms": 1500}
    )

    assert not result.get("is_error"), result
    media = body(result)["media_import"]
    assert media["fileId"] == "file_123"
    assert media["url"] == "/api/v1/uploads/file_123"
    assert media["kind"] == "audio"
    assert media["name"] == "Narration"
    assert media["atMs"] == 1500
    assert jail.uploads[0]["ws"] == "ws_1"
    assert "done" not in body(result)["note"].lower()


async def test_the_envelope_survives_the_run_core_extractor(jail) -> None:
    from pocketpaw_ee.cloud.chat.runs.run_core import _EDITOR_ENVELOPE_MARKERS, _timeline_payload

    (jail.session / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    result = await _add_media_handler({"path": "clip.mp4"})

    assert "media_import" in _EDITOR_ENVELOPE_MARKERS
    payload = _timeline_payload(result["content"][0]["text"], "media_import")
    assert payload is not None and payload["kind"] == "video"
    assert "atMs" not in payload


async def test_a_path_outside_the_jail_is_refused(jail) -> None:
    outside = jail.root / "secret.mp3"
    outside.write_bytes(b"ID3")

    result = await _add_media_handler({"path": str(outside)})

    assert result.get("is_error")
    assert jail.uploads == []


async def test_a_file_that_is_not_media_is_refused(jail) -> None:
    (jail.session / "make_voice.py").write_text("print('hi')")

    result = await _add_media_handler({"path": "make_voice.py"})

    assert result.get("is_error")
    assert "audio, video or image" in result["content"][0]["text"]
    assert jail.uploads == []


async def test_a_missing_file_is_refused(jail) -> None:
    result = await _add_media_handler({"path": "nope.wav"})
    assert result.get("is_error")
    assert jail.uploads == []


async def test_nothing_uploads_when_no_editor_is_open(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(deliver, "_identity", lambda: ("ws_1", "u_1"))
    called = []

    async def fake_upload(*a, **k):
        called.append(1)

    monkeypatch.setattr(deliver, "_upload_artifact", fake_upload)
    result = await _add_media_handler({"path": "narration.mp3"})
    assert result.get("is_error")
    assert called == []
