# tests/cloud/studio/test_fal_motion.py — the direct fal.ai Kling MOTION CONTROL client.
#
# fal_motion is the seam that makes the /studio "Motion control" panel work: it
# resolves the model id → fal endpoint, builds the arguments (image_url /
# video_url / character_orientation), runs the endpoint through the fal-client
# SDK, and downloads the result video (+ optional poster). These tests keep the
# fal HTTP layer OUT of the picture — ``_run_fal`` and ``_download`` are
# monkeypatched — so argument building + dispatch + error mapping are asserted
# precisely.
#
# Created 2026-08-24 (studio-motion-control): Kling Motion Control dispatch tests.

from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.cloud.studio import fal_edit, fal_motion


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fal_api_key's env resolution deterministic (same as test_fal_edit)."""
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)


# ── Endpoint resolution ──────────────────────────────────────────────────────


def test_resolve_endpoint_passes_fal_ai_through() -> None:
    assert fal_motion.resolve_endpoint("fal-ai/foo/v2.6/motion-control") == (
        "fal-ai/foo/v2.6/motion-control"
    )


def test_resolve_endpoint_falls_back_to_default() -> None:
    assert fal_motion.resolve_endpoint("kling-motion") == fal_motion.DEFAULT_MOTION_MODEL
    assert fal_motion.resolve_endpoint(None) == fal_motion.DEFAULT_MOTION_MODEL
    assert fal_motion.resolve_endpoint("  ") == fal_motion.DEFAULT_MOTION_MODEL


# ── Argument building ────────────────────────────────────────────────────────


def test_build_arguments_defaults_orientation_to_video() -> None:
    args = fal_motion.build_arguments(
        image_url="data:image/png;base64,aaa", video_url="data:video/mp4;base64,vvv"
    )
    assert args == {
        "image_url": "data:image/png;base64,aaa",
        "video_url": "data:video/mp4;base64,vvv",
        "character_orientation": "video",
    }


def test_build_arguments_explicit_orientation() -> None:
    args = fal_motion.build_arguments(image_url="i", video_url="v", character_orientation="image")
    assert args["character_orientation"] == "image"


# ── Result extraction ────────────────────────────────────────────────────────


def test_extract_video_url_shapes() -> None:
    assert fal_motion._extract_video_url({"video": {"url": "https://fal.test/v.mp4"}}) == (
        "https://fal.test/v.mp4"
    )
    assert fal_motion._extract_video_url({"videos": [{"url": "https://fal.test/a.mp4"}]}) == (
        "https://fal.test/a.mp4"
    )
    assert fal_motion._extract_video_url({"url": "https://fal.test/b.mp4"}) == (
        "https://fal.test/b.mp4"
    )
    assert fal_motion._extract_video_url({}) is None


def test_extract_poster_url_shapes() -> None:
    assert fal_motion._extract_poster_url({"poster": {"url": "https://fal.test/p.jpg"}}) == (
        "https://fal.test/p.jpg"
    )
    assert fal_motion._extract_poster_url({"thumbnails": [{"url": "https://fal.test/t.jpg"}]}) == (
        "https://fal.test/t.jpg"
    )
    assert fal_motion._extract_poster_url({}) is None


# ── Dispatch ─────────────────────────────────────────────────────────────────


async def test_run_fal_motion_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fal result → downloaded mp4 (+ poster) is returned; the endpoint,
    arguments, and resolved key are asserted."""
    seen: dict = {}

    async def _fake_run(endpoint, arguments, *, key, client_timeout=None, start_timeout=None):
        seen.update(endpoint=endpoint, arguments=arguments, key=key)
        return {
            "video": {"url": "https://fal.test/out.mp4"},
            "poster": {"url": "https://fal.test/p.jpg"},
        }

    async def _fake_download(url):
        if url.endswith("p.jpg"):
            return b"poster", "image/jpeg"
        return b"mp4", "video/mp4"

    monkeypatch.setattr(fal_motion, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_motion, "_download", _fake_download)
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")

    out = await fal_motion.run_fal_motion(
        image_url="data:image/png;base64,aaa",
        video_url="data:video/mp4;base64,vvv",
        character_orientation="video",
    )

    assert out == (b"mp4", "video/mp4", b"poster", "image/jpeg")
    assert seen["key"] == "sk-fal"
    assert seen["endpoint"] == fal_motion.DEFAULT_MOTION_MODEL
    assert seen["arguments"]["image_url"] == "data:image/png;base64,aaa"
    assert seen["arguments"]["video_url"] == "data:video/mp4;base64,vvv"
    assert seen["arguments"]["character_orientation"] == "video"


async def test_run_fal_motion_requires_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")
    with pytest.raises(ValueError, match="character image"):
        await fal_motion.run_fal_motion(image_url="  ", video_url="v")


async def test_run_fal_motion_requires_video(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")
    with pytest.raises(ValueError, match="motion reference video"):
        await fal_motion.run_fal_motion(image_url="i", video_url="  ")


async def test_run_fal_motion_rejects_bad_orientation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")
    with pytest.raises(ValueError, match="character_orientation"):
        await fal_motion.run_fal_motion(
            image_url="i", video_url="v", character_orientation="diagonal"
        )


async def test_run_fal_motion_missing_key_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: None)
    with pytest.raises(fal_motion.FalMotionError, match="API key"):
        await fal_motion.run_fal_motion(image_url="i", video_url="v")


async def test_run_fal_motion_no_video_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run(endpoint, arguments, *, key, client_timeout=None, start_timeout=None):
        return {"status": "done"}

    monkeypatch.setattr(fal_motion, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")
    with pytest.raises(fal_motion.FalMotionError, match="no video"):
        await fal_motion.run_fal_motion(image_url="i", video_url="v")


async def test_run_fal_motion_upstream_failure_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FalMotionError raised by the SDK seam propagates (run_fal_motion
    doesn't swallow it — the real `_run_fal` wraps raw upstream errors)."""

    async def _boom(endpoint, arguments, *, key, client_timeout=None, start_timeout=None):
        raise fal_motion.FalMotionError("fal motion-control 'x' failed: upstream 500")

    monkeypatch.setattr(fal_motion, "_run_fal", _boom)
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")
    with pytest.raises(fal_motion.FalMotionError, match="upstream 500"):
        await fal_motion.run_fal_motion(image_url="i", video_url="v")


async def test_run_fal_maps_4xx_to_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx FalClientHTTPError from the SDK (fal rejected the inputs) maps to
    FalMotionValidationError, which the service turns into a 400."""
    import fal_client

    def _http_422() -> fal_client.FalClientHTTPError:
        request = httpx.Request("POST", "https://fal.run/x")
        response = httpx.Response(
            422,
            request=request,
            json=[{"msg": "Image dimensions are too small. Minimum 340x340."}],
        )
        return fal_client.FalClientHTTPError(str(response.json()), 422, {}, response)

    class _FakeAsyncClient:
        def __init__(self, *, key, default_timeout): ...

        async def run(self, *args, **kwargs):
            raise _http_422()

    monkeypatch.setattr(fal_client, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")
    with pytest.raises(fal_motion.FalMotionValidationError, match="rejected"):
        await fal_motion.run_fal_motion(image_url="i", video_url="v")
