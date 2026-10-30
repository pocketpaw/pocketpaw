# tests/cloud/studio/test_fal_elements.py — the direct fal.ai Kling ELEMENTS client.
#
# fal_elements is the seam that makes the /studio "Edit video" panel work: it
# resolves the model id → fal endpoint, builds the arguments (prompt /
# input_image_urls / video_url / duration / aspect_ratio), runs the endpoint
# through the fal-client SDK, and downloads the result video (+ optional poster).
# These tests keep the fal HTTP layer OUT of the picture — ``_run_fal`` and
# ``_download`` are monkeypatched — so argument building + dispatch + error
# mapping are asserted precisely.
#
# Created 2026-08-24 (studio-video-elements): Kling Elements dispatch tests.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import fal_edit, fal_elements


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fal_api_key's env resolution deterministic (same as test_fal_edit)."""
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)


# ── Endpoint resolution ──────────────────────────────────────────────────────


def test_resolve_endpoint_passes_fal_ai_through() -> None:
    assert fal_elements.resolve_endpoint("fal-ai/foo/v1/elements") == "fal-ai/foo/v1/elements"


def test_resolve_endpoint_falls_back_to_default() -> None:
    assert fal_elements.resolve_endpoint("kling-elements") == fal_elements.DEFAULT_ELEMENTS_MODEL
    assert fal_elements.resolve_endpoint(None) == fal_elements.DEFAULT_ELEMENTS_MODEL
    assert fal_elements.resolve_endpoint("  ") == fal_elements.DEFAULT_ELEMENTS_MODEL


# ── Argument building ────────────────────────────────────────────────────────


def test_build_arguments_prompt_only() -> None:
    assert fal_elements.build_arguments(prompt="a scene") == {"prompt": "a scene"}


def test_build_arguments_full() -> None:
    args = fal_elements.build_arguments(
        prompt="add a cow",
        input_image_urls=["data:image/png;base64,aaa", "  ", "data:image/png;base64,bbb"],
        video_url="data:video/mp4;base64,vvv",
        duration_sec=5,
        aspect_ratio="16:9",
    )
    assert args == {
        "prompt": "add a cow",
        "input_image_urls": ["data:image/png;base64,aaa", "data:image/png;base64,bbb"],
        "video_url": "data:video/mp4;base64,vvv",
        "duration": "5",
        "aspect_ratio": "16:9",
    }


def test_build_arguments_omits_blanks() -> None:
    args = fal_elements.build_arguments(
        prompt="p", input_image_urls=["  "], video_url="  ", duration_sec=0, aspect_ratio=None
    )
    assert args == {"prompt": "p"}


# ── Result extraction ────────────────────────────────────────────────────────


def test_extract_video_url_shapes() -> None:
    assert fal_elements._extract_video_url({"video": {"url": "https://fal.test/v.mp4"}}) == (
        "https://fal.test/v.mp4"
    )
    assert (
        fal_elements._extract_video_url({"videos": [{"url": "https://fal.test/a.mp4"}]})
        == "https://fal.test/a.mp4"
    )
    assert fal_elements._extract_video_url({"url": "https://fal.test/b.mp4"}) == (
        "https://fal.test/b.mp4"
    )
    assert fal_elements._extract_video_url({}) is None


def test_extract_poster_url_shapes() -> None:
    assert fal_elements._extract_poster_url({"poster": {"url": "https://fal.test/p.jpg"}}) == (
        "https://fal.test/p.jpg"
    )
    assert (
        fal_elements._extract_poster_url({"thumbnails": [{"url": "https://fal.test/t.jpg"}]})
        == "https://fal.test/t.jpg"
    )
    assert fal_elements._extract_poster_url({}) is None


# ── Dispatch ─────────────────────────────────────────────────────────────────


async def test_run_fal_elements_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(fal_elements, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_elements, "_download", _fake_download)
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")

    out = await fal_elements.run_fal_elements(
        prompt="add a cow",
        input_image_urls=["data:image/png;base64,aaa"],
        video_url="data:video/mp4;base64,vvv",
        duration_sec=5,
        aspect_ratio="16:9",
    )

    assert out == (b"mp4", "video/mp4", b"poster", "image/jpeg")
    assert seen["key"] == "sk-fal"
    assert seen["endpoint"] == fal_elements.DEFAULT_ELEMENTS_MODEL
    assert seen["arguments"]["input_image_urls"] == ["data:image/png;base64,aaa"]
    assert seen["arguments"]["video_url"] == "data:video/mp4;base64,vvv"


async def test_run_fal_elements_requires_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")
    with pytest.raises(ValueError):
        await fal_elements.run_fal_elements(prompt="  ", input_image_urls=[], video_url=None)


async def test_run_fal_elements_missing_key_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: None)
    with pytest.raises(fal_elements.FalElementsError, match="API key"):
        await fal_elements.run_fal_elements(prompt="a scene")


async def test_run_fal_elements_no_video_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run(endpoint, arguments, *, key, client_timeout=None, start_timeout=None):
        return {"status": "done"}

    monkeypatch.setattr(fal_elements, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")
    with pytest.raises(fal_elements.FalElementsError, match="no video"):
        await fal_elements.run_fal_elements(prompt="a scene")


async def test_run_fal_elements_upstream_failure_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FalElementsError raised by the SDK seam propagates (run_fal_elements
    doesn't swallow it — the real `_run_fal` wraps raw upstream errors)."""

    async def _boom(endpoint, arguments, *, key, client_timeout=None, start_timeout=None):
        raise fal_elements.FalElementsError("fal elements 'x' failed: upstream 500")

    monkeypatch.setattr(fal_elements, "_run_fal", _boom)
    monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "sk-fal")
    with pytest.raises(fal_elements.FalElementsError, match="upstream 500"):
        await fal_elements.run_fal_elements(prompt="a scene")
