# tests/cloud/studio/test_service.py — the direct /studio generation service.
#
# The proxy calls are mocked at the SAME seam the media MCP uses
# (``service._PROXY_TRANSPORT`` → httpx.MockTransport) so the full request
# shape — path, model, prompt, size, count, the OpenAI ``user`` tenant tag, and
# the Bearer key — is asserted end-to-end without a live proxy. The media
# storage adapter and ``_history_path`` are redirected to tmp dirs so nothing
# touches the developer's real ``~/.pocketpaw``. Coverage:
#   * list_models — catalog image/video entries map onto StudioModel shapes,
#     first image model is the catalog default, chat entries are excluded.
#   * generate (image) — happy path (b64_json): POSTs the right payload, saves a
#     PNG into the generated dir, returns a succeeded Generation, and persists it
#     to the workspace history.
#   * generate url path — a ``data[0].url`` result is fetched + saved too.
#   * generate style suffix — the style promptSuffix is appended server-side.
#   * generate guards — empty prompt / missing model are ValueError.
#   * generate (video) — a fal video result is saved (mp4 + optional poster),
#     aliases resolve onto a fal endpoint, a fal failure surfaces as
#     StudioUpstreamError, and nothing is persisted on failure.
#   * proxy failure — a 500 from the proxy surfaces as StudioUpstreamError.
#   * history scoping — list_generations / get_generation are workspace-scoped.
#   * edit — dispatch (monkeypatched fal_edit.run_fal_edit), stored-media source
#     resolution → data URL, unknown op → StudioNotSupported, missing source →
#     ValueError, fal failure → StudioUpstreamError.
#
# Created 2026-08-17 (studio-real-backend): new service tests.

from __future__ import annotations

import base64
import json

import httpx
import pocketpaw_ee.cloud.studio.service as service
import pytest
from pocketpaw_ee.catalog import service as catalog_service
from pocketpaw_ee.catalog.litellm_client import CatalogUpstreamError
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry, Pricing
from pocketpaw_ee.cloud.media import storage as media_storage
from pocketpaw_ee.cloud.studio import (
    fal_edit,
    fal_elements,
    fal_image,
    fal_motion,
    fal_music,
    fal_video,
    schemas,
)

from pocketpaw.uploads.local import LocalStorageAdapter

_PROXY_BASE = "https://proxy.test:4000"
_PROXY_KEY = "sk-proxy-master"
_DATA_URL = "data:image/png;base64,c3Jj"  # base64("src")


def _entry(
    id_: str,
    *,
    provider: str,
    modality: Modality,
    description: str | None = None,
) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        id=id_,
        display_name=id_.split("/", 1)[-1],
        provider=provider,
        modality=modality,
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=2.0),
        capabilities=[],
        description=description,
    )


@pytest.fixture
def proxy_env(monkeypatch):
    """Point the catalog proxy config (which studio reuses) at a fake proxy with
    a key so the Bearer header + base URL are exercised end-to-end."""
    monkeypatch.setenv("POCKETPAW_LITELLM_API_BASE", _PROXY_BASE)
    monkeypatch.setenv("POCKETPAW_LITELLM_API_KEY", _PROXY_KEY)


@pytest.fixture
def studio_env(tmp_path, monkeypatch):
    """Redirect media storage + history persistence + flow-project persistence
    into tmp dirs so tests never touch the real ~/.pocketpaw, and resolve the
    tenant key to a fixed value.

    The media storage adapter is swapped for a tmp-backed LOCAL adapter, so a
    generated PNG lands at ``<media_root>/generated/<name>`` — the same layout
    the deployed S3 adapter uses (key "generated/<name>")."""
    media_root = tmp_path / "media-root"
    media_root.mkdir(exist_ok=True)
    generated = media_root / "generated"
    generated.mkdir(exist_ok=True)
    monkeypatch.setattr(media_storage, "_ADAPTER", LocalStorageAdapter(root=media_root))
    history = tmp_path / "studio" / "generations.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(service, "_history_path", lambda: history)
    projects = tmp_path / "studio" / "flow-projects.jsonl"
    projects.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(service, "_projects_path", lambda: projects)

    async def _tenant_key(workspace_id):
        return "sk-tenant-ws"

    monkeypatch.setattr(service, "_resolve_auth_key", _tenant_key)
    return generated, history


