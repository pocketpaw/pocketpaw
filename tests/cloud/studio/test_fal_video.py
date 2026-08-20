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

import asyncio

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


# ── Image-to-video (the flow passes Image nodes' results into a Video node) ──


def test_resolve_image_to_video_endpoint_single_uses_single() -> None:
    assert (
        fal_video.resolve_image_to_video_endpoint(
            "fal-ai/kling-video/v1/standard/image-to-video", 1
        )
        == "fal-ai/kling-video/v1/standard/image-to-video"
    )


def test_resolve_image_to_video_endpoint_pair_uses_pair_model() -> None:
    assert (
        fal_video.resolve_image_to_video_endpoint(
            "fal-ai/kling-video/v1/standard/image-to-video", 2
        )
        == fal_video.IMAGE_TO_VIDEO_PAIR_MODEL
    )


def test_resolve_image_to_video_endpoint_many_uses_pair_model() -> None:
    """3+ frames chain adjacent 2-frame pairs, so every pair call still uses the
    v1.6 pair endpoint (fal's documented two-frame contract) — never a guessed
    ``keyframes`` shape fal silently ignores."""
    assert (
        fal_video.resolve_image_to_video_endpoint("kling-v2", 3)
        == fal_video.IMAGE_TO_VIDEO_PAIR_MODEL
    )
    assert (
        fal_video.resolve_image_to_video_endpoint("kling-v2", 5)
        == fal_video.IMAGE_TO_VIDEO_PAIR_MODEL
    )


def test_pair_model_points_at_real_fal_endpoint() -> None:
    """The pair endpoint must be the fal endpoint that actually exists AND
    documents ``end_image_url`` (fal.ai docs: ``v1.6/standard/image-to-video``).
    ``v1.5/standard/image-to-video`` does NOT exist on fal (404) and the older
    ``v1/standard`` endpoint only accepts ``image_url`` — both were burned in
    live runs, so lock the real value here."""
    assert fal_video.IMAGE_TO_VIDEO_PAIR_MODEL == "fal-ai/kling-video/v1.6/standard/image-to-video"
    # v1.6 documents both frames — start is required, end is optional.
    assert fal_video.DEFAULT_IMAGE_TO_VIDEO_MODEL == "fal-ai/kling-video/v1/standard/image-to-video"


def test_resolve_image_to_video_endpoint_unknown_single_falls_back() -> None:
    assert (
        fal_video.resolve_image_to_video_endpoint("some-model", 1)
        == fal_video.DEFAULT_IMAGE_TO_VIDEO_MODEL
    )


def test_build_image_to_video_arguments_single() -> None:
    args = fal_video.build_image_to_video_arguments(
        prompt="waves", image_urls=["data:img0"], duration_sec=5, aspect_ratio="16:9"
    )
    assert args == {
        "image_url": "data:img0",
        "prompt": "waves",
        "duration": "5",
        "aspect_ratio": "16:9",
    }


def test_build_image_to_video_arguments_pair_sets_end_image_url() -> None:
    args = fal_video.build_image_to_video_arguments(
        prompt="zoom", image_urls=["data:img0", "data:img1"], duration_sec=10, aspect_ratio="1:1"
    )
    assert args["image_url"] == "data:img0"
    assert args["end_image_url"] == "data:img1"
    assert "keyframes" not in args


def test_build_image_to_video_arguments_three_plus_frames_is_valueerror() -> None:
    """A single fal image-to-video call carries at most 2 frames — passing 3+ to
    the arg builder is a clear error (the caller must chain pairs) rather than a
    silent drop of the extra images."""
    with pytest.raises(ValueError, match="at most 2 frames"):
        fal_video.build_image_to_video_arguments(
            prompt="pan",
            image_urls=["data:a", "data:b", "data:c"],
            duration_sec=5,
            aspect_ratio="16:9",
        )
    with pytest.raises(ValueError, match="at most 2 frames"):
        fal_video.build_image_to_video_arguments(
            prompt="pan",
            image_urls=["data:a", "data:b", "data:c", "data:d", "data:e"],
            duration_sec=5,
            aspect_ratio="16:9",
        )


