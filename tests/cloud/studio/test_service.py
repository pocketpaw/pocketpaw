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
from pocketpaw_ee.cloud.studio import fal_edit, fal_video, schemas

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
    assert ids == [
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
    assert len(video) == 1
    assert video[0].id == fal_video.DEFAULT_VIDEO_MODEL
    assert video[0].provider == "fal"
    assert video[0].durationsSec == [2, 5, 10]
    assert video[0].default is True
    assert video[0].aspectRatios == ["16:9", "9:16", "1:1"]


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
    assert len(videos) == 1
    assert videos[0].id == "fal_ai/fal-ai/kling/v2"
    assert videos[0].durationsSec == [2, 5, 10]


async def test_list_models_upstream_failure_propagates(monkeypatch) -> None:
    """A catalog outage propagates CatalogUpstreamError so the router can 502."""

    async def _boom(**kw):
        raise CatalogUpstreamError("proxy down")

    monkeypatch.setattr(catalog_service, "list_models", _boom)
    with pytest.raises(CatalogUpstreamError):
        await service.list_models()


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

    async def _fake_video(*, prompt, duration_sec, aspect_ratio, model, key=None):
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

    async def _fake_video(*, prompt, duration_sec, aspect_ratio, model, key=None):
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

    async def _boom(*, prompt, duration_sec, aspect_ratio, model, key=None):
        raise fal_video.FalVideoError("fal video 'x' failed: bad key")

    monkeypatch.setattr(fal_video, "run_fal_video", _boom)

    req = schemas.GenerateRequest(prompt="a clip", kind="video", model="m", aspectRatio="16:9")
    with pytest.raises(service.StudioUpstreamError, match="bad key"):
        await service.generate(req, workspace_id="ws-1")
    assert service._load_history() == []


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


# ── styles + suggest ─────────────────────────────────────────────────────────


def test_list_styles_matches_mock() -> None:
    styles = service.list_styles()
    ids = [s.id for s in styles]
    assert ids == ["none", "cinematic", "photoreal", "watercolor", "anime", "threed", "neon"]
    cinematic = next(s for s in styles if s.id == "cinematic")
    assert "cinematic" in cinematic.promptSuffix


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