def _install_transport(monkeypatch, handler) -> dict:
    """Install an httpx.MockTransport on service._PROXY_TRANSPORT and capture
    every request the handler sees (asserts the proxy wire shape)."""
    captured: dict = {"requests": []}

    def _wrapped(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(request)
        return handler(request)

    monkeypatch.setattr(service, "_PROXY_TRANSPORT", httpx.MockTransport(_wrapped))
    return captured


# ── Model mapping ────────────────────────────────────────────────────────────


async def test_list_models_maps_image_and_video_entries(monkeypatch) -> None:
    """Image entries map to StudioModel picker rows (first image is default),
    video entries are included, chat/embedding entries are excluded."""
    entries = [
        _entry("fal_ai/fal-ai/flux/schnell", provider="fal_ai", modality=Modality.IMAGE),
        _entry("fal_ai/fal-ai/gpt-image-1", provider="fal_ai", modality=Modality.IMAGE),
        _entry("fal_ai/fal-ai/kling/v2", provider="fal_ai", modality=Modality.VIDEO),
        _entry("anthropic/claude-3-5-sonnet", provider="anthropic", modality=Modality.CHAT),
        _entry("openai/text-embedding-3-small", provider="openai", modality=Modality.EMBEDDING),
    ]

    async def _list(**kw):
        return entries

    monkeypatch.setattr(catalog_service, "list_models", _list)

    models = await service.list_models()

    ids = [m.id for m in models]
    assert ids[:3] == [
        "fal_ai/fal-ai/flux/schnell",
        "fal_ai/fal-ai/gpt-image-1",
        "fal_ai/fal-ai/kling/v2",
    ]
    flux = models[0]
    assert flux.kind == "image"
    assert flux.default is True
    assert flux.maxCount == 1
    assert flux.provider == "fal_ai"
    assert flux.label == "Flux Schnell"
    assert "1:1" in flux.aspectRatios and "16:9" in flux.aspectRatios
    assert flux.supportsNegativePrompt is False
    gpt = models[1]
    assert gpt.default is False
    video = models[2]
    assert video.kind == "video"
    assert video.durationsSec == [2, 5, 10]

    # Curated fal registries (the movie-maker surface) are appended after the
    # LiteLLM-derived rows, deduplicated by id.
    assert "fal-ai/nano-banana-2" in ids
    assert "openai/gpt-image-2" in ids
    assert "bytedance/seedance-2.5/enterprise/text-to-video" in ids
    assert "google/gemini-omni-flash/edit" in ids
    audio = [m for m in models if m.kind == "audio"]
    assert {m.id for m in audio} == {
        "fal-ai/elevenlabs/music",
        "fal-ai/ace-step-1.5",
        "fal-ai/ace-step/prompt-to-audio",
    }


async def test_list_models_appends_fal_video_fallback_when_catalog_has_none(
    monkeypatch,
) -> None:
    """When the proxy catalog serves no video entries, the fal video model is
    still surfaced so the rail's Video kind offers a picker row with the
    2s / 5s / 10s duration set (video generation runs directly against fal)."""
    entries = [
        _entry("fal_ai/fal-ai/flux/schnell", provider="fal_ai", modality=Modality.IMAGE),
        _entry("anthropic/claude-3-5-sonnet", provider="anthropic", modality=Modality.CHAT),
    ]

    async def _list(**kw):
        return entries

    monkeypatch.setattr(catalog_service, "list_models", _list)

    models = await service.list_models()

    video = [m for m in models if m.kind == "video"]
    fallback = next(m for m in video if m.id == fal_video.DEFAULT_VIDEO_MODEL)
    assert fallback.provider == "fal"
    assert fallback.durationsSec == [2, 5, 10]
    assert fallback.default is True
    assert fallback.aspectRatios == ["16:9", "9:16", "1:1"]
    # The curated video registries are appended alongside the fallback.
    video_ids = {m.id for m in video}
    assert "bytedance/seedance-2.5/enterprise/text-to-video" in video_ids
    assert "google/gemini-omni-flash/edit" in video_ids


async def test_list_models_does_not_append_fallback_when_video_served(
    monkeypatch,
) -> None:
    """A catalog that already serves video keeps exactly those entries — no
    fallback is appended."""
    entries = [
        _entry("fal_ai/fal-ai/flux/schnell", provider="fal_ai", modality=Modality.IMAGE),
        _entry("fal_ai/fal-ai/kling/v2", provider="fal_ai", modality=Modality.VIDEO),
    ]

    async def _list(**kw):
        return entries

    monkeypatch.setattr(catalog_service, "list_models", _list)

    models = await service.list_models()

    videos = [m for m in models if m.kind == "video"]
    assert videos[0].id == "fal_ai/fal-ai/kling/v2"
    assert videos[0].durationsSec == [2, 5, 10]
    # No fallback row is appended (its label would be "Kling Video 1.0"), but the
    # curated video registries still are.
    assert not any(m.label == "Kling Video 1.0" for m in videos)
    assert "bytedance/seedance-2.5/enterprise/text-to-video" in {m.id for m in videos}


async def test_list_models_upstream_failure_propagates(monkeypatch) -> None:
    """A catalog outage propagates CatalogUpstreamError so the router can 502."""

    async def _boom(**kw):
        raise CatalogUpstreamError("proxy down")

    monkeypatch.setattr(catalog_service, "list_models", _boom)
    with pytest.raises(CatalogUpstreamError):
        await service.list_models()


async def test_curated_image_models_expose_edit_params(monkeypatch) -> None:
    """Curated image models surface their per-model edit knobs from
    ``fal_image.MODEL_PARAMS`` — only the edit-capable models carry any."""

    async def _list(**kw):
        return []

    monkeypatch.setattr(catalog_service, "list_models", _list)

    models = await service.list_models()
    by_id = {m.id: m for m in models}

    nana = by_id["fal-ai/nano-banana-2"]
    assert {p.key for p in nana.params} == {
        "num_images",
        "seed",
        "output_format",
        "safety_tolerance",
    }
    num = next(p for p in nana.params if p.key == "num_images")
    assert num.type == "stepper" and num.min == 1 and num.max == 4

    gpt = by_id["openai/gpt-image-2"]
    assert {p.key for p in gpt.params} == {
        "quality",
        "num_images",
        "size",
        "background",
        "output_format",
        "seed",
    }

    seedream = by_id["bytedance/seedream/v5/pro/text-to-image"]
    assert {p.key for p in seedream.params} == {
        "num_images",
        "resolution",
        "output_format",
        "seed",
    }

    grok = by_id["xai/grok-imagine-image/v2.0/text-to-image"]
    assert {p.key for p in grok.params} == {
        "resolution",
        "quality",
        "num_images",
        "output_format",
        "seed",
    }

    # Non-edit curated image models expose no params.
    assert by_id["fal-ai/recraft/v3/text-to-image"].params == []


# ── generate (image) ─────────────────────────────────────────────────────────


async def test_generate_image_happy_path(monkeypatch, proxy_env, studio_env) -> None:
    """A b64_json proxy result → the PNG is saved under generated/, a succeeded
    Generation is returned, and the history records it under the workspace."""
    generated, history = studio_env
    png = b"\x89PNG\r\n\x1a\nfake-image"
    b64 = base64.b64encode(png).decode()
    _CINEMATIC_SUFFIX = (
        ", cinematic lighting, shallow depth of field, film grain, dramatic composition"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{_PROXY_BASE}/v1/images/generations"
        assert request.headers["Authorization"] == "Bearer sk-tenant-ws"
        body = json.loads(request.content)
        assert body["model"] == "fal_ai/fal-ai/flux/schnell"
        assert body["prompt"] == "a red bicycle" + _CINEMATIC_SUFFIX
        assert body["size"] == "1024x1024"  # 1:1 mapped
        assert body["n"] == 1
        assert body["user"] == "ws-1"  # workspace tenant tag
        return httpx.Response(200, json={"data": [{"b64_json": b64}]})

    captured = _install_transport(monkeypatch, handler)

    req = schemas.GenerateRequest(
        prompt="a red bicycle",
        kind="image",
        model="fal_ai/fal-ai/flux/schnell",
        aspectRatio="1:1",
        count=1,
        styleId="cinematic",
    )
    gen = await service.generate(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert gen.kind == "image"
    assert gen.model == "fal_ai/fal-ai/flux/schnell"
    assert gen.prompt == "a red bicycle" + _CINEMATIC_SUFFIX
    assert gen.params.styleId == "cinematic"
    assert len(gen.assets) == 1
    url = gen.assets[0].url
    assert url.startswith("/api/v1/media/")
    name = url.rsplit("/", 1)[-1]
    assert (generated / name).read_bytes() == png
    assert len(captured["requests"]) == 1

    # History: one record, scoped to ws-1, re-readable as a wire Generation.
    records = service._load_history()
    assert len(records) == 1
    assert records[0]["_workspace"] == "ws-1"
    listed = service.list_generations("ws-1")
    assert [g.id for g in listed] == [gen.id]
    assert service.get_generation(gen.id, "ws-1") is not None
    # A different workspace does not see it.
    assert service.list_generations("ws-other") == []
    assert service.get_generation(gen.id, "ws-other") is None
    # The media router exclusion sees the saved file.
    assert name in service.tracked_generation_filenames()


async def test_generate_image_url_path(monkeypatch, proxy_env, studio_env) -> None:
    """A proxy ``data[0].url`` result (dall-e style) is fetched and saved too."""
    generated, _ = studio_env
    png = b"\x89PNG\r\n\x1a\nfake-from-url"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/generations":
            return httpx.Response(200, json={"data": [{"url": "http://cdn.test/img.png"}]})
        if request.url.path == "/img.png":
            return httpx.Response(200, content=png)
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)

    req = schemas.GenerateRequest(
        prompt="a cat",
        kind="image",
        model="fal_ai/fal-ai/flux/schnell",
        aspectRatio="16:9",
        count=1,
    )
    gen = await service.generate(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    name = gen.assets[0].url.rsplit("/", 1)[-1]
    assert (generated / name).read_bytes() == png


async def test_generate_empty_prompt_is_valueerror(monkeypatch, studio_env) -> None:
    req = schemas.GenerateRequest(prompt="  ", model="m")
    with pytest.raises(ValueError):
        await service.generate(req, workspace_id="ws-1")


async def test_generate_missing_model_is_valueerror(monkeypatch, studio_env) -> None:
    req = schemas.GenerateRequest(prompt="x", model="")
    with pytest.raises(ValueError):
        await service.generate(req, workspace_id="ws-1")


async def test_generate_video_happy_path(monkeypatch, studio_env) -> None:
    """A fal video result → the mp4 (+ poster) is saved into the generated dir, a
    succeeded Generation is returned, and history records it under the workspace."""
    generated, history = studio_env
    mp4 = b"\x00\x00\x00\x18ftypmp42fake-video"
    poster = b"\x89PNG\r\n\x1a\nfake-poster"
    seen: dict = {}

    async def _fake_video(*, prompt, duration_sec, aspect_ratio, model, key=None, image_urls=None, resolution=None, generate_audio=None):
        seen.update(
            prompt=prompt,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            model=model,
        )
        return mp4, "video/mp4", poster, "image/png"

    monkeypatch.setattr(fal_video, "run_fal_video", _fake_video)

    req = schemas.GenerateRequest(
        prompt="a clip of waves",
        kind="video",
        model="fal-ai/kling-video/v1/standard/text-to-video",
        aspectRatio="16:9",
        durationSec=5,
        styleId="cinematic",
    )
    gen = await service.generate(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert gen.kind == "video"
    assert gen.params.durationSec == 5
    assert gen.params.aspectRatio == "16:9"
    assert seen["duration_sec"] == 5
    assert seen["aspect_ratio"] == "16:9"
    assert gen.assets and gen.assets[0].mime == "video/mp4"
    assert gen.assets[0].url.startswith("/api/v1/media/")
    assert gen.assets[0].posterUrl.startswith("/api/v1/media/")
    saved = list(generated.iterdir())
    assert any(p.suffix == ".mp4" for p in saved)
    assert any(p.suffix == ".png" for p in saved)
    # The persisted record is scoped to the workspace.
    assert service.list_generations("ws-1")[0].id == gen.id


async def test_generate_video_alias_resolves_endpoint(monkeypatch, studio_env) -> None:
    """A catalog id (fal_ai/fal-ai/kling/v2) is resolved onto a real fal endpoint
    by fal_video before dispatch."""
    seen: dict = {}

    async def _fake_video(*, prompt, duration_sec, aspect_ratio, model, key=None, image_urls=None, resolution=None, generate_audio=None):
        seen["model"] = model
        return b"mp4", "video/mp4", None, None

    monkeypatch.setattr(fal_video, "run_fal_video", _fake_video)

    req = schemas.GenerateRequest(
        prompt="a clip", kind="video", model="fal_ai/fal-ai/kling/v2", aspectRatio="16:9"
    )
    gen = await service.generate(req, workspace_id="ws-1")
    assert gen.status == "succeeded"
    assert seen["model"] == "fal_ai/fal-ai/kling/v2"  # echoed back to fal_video
    assert gen.model == "fal_ai/fal-ai/kling/v2"  # user sees what they asked for


async def test_generate_video_fal_failure_is_upstream_error(monkeypatch, studio_env) -> None:
    """A fal video upstream failure surfaces as StudioUpstreamError (→ 502)."""

    async def _boom(*, prompt, duration_sec, aspect_ratio, model, key=None, image_urls=None, resolution=None, generate_audio=None):
        raise fal_video.FalVideoError("fal video 'x' failed: bad key")

    monkeypatch.setattr(fal_video, "run_fal_video", _boom)

    req = schemas.GenerateRequest(prompt="a clip", kind="video", model="m", aspectRatio="16:9")
    with pytest.raises(service.StudioUpstreamError, match="bad key"):
        await service.generate(req, workspace_id="ws-1")
    assert service._load_history() == []


async def test_generate_video_image_to_video_passes_all_images(monkeypatch, studio_env) -> None:
    """A video request carrying ``inputImageUrls`` (the flow wiring Image nodes in)
    dispatches to the image-to-video path: every image is resolved to a data URL
    and forwarded to fal_video, and the Generation echoes the input image count."""
    generated, history = studio_env
    seen: dict = {}

    async def _fake_video(*, prompt, duration_sec, aspect_ratio, model, key=None, image_urls=None, resolution=None, generate_audio=None):
        seen.update(
            prompt=prompt,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            model=model,
            image_urls=image_urls,
        )
        return b"mp4", "video/mp4", None, None

    monkeypatch.setattr(fal_video, "run_fal_video", _fake_video)

    req = schemas.GenerateRequest(
        prompt="",
        kind="video",
        model="fal-ai/kling-video/v1/standard/image-to-video",
        aspectRatio="16:9",
        durationSec=5,
        inputImageUrls=["data:image/png;base64,AAAA", "data:image/png;base64,BBBB"],
    )
    gen = await service.generate(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert gen.params.inputImageCount == 2
    assert seen["image_urls"] == ["data:image/png;base64,AAAA", "data:image/png;base64,BBBB"]
    assert seen["duration_sec"] == 5
    assert seen["aspect_ratio"] == "16:9"
    # No typed prompt + images → the fal default motion prompt drives the call.
    assert seen["prompt"] == fal_video.DEFAULT_I2V_PROMPT


async def test_generate_video_image_to_video_forwards_typed_prompt(monkeypatch, studio_env) -> None:
    """A TYPED prompt is forwarded with the images — the user's motion direction
    drives the fal image-to-video call, with the active style suffix applied."""
    generated, history = studio_env
    seen: dict = {}

    async def _fake_video(*, prompt, duration_sec, aspect_ratio, model, key=None, image_urls=None, resolution=None, generate_audio=None):
        seen.update(prompt=prompt, image_urls=image_urls, duration_sec=duration_sec)
        return b"mp4", "video/mp4", None, None

    monkeypatch.setattr(fal_video, "run_fal_video", _fake_video)

    req = schemas.GenerateRequest(
        prompt="slow zoom in, then pan across",
        kind="video",
        model="fal-ai/kling-video/v1/standard/image-to-video",
        aspectRatio="16:9",
        durationSec=5,
        styleId="cinematic",
        inputImageUrls=["data:image/png;base64,AAAA"],
    )
    gen = await service.generate(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert seen["image_urls"] == ["data:image/png;base64,AAAA"]
    assert seen["duration_sec"] == 5
    # The typed prompt survives style suffixing and reaches fal_video verbatim-ish.
    assert seen["prompt"].startswith("slow zoom in, then pan across")
    assert "cinematic lighting" in seen["prompt"]
    assert gen.params.inputImageCount == 1


async def test_generate_video_without_images_requires_prompt(monkeypatch, studio_env) -> None:
    """Text-to-video still requires a prompt; only the image-to-video path may run
    prompt-less (the model animates the supplied frames)."""
    req = schemas.GenerateRequest(prompt="", kind="video", model="m", aspectRatio="16:9")
    with pytest.raises(ValueError, match="prompt is required for text-to-video"):
        await service.generate(req, workspace_id="ws-1")


async def test_generate_video_seedance_i2v_forwards_schema(monkeypatch, studio_env) -> None:
    """A Seedance i2v video request forwards the Seedance-specific extras
    (``resolution`` / ``generateAudio``) plus the resolved images to fal_video."""
    generated, history = studio_env
    seen: dict = {}

    async def _fake_video(*, prompt, duration_sec, aspect_ratio, model, key=None, image_urls=None, resolution=None, generate_audio=None):
        seen.update(
            model=model,
            image_urls=image_urls,
            resolution=resolution,
            generate_audio=generate_audio,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
        )
        return b"mp4", "video/mp4", None, None

    monkeypatch.setattr(fal_video, "run_fal_video", _fake_video)

    req = schemas.GenerateRequest(
        prompt="a character walks across the frame",
        kind="video",
        model="bytedance/seedance-2.5/image-to-video",
        aspectRatio="16:9",
        durationSec=30,
        inputImageUrls=["data:image/png;base64,AAAA"],
        resolution="720p",
        generateAudio=True,
    )
    gen = await service.generate(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert seen["model"] == "bytedance/seedance-2.5/image-to-video"
    assert seen["image_urls"] == ["data:image/png;base64,AAAA"]
    assert seen["resolution"] == "720p"
    assert seen["generate_audio"] is True
    assert seen["duration_sec"] == 30
    assert seen["aspect_ratio"] == "16:9"


# ── curated image + music (direct fal.ai dispatch, movie-maker) ──────────────


async def test_generate_curated_image_dispatches_direct_to_fal(monkeypatch, studio_env) -> None:
    """A curated image model id (in fal_image.IMAGE_MODEL_IDS) skips the LiteLLM
    proxy and dispatches directly against fal; each returned image is persisted."""
    generated, _ = studio_env
    png = b"\x89PNG\r\n\x1a\nfake-image"
    seen: dict = {}

    async def _fake_image(*, prompt, model, aspect_ratio, count, seed, key=None):
        seen.update(prompt=prompt, model=model, aspect_ratio=aspect_ratio, count=count)
        return [(png, "image/png")]

    monkeypatch.setattr(fal_image, "run_fal_image", _fake_image)

    req = schemas.GenerateRequest(
        prompt="a poster",
        kind="image",
        model="fal-ai/nano-banana-2",
        aspectRatio="16:9",
        count=1,
    )
    gen = await service.generate(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert gen.model == "fal-ai/nano-banana-2"
    assert seen["model"] == "fal-ai/nano-banana-2"
    assert seen["aspect_ratio"] == "16:9"
    assert gen.assets and gen.assets[0].mime == "image/png"
    assert gen.assets[0].url.startswith("/api/v1/media/")
    assert any(p.suffix == ".png" for p in generated.iterdir())


async def test_generate_image_edit_routes_on_references(monkeypatch, studio_env) -> None:
    """Reference images switch image generation to the curated edit endpoint."""
    png = b"\x89PNG\r\n\x1a\nfake-edit"
    seen: dict = {}

    async def _fake_edit(*, prompt, image_urls, model, aspect_ratio, count, seed, key=None):
        seen.update(prompt=prompt, model=model, image_urls=image_urls)
        return [(png, "image/png")]

    monkeypatch.setattr(fal_image, "run_fal_image_edit", _fake_edit)

    req = schemas.GenerateRequest(
        prompt="same character in a new scene",
        kind="image",
        model="openai/gpt-image-2",
        aspectRatio="1:1",
        count=1,
        referenceImageUrls=[_DATA_URL],
    )
    gen = await service.generate(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert seen["model"] == "openai/gpt-image-2"
    assert len(seen["image_urls"]) == 1
    assert seen["image_urls"][0] == _DATA_URL


async def test_generate_music_happy_path(monkeypatch, studio_env) -> None:
    """A fal music result → the audio is saved, a succeeded kind='audio'
    Generation is returned, and history records it."""
    generated, history = studio_env
    mp3 = b"ID3\x04fake-audio"
    seen: dict = {}

    async def _fake_music(*, prompt, model, lyrics, instrumental, duration_sec, steps, key=None):
        seen.update(prompt=prompt, model=model, instrumental=instrumental)
        return mp3, "audio/mpeg"

    monkeypatch.setattr(fal_music, "run_fal_music", _fake_music)

    req = schemas.MusicRequest(
        prompt="a tense thriller score",
        model="fal-ai/elevenlabs/music",
        instrumental=True,
        durationSec=60,
    )
    gen = await service.generate_music(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert gen.kind == "audio"
    assert seen["model"] == "fal-ai/elevenlabs/music"
    assert seen["instrumental"] is True
    assert gen.assets and gen.assets[0].mime == "audio/mpeg"
    assert gen.assets[0].url.startswith("/api/v1/media/")
    assert any(p.suffix == ".mp3" for p in generated.iterdir())
    assert service.list_generations("ws-1")[0].id == gen.id


async def test_generate_music_missing_prompt_is_valueerror(monkeypatch, studio_env) -> None:
    """A music request with no prompt fails fast with ValueError (→ 400)."""
    req = schemas.MusicRequest(prompt="")
    with pytest.raises(ValueError, match="prompt is required for music generation"):
        await service.generate_music(req, workspace_id="ws-1")


async def test_generate_proxy_failure_is_upstream_error(monkeypatch, proxy_env, studio_env) -> None:
    """A non-2xx proxy response surfaces as StudioUpstreamError (→ 502), and
    nothing is persisted."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "no quota"}})

    _install_transport(monkeypatch, handler)

    req = schemas.GenerateRequest(prompt="x", model="fal_ai/fal-ai/flux/schnell", aspectRatio="1:1")
    with pytest.raises(service.StudioUpstreamError):
        await service.generate(req, workspace_id="ws-1")
    assert service._load_history() == []


# ── edit (direct fal.ai dispatch) ────────────────────────────────────────────


async def test_edit_happy_path(monkeypatch, studio_env) -> None:
    """A fal edit result → a NEW succeeded Generation whose asset is saved into
    the generated dir, recorded in history, and excluded from the /media list."""
    generated, _ = studio_env
    png = b"\x89PNG\r\n\x1a\nedited"

    async def _run_fal_edit(**kwargs):
        assert kwargs["op"] == "upscale"
        assert kwargs["image_data_url"].startswith("data:image/png;base64,")
        assert kwargs["factor"] == 4
        return [(png, "image/png")]

    monkeypatch.setattr(fal_edit, "run_fal_edit", _run_fal_edit)

    req = schemas.EditRequest(op="upscale", sourceUrl=_DATA_URL, factor=4)
    gen = await service.edit(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert gen.kind == "image"
    assert gen.model == "fal-ai/esrgan"  # DEFAULT_EDIT_MODELS["upscale"]
    assert gen.prompt == "upscale"
    assert len(gen.assets) == 1
    url = gen.assets[0].url
    assert url.startswith("/api/v1/media/")
    name = url.rsplit("/", 1)[-1]
    assert (generated / name).read_bytes() == png

    # History: one record, scoped to ws-1, excluded from the /media list.
    records = service._load_history()
    assert len(records) == 1
    assert records[0]["_workspace"] == "ws-1"
    assert name in service.tracked_generation_filenames()


async def test_edit_op_routes_curated_model_through_fal_image(monkeypatch, studio_env) -> None:
    """``op='edit'`` with a curated image model id + per-model params dispatches
    through the model's own /edit variant (fal_image), forwarding ``num_images``
    and ``seed`` instead of the generic canvas op."""
    png = b"\x89PNG\r\n\x1a\nedited"
    seen: dict = {}

    async def _fake_fal_image_edit(**kwargs):
        seen.update(kwargs)
        return [(png, "image/png")]

    monkeypatch.setattr(fal_image, "run_fal_image_edit", _fake_fal_image_edit)

    req = schemas.EditRequest(
        op="edit",
        sourceUrl=_DATA_URL,
        prompt="turn the sky purple",
        model="fal-ai/nano-banana-2",
        params={"num_images": 3, "seed": "42"},
    )
    gen = await service.edit(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert seen["model"] == "fal-ai/nano-banana-2"
    assert seen["count"] == 3
    assert seen["seed"] == 42
    assert seen["image_urls"] == [_DATA_URL]
    assert gen.assets[0].url.startswith("/api/v1/media/")


async def test_edit_op_forwards_gpt_image_2_knobs(monkeypatch, studio_env) -> None:
    """gpt-image-2 edit forwards its own knobs (quality / size / background /
    output_format) onto the fal arguments."""
    png = b"\x89PNG\r\n\x1a\nedited"
    seen: dict = {}

    async def _fake_fal_image_edit(**kwargs):
        seen.update(kwargs)
        return [(png, "image/png")]

    monkeypatch.setattr(fal_image, "run_fal_image_edit", _fake_fal_image_edit)

    req = schemas.EditRequest(
        op="edit",
        sourceUrl=_DATA_URL,
        prompt="livery",
        model="openai/gpt-image-2",
        params={
            "quality": "high",
            "size": "1536x1024",
            "background": "transparent",
            "output_format": "png",
            "num_images": 2,
        },
    )
    gen = await service.edit(req, workspace_id="ws-1")
    assert gen.status == "succeeded"
    assert seen["quality"] == "high"
    assert seen["size"] == "1536x1024"
    assert seen["background"] == "transparent"
    assert seen["output_format"] == "png"
    assert seen["count"] == 2


async def test_edit_op_curated_params_blank_seed_is_none(monkeypatch, studio_env) -> None:
    """A blank ``seed`` from the composer's untouched text knob coerces to None
    (never ``int('')`` → 400/500)."""
    png = b"\x89PNG\r\n\x1a\nedited"
    seen: dict = {}

    async def _fake_fal_image_edit(**kwargs):
        seen.update(kwargs)
        return [(png, "image/png")]

    monkeypatch.setattr(fal_image, "run_fal_image_edit", _fake_fal_image_edit)

    req = schemas.EditRequest(
        op="edit",
        sourceUrl=_DATA_URL,
        prompt="recolor",
        model="bytedance/seedream/v5/pro/text-to-image",
        params={"seed": ""},
    )
    gen = await service.edit(req, workspace_id="ws-1")
    assert gen.status == "succeeded"
    assert seen["seed"] is None


async def test_edit_resolves_stored_media_source(monkeypatch, studio_env) -> None:
    """A ``/api/v1/media/<name>`` sourceUrl reads the stored bytes through the
    media adapter and hands fal a ``data:`` URL (the common edit-a-previous-
    generation case)."""
    generated, _ = studio_env
    src = b"\x89PNG\r\n\x1a\nsource"
    (generated / "src.png").write_bytes(src)
    seen: dict = {}

    async def _run_fal_edit(**kwargs):
        seen["image_data_url"] = kwargs["image_data_url"]
        return [(b"\x89PNG\r\n\x1a\nout", "image/png")]

    monkeypatch.setattr(fal_edit, "run_fal_edit", _run_fal_edit)

    req = schemas.EditRequest(op="remove-bg", sourceUrl="/api/v1/media/src.png")
    gen = await service.edit(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert seen["image_data_url"] == fal_edit.encode_bytes(src, "image/png")
    assert gen.model == "fal-ai/birefnet/v2"  # DEFAULT_EDIT_MODELS["remove-bg"]


async def test_edit_unknown_op_is_not_supported(monkeypatch, studio_env) -> None:
    req = schemas.EditRequest(op="warp", sourceUrl=_DATA_URL)
    with pytest.raises(service.StudioNotSupported):
        await service.edit(req, workspace_id="ws-1")


async def test_edit_missing_source_is_valueerror(monkeypatch, studio_env) -> None:
    req = schemas.EditRequest(op="upscale", sourceUrl="  ")
    with pytest.raises(ValueError, match="sourceUrl is required"):
        await service.edit(req, workspace_id="ws-1")


async def test_edit_fal_failure_is_upstream_error(monkeypatch, studio_env) -> None:
    """A fal upstream error surfaces as StudioUpstreamError (→ 502), and
    nothing is persisted."""

    async def _boom(**kwargs):
        raise fal_edit.FalEditError("fal edit 'fal-ai/esrgan' failed: timeout")

    monkeypatch.setattr(fal_edit, "run_fal_edit", _boom)

    req = schemas.EditRequest(op="upscale", sourceUrl=_DATA_URL)
    with pytest.raises(service.StudioUpstreamError):
        await service.edit(req, workspace_id="ws-1")
    assert service._load_history() == []


async def test_edit_missing_prompt_is_valueerror(monkeypatch, studio_env) -> None:
    """A prompt-driven op (edit) with no prompt → ValueError, so the router can
    400 without a doomed fal round-trip. The REAL run_fal_edit raises it in
    build_arguments before any fal call."""
    req = schemas.EditRequest(op="edit", sourceUrl=_DATA_URL)
    with pytest.raises(ValueError, match="prompt is required"):
        await service.edit(req, workspace_id="ws-1")
    assert service._load_history() == []


async def test_edit_no_output_is_upstream_error(monkeypatch, studio_env) -> None:
    """fal returns [] → StudioUpstreamError, nothing persisted."""

    async def _run_fal_edit(**kwargs):
        return []

    monkeypatch.setattr(fal_edit, "run_fal_edit", _run_fal_edit)

    req = schemas.EditRequest(op="upscale", sourceUrl=_DATA_URL)
    with pytest.raises(service.StudioUpstreamError, match="no output images"):
        await service.edit(req, workspace_id="ws-1")
    assert service._load_history() == []


# ── video elements (direct fal.ai Kling Elements dispatch) ───────────────────


async def test_generate_video_elements_happy_path(monkeypatch, studio_env) -> None:
    """A fal elements result → the mp4 (+ poster) is saved, a succeeded video
    Generation is returned, and every input (video + elements) is forwarded."""
    generated, history = studio_env
    seen: dict = {}

    async def _fake_elements(
        *, prompt, input_image_urls, video_url, duration_sec, aspect_ratio, model, key=None
    ):
        seen.update(
            prompt=prompt,
            input_image_urls=input_image_urls,
            video_url=video_url,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            model=model,
        )
        return b"mp4", "video/mp4", b"png-poster", "image/png"

    monkeypatch.setattr(fal_elements, "run_fal_elements", _fake_elements)

    req = schemas.VideoElementsRequest(
        prompt="add a cow",
        videoUrl=_DATA_URL,
        inputImageUrls=[_DATA_URL, _DATA_URL],
        aspectRatio="16:9",
        durationSec=5,
        sourceDurationSec=4.2,
    )
    gen = await service.generate_video_elements(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert gen.kind == "video"
    assert gen.params.inputImageCount == 2
    assert gen.assets[0].mime == "video/mp4"
    assert gen.assets[0].url.startswith("/api/v1/media/")
    assert gen.assets[0].posterUrl.startswith("/api/v1/media/")
    assert seen["input_image_urls"] == [_DATA_URL, _DATA_URL]
    assert seen["video_url"] == _DATA_URL
    assert seen["duration_sec"] == 5
    assert service.list_generations("ws-1")[0].id == gen.id


async def test_generate_video_elements_prompt_only(monkeypatch, studio_env) -> None:
    """A prompt-only request is valid (text-to-video through Elements)."""
    seen: dict = {}

    async def _fake_elements(
        *, prompt, input_image_urls, video_url, duration_sec, aspect_ratio, model, key=None
    ):
        seen["prompt"] = prompt
        return b"mp4", "video/mp4", None, None

    monkeypatch.setattr(fal_elements, "run_fal_elements", _fake_elements)
    req = schemas.VideoElementsRequest(prompt="a wave", aspectRatio="1:1")
    gen = await service.generate_video_elements(req, workspace_id="ws-1")
    assert gen.status == "succeeded"
    assert seen["prompt"] == "a wave"


async def test_generate_video_elements_rejects_too_many_images(monkeypatch, studio_env) -> None:
    req = schemas.VideoElementsRequest(
        prompt="x", inputImageUrls=[_DATA_URL] * (fal_elements.MAX_ELEMENT_IMAGES + 1)
    )
    with pytest.raises(ValueError, match="at most 20"):
        await service.generate_video_elements(req, workspace_id="ws-1")


async def test_generate_video_elements_rejects_overlong_source(monkeypatch, studio_env) -> None:
    req = schemas.VideoElementsRequest(prompt="x", videoUrl=_DATA_URL, sourceDurationSec=30.1)
    with pytest.raises(ValueError, match="30 seconds or less"):
        await service.generate_video_elements(req, workspace_id="ws-1")


async def test_generate_video_elements_requires_something(monkeypatch, studio_env) -> None:
    req = schemas.VideoElementsRequest(prompt="  ")
    with pytest.raises(ValueError, match="required"):
        await service.generate_video_elements(req, workspace_id="ws-1")


async def test_generate_video_elements_fal_failure_is_upstream_error(
    monkeypatch, studio_env
) -> None:
    async def _boom(**kwargs):
        raise fal_elements.FalElementsError("fal elements 'x' failed: bad key")

    monkeypatch.setattr(fal_elements, "run_fal_elements", _boom)
    req = schemas.VideoElementsRequest(prompt="a scene")
    with pytest.raises(service.StudioUpstreamError, match="bad key"):
        await service.generate_video_elements(req, workspace_id="ws-1")
    assert service._load_history() == []


# ── motion control (direct fal.ai Kling Motion Control dispatch) ─────────────


async def test_generate_video_motion_happy_path(monkeypatch, studio_env) -> None:
    """A fal motion-control result → the mp4 (+ poster) is saved, a succeeded
    video Generation is returned, and both inputs are forwarded."""
    generated, history = studio_env
    seen: dict = {}

    async def _fake_motion(*, image_url, video_url, character_orientation, model, key=None):
        seen.update(
            image_url=image_url,
            video_url=video_url,
            character_orientation=character_orientation,
            model=model,
        )
        return b"mp4", "video/mp4", b"png-poster", "image/png"

    monkeypatch.setattr(fal_motion, "run_fal_motion", _fake_motion)

    req = schemas.VideoMotionRequest(
        imageUrl=_DATA_URL,
        videoUrl=_DATA_URL,
        characterOrientation="video",
        aspectRatio="16:9",
    )
    gen = await service.generate_video_motion(req, workspace_id="ws-1")

    assert gen.status == "succeeded"
    assert gen.kind == "video"
    assert gen.assets[0].mime == "video/mp4"
    assert gen.assets[0].url.startswith("/api/v1/media/")
    assert gen.assets[0].posterUrl.startswith("/api/v1/media/")
    assert seen["image_url"] == _DATA_URL
    assert seen["video_url"] == _DATA_URL
    assert seen["character_orientation"] == "video"
    assert service.list_generations("ws-1")[0].id == gen.id


async def test_generate_video_motion_requires_image(monkeypatch, studio_env) -> None:
    req = schemas.VideoMotionRequest(imageUrl="  ", videoUrl=_DATA_URL)
    with pytest.raises(ValueError, match="character image"):
        await service.generate_video_motion(req, workspace_id="ws-1")


async def test_generate_video_motion_requires_video(monkeypatch, studio_env) -> None:
    req = schemas.VideoMotionRequest(imageUrl=_DATA_URL, videoUrl="  ")
    with pytest.raises(ValueError, match="motion reference video"):
        await service.generate_video_motion(req, workspace_id="ws-1")


async def test_generate_video_motion_fal_failure_is_upstream_error(monkeypatch, studio_env) -> None:
    async def _boom(**kwargs):
        raise fal_motion.FalMotionError("fal motion-control 'x' failed: bad key")

    monkeypatch.setattr(fal_motion, "run_fal_motion", _boom)
    req = schemas.VideoMotionRequest(imageUrl=_DATA_URL, videoUrl=_DATA_URL)
    with pytest.raises(service.StudioUpstreamError, match="bad key"):
        await service.generate_video_motion(req, workspace_id="ws-1")
    assert service._load_history() == []


async def test_generate_video_motion_validation_error_is_value_error(
    monkeypatch, studio_env
) -> None:
    """fal rejecting the inputs (a 4xx) surfaces as ValueError (→ 400), not 502."""

    async def _invalid(**kwargs):
        raise fal_motion.FalMotionValidationError(
            "fal motion-control rejected the request: Image dimensions are too small"
        )

    monkeypatch.setattr(fal_motion, "run_fal_motion", _invalid)
    req = schemas.VideoMotionRequest(imageUrl=_DATA_URL, videoUrl=_DATA_URL)
    with pytest.raises(ValueError, match="dimensions are too small"):
        await service.generate_video_motion(req, workspace_id="ws-1")
    assert service._load_history() == []


async def test_generate_video_motion_passes_public_video_url_through(
    monkeypatch, studio_env
) -> None:
    """A hosted http(s) video URL is forwarded to fal AS-IS (not re-encoded to a
    data URL), while the character image is still resolved to a data URL."""
    seen: dict = {}
    public_video = "https://cdn.higgsfield.ai/kling_motion_control_preset/x.mp4"

    async def _fake_motion(*, image_url, video_url, character_orientation, model, key=None):
        seen.update(image_url=image_url, video_url=video_url)
        return b"mp4", "video/mp4", None, None

    monkeypatch.setattr(fal_motion, "run_fal_motion", _fake_motion)
    req = schemas.VideoMotionRequest(imageUrl=_DATA_URL, videoUrl=public_video)
    await service.generate_video_motion(req, workspace_id="ws-1")

    assert seen["image_url"] == _DATA_URL
    assert seen["video_url"] == public_video


def test_fit_character_image_downscales_oversized() -> None:
    """An image over fal's 3850px cap is downscaled (aspect preserved) to a JPEG
    whose long side equals the cap."""
    from io import BytesIO

    from PIL import Image

    im = Image.new("RGB", (5000, 2000), (10, 20, 30))
    buf = BytesIO()
    im.save(buf, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    out_url, out_mime = service._fit_character_image(data_url, "image/jpeg")

    assert out_mime == "image/jpeg"
    out = Image.open(BytesIO(base64.b64decode(out_url.split(",", 1)[1])))
    width, height = out.size
    assert max(width, height) == service.FAL_IMAGE_MAX_DIM
    assert min(width, height) == round(2000 * service.FAL_IMAGE_MAX_DIM / 5000)


def test_fit_character_image_passes_through_within_limit() -> None:
    """An image already within the cap is returned unchanged."""
    from io import BytesIO

    from PIL import Image

    im = Image.new("RGB", (512, 512), (200, 100, 50))
    buf = BytesIO()
    im.save(buf, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    assert service._fit_character_image(data_url, "image/jpeg") == (data_url, "image/jpeg")


# ── styles + suggest ─────────────────────────────────────────────────────────


def test_list_styles_matches_mock() -> None:
    styles = service.list_styles()
    ids = [s.id for s in styles]
    # The legacy quick styles stay first (the gallery/composer depend on the
    # "none" head + the short ids); the curated rich registry is appended.
    assert ids[:7] == ["none", "cinematic", "photoreal", "watercolor", "anime", "threed", "neon"]
    cinematic = next(s for s in styles if s.id == "cinematic")
    assert "cinematic" in cinematic.promptSuffix


def test_list_styles_includes_curated_registry() -> None:
    """Curated styles carry category/tags + full look/motion/references config so
    the movie-maker can render detail cards (the Sci-Fi Futuristic example)."""
    styles = service.list_styles()
    ids = {s.id for s in styles}
    assert "sci-fi-futuristic" in ids
    assert len(ids) > 7  # quick styles + the curated registry

    sci_fi = next(s for s in styles if s.id == "sci-fi-futuristic")
    assert sci_fi.category == "film"
    assert "scifi" in sci_fi.tags
    assert sci_fi.config is not None
    assert sci_fi.config.look.mood == "Futuristic and technological"
    assert sci_fi.config.look.colorPalette == ["#00FFFF", "#0000FF", "#C0C0C0", "#800080", "#00FF00"]
    assert "holographic elements" in sci_fi.config.look.artStyle
    assert sci_fi.config.motion.camera
    assert len(sci_fi.config.references) == 3
    assert "Art style" in sci_fi.promptSuffix  # "Use this style" appends treatment


def test_suggest_prompt_heuristic() -> None:
    image = service.suggest_prompt("a lighthouse")
    assert image.kind == "image"
    assert "highly detailed" in image.prompt
    video = service.suggest_prompt("a clip of waves moving")
    assert video.kind == "video"


# ── Flow projects (JSONL persistence, workspace-scoped) ─────────────────────


def _node(node_id: str = "text_1") -> schemas.FlowNode:
    return schemas.FlowNode(
        id=node_id,
        type="text",
        position={"x": 40.0, "y": 120.0},
        data={"status": "idle", "text": "hello"},
    )


def _edge() -> schemas.FlowEdge:
    return schemas.FlowEdge(id="e-1", source="text_1", target="image_1")


def test_save_flow_project_upserts(studio_env) -> None:
    """PUT semantics: an unknown id creates the project, a known id updates it
    in place (nodes/edges replaced, createdAt preserved, updatedAt bumped)."""
    saved = service.save_flow_project("proj_1", "ws-1", name="Posters", nodes=[_node()], edges=[])
    assert saved.id == "proj_1"
    assert saved.name == "Posters"
    assert saved.nodes[0].data["text"] == "hello"
    assert saved.createdAt > 0

    # Update — nodes replaced, name kept when omitted, createdAt stable.
    moved = service.save_flow_project(
        "proj_1", "ws-1", name=None, nodes=[_node("text_2"), _node("image_2")], edges=[_edge()]
    )
    assert moved.id == "proj_1"
    assert moved.name == "Posters"  # preserved when name omitted
    assert moved.createdAt == saved.createdAt
    assert moved.updatedAt >= saved.updatedAt
    assert [n.id for n in moved.nodes] == ["text_2", "image_2"]
    assert moved.edges[0].source == "text_1"
    assert service.get_flow_project("proj_1", "ws-1").nodes[0].id == "text_2"


def test_save_flow_project_blank_name_falls_back(studio_env) -> None:
    """A blank name defaults to 'Flow' rather than persisting an empty title."""
    saved = service.save_flow_project("proj_2", "ws-1", name="   ", nodes=[], edges=[])
    assert saved.name == "Flow"


def test_list_flow_projects_workspace_scoped(studio_env, monkeypatch) -> None:
    """list_flow_projects returns only the caller's workspace, newest-first."""
    # Deterministic timestamps so ordering is asserted, not racy (saves that land
    # in the same millisecond would otherwise stay in insertion order).
    ticks = iter([100, 200, 300])
    monkeypatch.setattr(service, "time_now_ms", lambda: next(ticks))
    service.save_flow_project("a", "ws-1", name="A", nodes=[_node()], edges=[])
    service.save_flow_project("b", "ws-1", name="B", nodes=[_node()], edges=[])
    service.save_flow_project("c", "ws-2", name="C", nodes=[_node()], edges=[])

    mine = service.list_flow_projects("ws-1")
    assert {p.id for p in mine} == {"a", "b"}
    # Most-recently-updated first (b was saved last).
    assert mine[0].id == "b"

    other = service.list_flow_projects("ws-2")
    assert [p.id for p in other] == ["c"]


def test_get_flow_project_scoped(studio_env) -> None:
    """get_flow_project returns the record only when it belongs to the workspace."""
    service.save_flow_project("proj_1", "ws-1", name="Posters", nodes=[_node()], edges=[])
    assert service.get_flow_project("proj_1", "ws-1") is not None
    assert service.get_flow_project("proj_1", "ws-2") is None
    assert service.get_flow_project("nope", "ws-1") is None


def test_delete_flow_project(studio_env) -> None:
    """Delete removes the record and reports False for an unknown id."""
    service.save_flow_project("proj_1", "ws-1", name="Posters", nodes=[_node()], edges=[])
    assert service.delete_flow_project("proj_1", "ws-1") is True
    assert service.get_flow_project("proj_1", "ws-1") is None
    assert service.delete_flow_project("proj_1", "ws-1") is False