def test_build_image_to_video_arguments_empty_prompt_omitted() -> None:
    args = fal_video.build_image_to_video_arguments(
        prompt="", image_urls=["data:a"], duration_sec=None, aspect_ratio=None
    )
    assert "prompt" not in args


def test_build_image_to_video_arguments_no_images_is_valueerror() -> None:
    with pytest.raises(ValueError, match="image URL"):
        fal_video.build_image_to_video_arguments(
            prompt="x", image_urls=[], duration_sec=None, aspect_ratio=None
        )


async def test_run_fal_video_single_image_dispatches_image_to_video(monkeypatch) -> None:
    seen: dict = {}

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        seen.update(endpoint=endpoint, arguments=arguments, key=key)
        return {"video": {"url": "https://fal.test/out.mp4"}}

    async def _fake_download(url):
        return b"MP4DATA", "video/mp4"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    out = await fal_video.run_fal_video(
        prompt="", image_urls=["data:img0"], duration_sec=5, aspect_ratio="16:9", key="k"
    )

    assert out == (b"MP4DATA", "video/mp4", None, None)
    assert seen["endpoint"] == fal_video.DEFAULT_IMAGE_TO_VIDEO_MODEL
    assert seen["arguments"]["image_url"] == "data:img0"
    # Empty prompt on the image path falls back to the default motion prompt.
    assert seen["arguments"]["prompt"] == fal_video.DEFAULT_I2V_PROMPT


async def test_run_fal_video_two_images_dispatches_pair(monkeypatch) -> None:
    seen: dict = {}

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        seen.update(endpoint=endpoint, arguments=arguments)
        return {"video": {"url": "https://fal.test/out.mp4"}}

    async def _fake_download(url):
        return b"MP4DATA", "video/mp4"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    await fal_video.run_fal_video(prompt="zoom", image_urls=["data:a", "data:b"], key="k")

    assert seen["endpoint"] == fal_video.IMAGE_TO_VIDEO_PAIR_MODEL
    assert seen["arguments"]["image_url"] == "data:a"
    assert seen["arguments"]["end_image_url"] == "data:b"


async def test_run_fal_video_three_images_chains_pairs_and_concats(monkeypatch) -> None:
    """3 images → TWO 2-frame fal calls (a→b, b→c) on the pair endpoint, then the
    clips are stitched into ONE video. Every image is in the output, in order."""
    calls: list[tuple[str, dict]] = []
    concat_clips: list[tuple[bytes, str]] = []

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        calls.append((endpoint, dict(arguments)))
        return {"video": {"url": f"https://fal.test/clip-{len(calls)}.mp4"}}

    async def _fake_download(url):
        return b"CLIP", "video/mp4"

    async def _fake_concat(clips):
        concat_clips.extend(clips)
        return b"JOINED-MP4", "video/mp4"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)
    monkeypatch.setattr(fal_video, "_concat_videos", _fake_concat)

    out = await fal_video.run_fal_video(
        prompt="pan", image_urls=["data:a", "data:b", "data:c"], duration_sec=5, key="k"
    )

    assert out == (b"JOINED-MP4", "video/mp4", None, None)
    # Exactly (N-1) pair calls, each animating one adjacent 2-frame transition.
    assert [c[0] for c in calls] == [
        fal_video.IMAGE_TO_VIDEO_PAIR_MODEL,
        fal_video.IMAGE_TO_VIDEO_PAIR_MODEL,
    ]
    assert [c[1]["image_url"] for c in calls] == ["data:a", "data:b"]
    assert [c[1]["end_image_url"] for c in calls] == ["data:b", "data:c"]
    assert [c[1]["prompt"] for c in calls] == ["pan", "pan"]
    # Both clips were stitched into one video.
    assert concat_clips == [(b"CLIP", "video/mp4"), (b"CLIP", "video/mp4")]


