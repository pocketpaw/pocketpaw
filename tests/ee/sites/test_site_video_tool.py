# tests/ee/sites/test_site_video_tool.py — the site-authoring video generator.
#
# Created 2026-09-11 (feat/site-video-tool, SM-5). Sibling of
# test_site_media_tool.py, and it inherits that file's two failure modes (the
# sink must be public; the allow-list is not a boundary, ownership is). What is
# NEW here is everything that follows from video being slow and expensive:
#
#   * THE ROW IS WRITTEN BEFORE THE RENDER. A fal video takes minutes. If the
#     history row only appeared on success, /studio would show nothing while the
#     job ran and a page reload would lose it — exactly the defect that made the
#     old JSONL store untenable, since append-on-success was its only write.
#   * A FAILURE MOVES THAT ROW, it does not leave a permanent `running` ghost
#     and it does not add a second row.
#   * THE POSTER IS PART OF THE DELIVERABLE. A scroll-scrubbed <video> paints
#     nothing until it has buffered enough to decode, so a hero without a poster
#     is blank on first load.

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from pocketpaw_ee.agent.mcp_servers import site_media
from pocketpaw_ee.agent.mcp_servers.site_media import (
    GENERATE_SITE_VIDEO_TOOL_ID,
    SITE_MEDIA_TOOL_IDS,
    _generate_site_video_handler,
)

_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _text(resp: dict) -> str:
    return resp["content"][0]["text"]


def _body(resp: dict) -> dict:
    return json.loads(_text(resp))


@dataclass
class _FakeAsset:
    url: str
    mime: str = "video/mp4"
    size: int = 4096
    filename: str = "hero.mp4"
    key: str = "sites-assets/ws/pk/abc-hero.mp4"


class FakeStore:
    def __init__(self, fail_on: str | None = None) -> None:
        self.seen: list[tuple[str, str, str]] = []
        self._fail_on = fail_on

    async def put(self, data, *, filename, workspace_id, pocket_id):  # noqa: ANN001
        if self._fail_on and self._fail_on in filename:
            from pocketpaw_ee.sites.public_assets import PublicAssetError

            raise PublicAssetError("That video is 84 MB. The ceiling is 50 MB.")
        self.seen.append((workspace_id, pocket_id, filename))
        return _FakeAsset(url=f"https://cdn.example.com/sites-assets/{workspace_id}/{filename}")


def _patch_owner(monkeypatch, owner: str | None = "ws1"):
    async def _owner(_pocket_id):
        return owner

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get_pocket_workspace", _owner, raising=False
    )


def _patch_fal(monkeypatch, *, video=_MP4, poster=_JPG, raises: Exception | None = None):
    """Stub the fal render — no network, no spend, no minutes."""
    seen: dict = {}

    async def _run(**kwargs):
        seen.update(kwargs)
        if raises is not None:
            raise raises
        return (video, "video/mp4", poster, "image/jpeg")

    monkeypatch.setattr("pocketpaw_ee.cloud.studio.fal_video.run_fal_video", _run, raising=False)

    async def _resolve(url, **_kw):
        return (f"data:image/png;base64,FAKE({url})", "image/png")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.studio.service._resolve_source_data_url", _resolve, raising=False
    )
    return seen


def _patch_history(monkeypatch) -> list[dict]:
    """Capture the ORDERED history writes — order is the point of these tests."""
    recorded: list[dict] = []

    async def _record(workspace_id, generation, *, source="studio", pocket_id=None):  # noqa: ANN001
        recorded.append({"status": generation.status, "gen": generation, "source": source})

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.studio.service.record_generation_best_effort",
        _record,
        raising=False,
    )
    return recorded


def _wire(monkeypatch, store, **fal_kwargs):
    monkeypatch.setattr(site_media, "_identity", lambda: ("ws1", "u1"))
    _patch_owner(monkeypatch, "ws1")
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )
    seen = _patch_fal(monkeypatch, **fal_kwargs)
    return seen, _patch_history(monkeypatch)


# ── Reachability ────────────────────────────────────────────────────────


def test_the_video_tool_rides_the_tuple() -> None:
    assert GENERATE_SITE_VIDEO_TOOL_ID in SITE_MEDIA_TOOL_IDS


def test_the_video_tool_is_reachable_on_sites() -> None:
    """An id absent from the hard whitelist is filtered out and never fires."""
    from pocketpaw_ee.cloud.surface.surface_registry import _load_mcp_tool_ids

    ids = _load_mcp_tool_ids()
    assert ids.loaded, "the MCP allow-lists failed to load; scoping would be disabled"
    assert GENERATE_SITE_VIDEO_TOOL_ID in (ids.sites_allow or frozenset())


# ── The sink ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_video_and_poster_are_public_and_absolute(monkeypatch) -> None:
    """A backend-relative /api/v1/media/<name> resolves against the SITE's own
    domain and 404s, so returning one would break the hero on every visit.

    MUTATION: sink through media_storage.save_generated instead of the store.
    """
    store = FakeStore()
    _wire(monkeypatch, store)

    resp = await _generate_site_video_handler(
        {"pocket_id": "pk1", "prompt": "slow dolly in", "image_url": "https://x/a.png"}
    )
    body = _body(resp)

    assert body["ok"] is True
    assert body["url"].startswith("https://")
    assert "/api/v1/media/" not in body["url"]
    assert body["poster_url"].startswith("https://"), "a scrubbed video needs a poster"


