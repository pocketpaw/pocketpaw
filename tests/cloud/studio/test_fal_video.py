# tests/cloud/studio/test_fal_video.py — the direct fal.ai video-GENERATION client.
#
# fal_video is the seam that makes /studio video generation work: it resolves the
# model id → fal endpoint, builds the arguments (prompt / duration / aspect_ratio),
# runs the endpoint through the fal-client SDK, and downloads the result video (+
# optional poster). These tests keep the fal HTTP layer OUT of the picture —
# ``_run_fal`` and ``_download`` are monkeypatched — so the argument building +
# dispatch + error mapping are asserted precisely. Coverage:
#   * resolve_endpoint — fal-ai ids pass through, catalog aliases resolve, unknown
#     ids fall back to the default.
#   * build_arguments — prompt/duration/aspect_ratio shapes + guard defaults.
#   * _extract_video_url / _extract_poster_url — common result shapes + malformed.
#   * run_fal_video — dispatch (endpoint + arguments + key), env-key fallback
#     (fal_edit.fal_api_key), ValueError for empty prompt, FalVideoError for a
#     missing key and for a result with no output video, poster download failure
#     is non-fatal.
#
# Created 2026-08-18 (studio-video-generation): direct fal video dispatch tests.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import fal_edit, fal_video


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fal_api_key's env resolution deterministic (same as test_fal_edit)."""
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)


# ── Endpoint resolution ──────────────────────────────────────────────────────


def test_resolve_endpoint_passes_fal_ai_through() -> None:
    assert (
        fal_video.resolve_endpoint("fal-ai/wan/v2.1/text-to-video")
        == "fal-ai/wan/v2.1/text-to-video"
    )


def test_resolve_endpoint_aliases_catalog_id() -> None:
    assert (
        fal_video.resolve_endpoint("fal_ai/fal-ai/kling/v2")
        == "fal-ai/kling-video/v1/standard/text-to-video"
    )


def test_resolve_endpoint_aliases_replicate_slug() -> None:
    assert (
        fal_video.resolve_endpoint("kwaivgi/kling-v2.0")
        == "fal-ai/kling-video/v1/standard/text-to-video"
    )


def test_resolve_endpoint_unknown_falls_back_to_default() -> None:
    assert fal_video.resolve_endpoint("some-other-model") == fal_video.DEFAULT_VIDEO_MODEL


def test_resolve_endpoint_none_falls_back_to_default() -> None:
    assert fal_video.resolve_endpoint(None) == fal_video.DEFAULT_VIDEO_MODEL


def test_resolve_endpoint_empty_falls_back_to_default() -> None:
    assert fal_video.resolve_endpoint("  ") == fal_video.DEFAULT_VIDEO_MODEL


# ── Argument building ────────────────────────────────────────────────────────


def test_build_arguments_minimal() -> None:
    assert fal_video.build_arguments(prompt="a wave", duration_sec=None, aspect_ratio=None) == {
        "prompt": "a wave"
    }


def test_build_arguments_with_duration_and_ratio() -> None:
    args = fal_video.build_arguments(prompt="a wave", duration_sec=10, aspect_ratio="16:9")
    assert args == {"prompt": "a wave", "duration": "10", "aspect_ratio": "16:9"}


def test_build_arguments_short_duration_kept_verbatim() -> None:
    # 2s is offered in the rail; the endpoint's validation reports it if the
    # chosen model can't produce it — we never silently clamp.
    args = fal_video.build_arguments(prompt="a wave", duration_sec=2, aspect_ratio="1:1")
    assert args["duration"] == "2"


def test_build_arguments_zero_duration_omitted() -> None:
    args = fal_video.build_arguments(prompt="a wave", duration_sec=0, aspect_ratio=None)
    assert "duration" not in args


# ── Result extraction ────────────────────────────────────────────────────────


def test_extract_video_url_from_video_dict() -> None:
    result = {"video": {"url": "https://fal.test/out.mp4", "content_type": "video/mp4"}}
    assert fal_video._extract_video_url(result) == "https://fal.test/out.mp4"


def test_extract_video_url_from_videos_list() -> None:
    result = {"videos": [{"url": "https://fal.test/a.mp4"}, {"url": "https://fal.test/b.mp4"}]}
    assert fal_video._extract_video_url(result) == "https://fal.test/a.mp4"


def test_extract_video_url_from_bare_url() -> None:
    assert (
        fal_video._extract_video_url({"url": "https://fal.test/out.mp4"})
        == "https://fal.test/out.mp4"
    )


def test_extract_video_url_none() -> None:
    assert fal_video._extract_video_url({"status": "ok"}) is None
    assert fal_video._extract_video_url({}) is None


def test_extract_poster_url_from_poster_dict() -> None:
    result = {"poster": {"url": "https://fal.test/poster.jpg"}}
    assert fal_video._extract_poster_url(result) == "https://fal.test/poster.jpg"