async def test_run_fal_video_five_images_chains_four_pairs(monkeypatch) -> None:
    """5 images → 4 pair calls; the joins are lossless (same endpoint/params)."""
    seen_calls = 0
    concat_count = 0

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        nonlocal seen_calls
        seen_calls += 1
        return {"video": {"url": f"https://fal.test/c{seen_calls}.mp4"}}

    async def _fake_download(url):
        return b"CLIP", "video/mp4"

    async def _fake_concat(clips):
        nonlocal concat_count
        concat_count = len(clips)
        return b"JOINED", "video/mp4"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)
    monkeypatch.setattr(fal_video, "_concat_videos", _fake_concat)

    await fal_video.run_fal_video(
        prompt="pan",
        image_urls=["data:a", "data:b", "data:c", "data:d", "data:e"],
        key="k",
    )

    assert seen_calls == 4
    assert concat_count == 4


# ── Clip stitching (3+ images) ───────────────────────────────────────────────


def test_concat_ffmpeg_args_uses_concat_demuxer_copy() -> None:
    args = fal_video._concat_ffmpeg_args("/tmp/c.txt", "/tmp/out.mp4")
    assert args == [
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        "/tmp/c.txt",
        "-c",
        "copy",
        "/tmp/out.mp4",
    ]


def test_concat_videos_single_clip_passes_through(monkeypatch) -> None:
    async def _boom(*a, **kw):  # pragma: no cover — must not be reached
        raise AssertionError("no ffmpeg for a single clip")

    monkeypatch.setattr(fal_video, "_run_ffmpeg_concat", _boom)
    out = asyncio.run(fal_video._concat_videos([(b"ONE", "video/mp4")]))
    assert out == (b"ONE", "video/mp4")


async def test_concat_videos_wires_temp_files_and_output(monkeypatch, tmp_path) -> None:
    """Two clips → temp files, a concat list with absolute paths, ffmpeg run via
    the seam, then the joined bytes are read back (mime from the first clip)."""
    from pathlib import Path

    seen_args: dict = {}

    async def _fake_ffmpeg(args):
        concat_file = Path(args[args.index("-i") + 1])
        seen_args["concat"] = concat_file.read_text(encoding="utf-8")
        seen_args["output"] = args[-1]
        Path(args[-1]).write_bytes(b"JOINED")

    monkeypatch.setattr(fal_video, "_run_ffmpeg_concat", _fake_ffmpeg)

    out = await fal_video._concat_videos([(b"CLIP1", "video/mp4"), (b"CLIP2", "video/mp4")])

    assert out == (b"JOINED", "video/mp4")
    entries = [line.strip() for line in seen_args["concat"].splitlines() if line.strip()]
    assert len(entries) == 2
    assert entries[0].startswith("file '") and entries[0].endswith("clip-0.mp4'")
    assert entries[1].startswith("file '") and entries[1].endswith("clip-1.mp4'")
    assert seen_args["output"].endswith("combined.mp4")


async def test_run_ffmpeg_concat_missing_ffmpeg_is_fal_error(monkeypatch) -> None:
    monkeypatch.setattr(fal_video.shutil, "which", lambda _name: None)
    with pytest.raises(fal_video.FalVideoError, match="ffmpeg is not installed"):
        await fal_video._run_ffmpeg_concat(["-y", "-f", "concat", "-i", "x.txt", "out.mp4"])


async def test_run_ffmpeg_concat_nonzero_exit_is_fal_error(monkeypatch) -> None:
    """A failing ffmpeg run surfaces as FalVideoError with the stderr tail."""

    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return None, b"stderr detail line"

    async def _fake_exec(prog, *args, **kw):
        return _FakeProc()

    monkeypatch.setattr(fal_video.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(fal_video.asyncio, "create_subprocess_exec", _fake_exec)

    with pytest.raises(fal_video.FalVideoError, match="exit 1.*stderr detail line"):
        await fal_video._run_ffmpeg_concat(["-y", "-f", "concat", "-i", "x.txt", "out.mp4"])


async def test_run_fal_video_empty_image_list_is_valueerror(monkeypatch) -> None:
    with pytest.raises(ValueError, match="image URL"):
        await fal_video.run_fal_video(prompt="x", image_urls=[], key="k")


async def test_run_fal_video_image_to_video_no_output_is_fal_error(monkeypatch) -> None:
    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        return {"status": "ok"}  # no video data

    async def _fake_download(url):
        return b"", "video/mp4"

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    with pytest.raises(fal_video.FalVideoError, match="no video data"):
        await fal_video.run_fal_video(prompt="x", image_urls=["data:a"], key="k")