@pytest.mark.asyncio
async def test_a_missing_poster_is_not_a_failure(monkeypatch) -> None:
    """Some endpoints return no poster. A worse hero, not a broken call."""
    store = FakeStore()
    _wire(monkeypatch, store, poster=None)

    body = _body(await _generate_site_video_handler({"pocket_id": "pk1", "prompt": "orbit"}))

    assert body["ok"] is True
    assert body["poster_url"] is None


# ── The lifecycle: visible while it renders ─────────────────────────────


@pytest.mark.asyncio
async def test_a_running_row_is_written_before_the_render(monkeypatch) -> None:
    """The job takes minutes. If the row only appeared on success, /studio would
    show nothing while it ran and a reload would lose it.

    MUTATION: move the `running` record below the run_fal_video await.
    """
    store = FakeStore()
    _, recorded = _wire(monkeypatch, store)

    await _generate_site_video_handler({"pocket_id": "pk1", "prompt": "push in"})

    assert [r["status"] for r in recorded] == ["running", "succeeded"]
    # Same row moved, rather than two rows appearing.
    assert recorded[0]["gen"].id == recorded[1]["gen"].id


@pytest.mark.asyncio
async def test_a_failed_render_moves_the_row_off_running(monkeypatch) -> None:
    """Otherwise the gallery keeps a permanent `running` ghost for a job that is
    never coming back.

    MUTATION: return the error without recording the failure.
    """
    store = FakeStore()
    _, recorded = _wire(monkeypatch, store, raises=RuntimeError("upstream quota exceeded"))

    resp = await _generate_site_video_handler({"pocket_id": "pk1", "prompt": "push in"})

    assert resp.get("is_error") is True
    assert "quota" in _text(resp)
    assert [r["status"] for r in recorded] == ["running", "failed"]
    assert store.seen == [], "nothing may be published when the render failed"


@pytest.mark.asyncio
async def test_a_rail_rejection_reaches_the_user_intact(monkeypatch) -> None:
    """The 50 MiB ceiling lives on the rail and its message is written to be
    shown verbatim, so it must not be swallowed into a generic error."""
    store = FakeStore(fail_on=".mp4")
    _, recorded = _wire(monkeypatch, store)

    resp = await _generate_site_video_handler({"pocket_id": "pk1", "prompt": "push in"})

    assert resp.get("is_error") is True
    assert "50 MB" in _text(resp)
    assert [r["status"] for r in recorded] == ["running", "failed"]


# ── Inputs ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_image_selects_the_image_to_video_model(monkeypatch) -> None:
    """This is the whole point when the owner handed us a photo: animate THEIR
    image rather than invent a lookalike.

    MUTATION: always use DEFAULT_VIDEO_MODEL.
    """
    from pocketpaw_ee.cloud.studio import fal_video

    store = FakeStore()
    seen, _ = _wire(monkeypatch, store)

    await _generate_site_video_handler(
        {"pocket_id": "pk1", "prompt": "orbit left", "image_url": "https://cdn/x.png"}
    )

    assert seen["model"] == fal_video.DEFAULT_IMAGE_TO_VIDEO_MODEL
    assert seen["image_urls"] and "x.png" in seen["image_urls"][0]


@pytest.mark.asyncio
async def test_no_image_falls_back_to_text_to_video(monkeypatch) -> None:
    from pocketpaw_ee.cloud.studio import fal_video

    store = FakeStore()
    seen, _ = _wire(monkeypatch, store)

    await _generate_site_video_handler({"pocket_id": "pk1", "prompt": "a city at dusk"})

    assert seen["model"] == fal_video.DEFAULT_VIDEO_MODEL
    assert seen["image_urls"] is None


@pytest.mark.asyncio
async def test_duration_is_coerced_to_a_supported_tier(monkeypatch) -> None:
    """Kling takes 5 or 10. A model asking for 30 must not reach fal with it, and
    a non-numeric value must not raise out of the handler — MCP input schemas are
    advisory for in-process tools.

    MUTATION: pass args['duration_sec'] straight through.
    """
    store = FakeStore()
    seen, _ = _wire(monkeypatch, store)

    await _generate_site_video_handler({"pocket_id": "pk1", "prompt": "x", "duration_sec": 30})
    assert seen["duration_sec"] in site_media._VIDEO_DURATIONS

    await _generate_site_video_handler({"pocket_id": "pk1", "prompt": "x", "duration_sec": "ten"})
    assert seen["duration_sec"] in site_media._VIDEO_DURATIONS


# ── The boundary is ownership, not the allow-list ───────────────────────


@pytest.mark.asyncio
async def test_a_foreign_pocket_is_refused_before_any_spend(monkeypatch) -> None:
    """Video is the most expensive call in the product. The check has to happen
    before the render, not after.

    MUTATION: drop the owner != workspace check.
    """
    store = FakeStore()
    seen, recorded = _wire(monkeypatch, store)
    _patch_owner(monkeypatch, "someone-elses-ws")

    resp = await _generate_site_video_handler({"pocket_id": "pk1", "prompt": "x"})

    assert resp.get("is_error") is True
    assert seen == {}, "fal must not have been called at all"
    assert recorded == [], "and nothing may be recorded against a foreign site"
    assert store.seen == []
