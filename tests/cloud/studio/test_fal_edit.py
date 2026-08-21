# tests/cloud/studio/test_fal_edit.py — the direct fal.ai image-EDIT client.
#
# fal_edit is the seam that makes the /studio canvas edit ops work: it resolves
# the op → fal endpoint, builds the arguments (source image as a ``data:`` URL),
# runs the endpoint through the fal-client SDK, and downloads the result bytes.
# These tests keep the fal HTTP layer OUT of the picture — ``_run_fal`` and
# ``_download`` are monkeypatched — so the argument building + dispatch +
# error mapping are asserted precisely. Coverage:
#   * fal_api_key — FAL_AI_API_KEY preferred, FAL_KEY fallback, None when unset,
#     and .env-loading regression (the serve process never merges .env into
#     os.environ, so fal_api_key must load it itself — this fixed the edit 502).
#   * encode_bytes / mime_to_ext — data: URL shape + storage extension mapping.
#   * build_arguments — per-op argument shapes (remove-bg / upscale / expand /
#     variations / edit / inpaint-with-mask / sketch-to-image), prompt guards.
#   * _extract_image_urls — images[] list, single image, malformed entries.
#   * run_fal_edit — dispatch (endpoint + arguments + key), env-key fallback,
#     ValueError guards (unknown op / model / empty image), FalEditError for
#     missing key and for a result with no output images.
#
# Created 2026-08-18 (studio-fal-edit): direct fal edit dispatch tests.

from __future__ import annotations

import base64

import dotenv
import pytest
from pocketpaw_ee.cloud.studio import fal_edit

_DATA = "data:image/png;base64," + base64.b64encode(b"src").decode()