def test_extract_poster_url_from_thumbnails_list() -> None:
    result = {
        "thumbnails": [{"url": "https://fal.test/t1.jpg"}, {"url": "https://fal.test/t2.jpg"}]
    }
    assert fal_video._extract_poster_url(result) == "https://fal.test/t1.jpg"


def test_extract_poster_url_none() -> None:
    assert fal_video._extract_poster_url({"video": {"url": "x"}}) is None


# ── run_fal_video dispatch ───────────────────────────────────────────────────


async def test_run_fal_video_dispatches_and_downloads(monkeypatch) -> None:
    calls: dict = {}

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        calls["endpoint"] = endpoint
        calls["arguments"] = arguments
        calls["key"] = key
        return {"video": {"url": "https://fal.test/out.mp4"}}

    async def _fake_download(url):
        calls["url"] = url
        return b"MP4DATA", "video/mp4"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    out = await fal_video.run_fal_video(
        prompt="a wave",
        duration_sec=5,
        aspect_ratio="16:9",
        model="fal_ai/fal-ai/kling/v2",
        key="fal-key",
    )

    assert out == (b"MP4DATA", "video/mp4", None, None)
    assert calls["endpoint"] == "fal-ai/kling-video/v1/standard/text-to-video"
    assert calls["arguments"] == {"prompt": "a wave", "duration": "5", "aspect_ratio": "16:9"}
    assert calls["key"] == "fal-key"
    assert calls["url"] == "https://fal.test/out.mp4"


async def test_run_fal_video_downloads_poster(monkeypatch) -> None:
    urls: list[str] = []

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        return {
            "video": {"url": "https://fal.test/out.mp4"},
            "poster": {"url": "https://fal.test/poster.jpg"},
        }

    async def _fake_download(url):
        urls.append(url)
        if url.endswith(".mp4"):
            return b"MP4DATA", "video/mp4"
        return b"JPGDATA", "image/jpeg"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    out = await fal_video.run_fal_video(prompt="a wave", key="k")

    assert out == (b"MP4DATA", "video/mp4", b"JPGDATA", "image/jpeg")
    assert urls == ["https://fal.test/out.mp4", "https://fal.test/poster.jpg"]


async def test_run_fal_video_poster_failure_is_non_fatal(monkeypatch) -> None:
    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        return {
            "video": {"url": "https://fal.test/out.mp4"},
            "poster": {"url": "https://fal.test/p.jpg"},
        }

    async def _fake_download(url):
        if url.endswith(".mp4"):
            return b"MP4DATA", "video/mp4"
        raise RuntimeError("poster host down")

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    out = await fal_video.run_fal_video(prompt="a wave", key="k")
    assert out == (b"MP4DATA", "video/mp4", None, None)


async def test_run_fal_video_uses_fal_ai_env_key(monkeypatch) -> None:
    monkeypatch.setenv("FAL_AI_API_KEY", "env-key")
    seen: dict = {}

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        seen["key"] = key
        return {"video": {"url": "https://fal.test/out.mp4"}}

    async def _fake_download(url):
        return b"X", "video/mp4"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    await fal_video.run_fal_video(prompt="a wave")
    assert seen["key"] == "env-key"


async def test_run_fal_video_empty_prompt_is_valueerror() -> None:
    with pytest.raises(ValueError, match="prompt is required"):
        await fal_video.run_fal_video(prompt="   ", key="k")


async def test_run_fal_video_missing_key_is_fal_error(monkeypatch) -> None:
    monkeypatch.delenv("FAL_AI_API_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY", raising=False)
    with pytest.raises(fal_video.FalVideoError, match="API key"):
        await fal_video.run_fal_video(prompt="a wave")


async def test_run_fal_video_no_output_video_is_fal_error(monkeypatch) -> None:
    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        return {"status": "ok"}  # no video / videos / url key

    async def _fake_download(url):
        return b"", "video/mp4"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)
    with pytest.raises(fal_video.FalVideoError, match="no video data"):
        await fal_video.run_fal_video(prompt="a wave", key="k")


async def test_run_fal_video_resolves_key_through_fal_edit(monkeypatch) -> None:
    """run_fal_video resolves the key through fal_edit.fal_api_key (the shared
    FAL_AI_API_KEY → FAL_KEY resolver), so the whole /studio fal surface reads one
    credential."""
    monkeypatch.delenv("FAL_AI_API_KEY", raising=False)
    monkeypatch.setenv("FAL_KEY", "sdk-key")
    seen: dict = {}

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        seen["key"] = key
        return {"video": {"url": "https://fal.test/out.mp4"}}

    async def _fake_download(url):
        return b"X", "video/mp4"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    await fal_video.run_fal_video(prompt="a wave")
    assert seen["key"] == "sdk-key"
    assert fal_edit.fal_api_key() == "sdk-key"