# Captured BEFORE the autouse fixture patches the module, so the .env-loading
# regression test can call the real dotenv loader on a tmp file.
_ORIG_LOAD_DOTENV = dotenv.load_dotenv


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fal_api_key's env resolution deterministic: fal_api_key now loads
    .env itself, and the repo .env would otherwise leak FAL_AI_API_KEY into any
    test that deletes the keys. The .env-loading regression test re-patches with
    a real dotenv file."""
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)


# ── Config / encoding helpers ────────────────────────────────────────────────


def test_fal_api_key_prefers_fal_ai_env(monkeypatch) -> None:
    monkeypatch.setenv("FAL_AI_API_KEY", "  fal-key  ")
    monkeypatch.setenv("FAL_KEY", "fallback")
    assert fal_edit.fal_api_key() == "fal-key"


def test_fal_api_key_falls_back_to_fal_key(monkeypatch) -> None:
    monkeypatch.delenv("FAL_AI_API_KEY", raising=False)
    monkeypatch.setenv("FAL_KEY", "sdk-key")
    assert fal_edit.fal_api_key() == "sdk-key"


def test_fal_api_key_none_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("FAL_AI_API_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY", raising=False)
    assert fal_edit.fal_api_key() is None


def test_fal_api_key_loads_dotenv_when_not_exported(monkeypatch, tmp_path) -> None:
    """Regression for the /studio/edit 502: the serve process never merges .env
    into os.environ (pydantic Settings reads it into the model only, and the
    POCKETPAW_ prefix excludes FAL_AI_API_KEY), so fal_api_key must load .env
    itself. An already-exported shell var still wins (load_dotenv is a
    no-override merge), but a .env-only key is now found."""
    monkeypatch.delenv("FAL_AI_API_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("FAL_AI_API_KEY=env-file-key\n")

    monkeypatch.setattr(
        dotenv, "load_dotenv", lambda *a, **kw: _ORIG_LOAD_DOTENV(dotenv_path=env_file)
    )
    assert fal_edit.fal_api_key() == "env-file-key"


def test_fal_api_key_exported_var_wins_over_dotenv(monkeypatch, tmp_path) -> None:
    """load_dotenv must not clobber a key the shell already exported."""
    monkeypatch.setenv("FAL_AI_API_KEY", "exported-key")
    env_file = tmp_path / ".env"
    env_file.write_text("FAL_AI_API_KEY=env-file-key\n")

    monkeypatch.setattr(
        dotenv, "load_dotenv", lambda *a, **kw: _ORIG_LOAD_DOTENV(dotenv_path=env_file)
    )
    assert fal_edit.fal_api_key() == "exported-key"


def test_encode_bytes_produces_data_url() -> None:
    assert fal_edit.encode_bytes(b"abc") == "data:image/png;base64,YWJj"
    assert fal_edit.encode_bytes(b"abc", "image/jpeg") == "data:image/jpeg;base64,YWJj"


@pytest.mark.parametrize(
    ("mime", "ext"),
    [
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("image/jpg", "jpg"),
        ("image/webp", "webp"),
        ("image/gif", "gif"),
        ("image/png; charset=utf-8", "png"),
        ("", "png"),
        ("application/octet-stream", "png"),
    ],
)
def test_mime_to_ext(mime: str, ext: str) -> None:
    assert fal_edit.mime_to_ext(mime) == ext


# ── Argument building ────────────────────────────────────────────────────────


def test_build_arguments_remove_bg() -> None:
    assert fal_edit.build_arguments(op="remove-bg", image_data_url=_DATA) == {"image_url": _DATA}


def test_build_arguments_upscale_defaults_to_2() -> None:
    args = fal_edit.build_arguments(op="upscale", image_data_url=_DATA)
    assert args == {"image_url": _DATA, "upscale_factor": 2}


def test_build_arguments_upscale_factor_4() -> None:
    args = fal_edit.build_arguments(op="upscale", image_data_url=_DATA, factor=4)
    assert args == {"image_url": _DATA, "upscale_factor": 4}


def test_build_arguments_upscale_invalid_factor_clamps() -> None:
    args = fal_edit.build_arguments(op="upscale", image_data_url=_DATA, factor=8)
    assert args["upscale_factor"] == 2


def test_build_arguments_expand_builds_outpaint_prompt() -> None:
    args = fal_edit.build_arguments(op="expand", image_data_url=_DATA, direction="up", factor=1.5)
    assert args["image_urls"] == [_DATA]
    assert "upward" in args["prompt"]
    assert "150%" in args["prompt"]


def test_build_arguments_expand_defaults_all_sides() -> None:
    args = fal_edit.build_arguments(op="expand", image_data_url=_DATA)
    assert "all sides" in args["prompt"]


def test_build_arguments_variations_uses_default_prompt() -> None:
    args = fal_edit.build_arguments(op="variations", image_data_url=_DATA)
    assert "variation" in args["prompt"].lower()
    assert args["image_urls"] == [_DATA]


def test_build_arguments_edit_requires_prompt() -> None:
    with pytest.raises(ValueError, match="prompt is required"):
        fal_edit.build_arguments(op="edit", image_data_url=_DATA)


def test_build_arguments_edit_uses_prompt() -> None:
    args = fal_edit.build_arguments(op="edit", image_data_url=_DATA, prompt="make it sunset")
    assert args == {"prompt": "make it sunset", "image_urls": [_DATA]}


def test_build_arguments_inpaint_defaults_prompt_and_mask() -> None:
    mask = "data:image/png;base64," + base64.b64encode(b"mask").decode()
    args = fal_edit.build_arguments(op="inpaint", image_data_url=_DATA, mask_data_url=mask)
    assert "masked region" in args["prompt"]
    assert args["image_urls"] == [_DATA]
    assert args["mask_url"] == mask


def test_build_arguments_inpaint_no_mask_omits_mask_url() -> None:
    args = fal_edit.build_arguments(op="inpaint", image_data_url=_DATA)
    assert "mask_url" not in args
    assert args["image_urls"] == [_DATA]


def test_build_arguments_sketch_default_prompt() -> None:
    args = fal_edit.build_arguments(op="sketch-to-image", image_data_url=_DATA)
    assert "polished" in args["prompt"]
    # Seedream keeps the single image_url shape (unlike nano-banana's array).
    assert args["image_url"] == _DATA
    assert "image_urls" not in args


def test_build_arguments_sketch_appends_user_text() -> None:
    args = fal_edit.build_arguments(op="sketch-to-image", image_data_url=_DATA, prompt="a dragon")
    assert "a dragon" in args["prompt"]
    assert "polished" in args["prompt"]


# ── Result extraction ────────────────────────────────────────────────────────


def test_extract_image_urls_from_images_list() -> None:
    result = {"images": [{"url": "a.png"}, {"url": "b.png"}]}
    assert fal_edit._extract_image_urls(result) == ["a.png", "b.png"]


def test_extract_image_urls_from_single_image() -> None:
    result = {"image": {"url": "c.png"}}
    assert fal_edit._extract_image_urls(result) == ["c.png"]


def test_extract_image_urls_handles_both_shapes() -> None:
    result = {
        "images": [{"url": "a.png"}],
        "image": {"url": "b.png"},
    }
    assert fal_edit._extract_image_urls(result) == ["a.png", "b.png"]


def test_extract_image_urls_drops_malformed_entries() -> None:
    result = {
        "images": [{"url": "a.png"}, {}, {"url": ""}],
        "image": {},
    }
    assert fal_edit._extract_image_urls(result) == ["a.png"]


def test_extract_image_urls_empty_result() -> None:
    assert fal_edit._extract_image_urls({}) == []


# ── run_fal_edit dispatch ────────────────────────────────────────────────────


async def test_run_fal_edit_dispatches_and_downloads(monkeypatch) -> None:
    calls: dict = {}

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        calls["endpoint"] = endpoint
        calls["arguments"] = arguments
        calls["key"] = key
        return {"images": [{"url": "https://fal.test/out.png", "width": 512, "height": 512}]}

    async def _fake_download(url):
        calls["url"] = url
        return b"PNGDATA", "image/png"

    monkeypatch.setattr(fal_edit, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_edit, "_download", _fake_download)

    out = await fal_edit.run_fal_edit(op="upscale", image_data_url=_DATA, factor=4, key="fal-key")

    assert out == [(b"PNGDATA", "image/png")]
    assert calls["endpoint"] == "fal-ai/esrgan"
    assert calls["arguments"] == {"image_url": _DATA, "upscale_factor": 4}
    assert calls["key"] == "fal-key"
    assert calls["url"] == "https://fal.test/out.png"


async def test_run_fal_edit_uses_fal_ai_env_key(monkeypatch) -> None:
    monkeypatch.setenv("FAL_AI_API_KEY", "env-key")
    seen: dict = {}

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        seen["key"] = key
        return {"image": {"url": "https://fal.test/out.png"}}

    async def _fake_download(url):
        return b"X", "image/png"

    monkeypatch.setattr(fal_edit, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_edit, "_download", _fake_download)

    await fal_edit.run_fal_edit(op="remove-bg", image_data_url=_DATA)
    assert seen["key"] == "env-key"


async def test_run_fal_edit_unknown_op_is_valueerror() -> None:
    with pytest.raises(ValueError, match="unknown edit op"):
        await fal_edit.run_fal_edit(op="warp", image_data_url=_DATA, key="k")


async def test_run_fal_edit_unknown_model_is_valueerror() -> None:
    with pytest.raises(ValueError, match="unknown edit model"):
        await fal_edit.run_fal_edit(
            op="upscale", image_data_url=_DATA, model="fal-ai/some-other", key="k"
        )


async def test_run_fal_edit_empty_image_is_valueerror() -> None:
    with pytest.raises(ValueError, match="image_data_url is required"):
        await fal_edit.run_fal_edit(op="upscale", image_data_url="", key="k")


async def test_run_fal_edit_missing_key_is_fal_error(monkeypatch) -> None:
    monkeypatch.delenv("FAL_AI_API_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY", raising=False)
    with pytest.raises(fal_edit.FalEditError, match="API key"):
        await fal_edit.run_fal_edit(op="upscale", image_data_url=_DATA)


async def test_run_fal_edit_no_output_images_is_fal_error(monkeypatch) -> None:
    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        return {"status": "ok"}  # no images[] / image key

    async def _fake_download(url):
        return b"", "image/png"

    monkeypatch.setattr(fal_edit, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_edit, "_download", _fake_download)
    with pytest.raises(fal_edit.FalEditError, match="no image data"):
        await fal_edit.run_fal_edit(op="remove-bg", image_data_url=_DATA, key="k")


async def test_run_fal_edit_missing_prompt_raises_valueerror(monkeypatch) -> None:
    """A prompt-driven op with no prompt surfaces ValueError before any fal call,
    so the router can answer 400 (not a doomed upstream round-trip)."""

    async def _fake_run(endpoint, arguments, *, key, client_timeout=..., start_timeout=...):
        raise AssertionError("should not reach fal without a prompt")

    monkeypatch.setattr(fal_edit, "_run_fal", _fake_run)
    with pytest.raises(ValueError, match="prompt is required"):
        await fal_edit.run_fal_edit(op="edit", image_data_url=_DATA, key="k")
