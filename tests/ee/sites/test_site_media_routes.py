# tests/ee/sites/test_site_media_routes.py — the builder UI's Studio bridge.
#
# Pins the HTTP contract the Sites builder codes against:
#   POST /sites/by-pocket/{id}/assets/generate     Studio image/video → public rail
#   POST /sites/by-pocket/{id}/assets/from-studio  an existing generation → public rail
#
# The router is real, the Studio history is real (mongomock), and the public rail
# is a real ``PublicAssetStore`` over an in-memory adapter. Only the provider
# calls (LiteLLM proxy, fal) and the private media adapter are doubled.
#
# What must hold, and why each test exists:
#   * The sink is the PUBLIC rail: a private ``/api/v1/media`` URL 404s on a site.
#   * Ownership is checked before spend: a foreign pocket never reaches the proxy.
#   * image_url and from-studio read only media the workspace owns, from storage.
#     A client-chosen URL is never fetched (SSRF).
#   * A foreign generation is a 404, indistinguishable from a missing one.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

WORKSPACE = "ws_owner"
OTHER_WS = "ws_other"
POCKET = "pk1"
USER = "user-test-1"
BASE = "https://cdn.example.com/public"

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
_PNG_2 = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64
_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32

pytestmark = pytest.mark.asyncio


# ── Doubles ──────────────────────────────────────────────────────────────


class _MemAdapter:
    """Just enough StorageAdapter for PublicAssetStore and the media reader."""

    def __init__(self, base: str | None = BASE) -> None:
        self.objects: dict[str, bytes] = {}
        self._base = base

    async def put(self, key, stream, mime):  # noqa: ANN001
        self.objects[key] = b"".join([c async for c in stream])

    async def open(self, key: str):
        from pocketpaw.uploads.errors import NotFound

        if key not in self.objects:
            raise NotFound(key)
        yield self.objects[key]

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def browse(self, prefix: str) -> list:
        return []

    def public_url(self, key: str) -> str | None:
        return f"{self._base}/{key.lstrip('/')}" if self._base else None


class _FakeMembership:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.role = "member"


class _FakeUser:
    def __init__(self, workspace_id: str) -> None:
        self.id = USER
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace_id)]


def _build_app(workspace_id: str = WORKSPACE) -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    user = _FakeUser(workspace_id)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(sites_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=USER,
            workspace_id=workspace_id,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://test")


@pytest.fixture
def rail(monkeypatch: pytest.MonkeyPatch) -> _MemAdapter:
    """The public rail, over memory, and the pocket owned by WORKSPACE."""
    from pocketpaw_ee.sites.public_assets import PublicAssetStore

    adapter = _MemAdapter()
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: PublicAssetStore(adapter)
    )

    async def _owner(pocket_id: str) -> str | None:
        return {POCKET: WORKSPACE, "pk-foreign": OTHER_WS}.get(pocket_id)

    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.service.get_pocket_workspace", _owner)
    return adapter


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch) -> _MemAdapter:
    """The PRIVATE media adapter Studio writes ``/api/v1/media/<name>`` to."""
    from pocketpaw_ee.cloud.media import storage as media_storage

    adapter = _MemAdapter(base=None)
    monkeypatch.setattr(media_storage, "_ADAPTER", adapter)
    return adapter


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Stub the LiteLLM image call — no network, no spend. Returns the calls."""
    calls: list[dict] = []
    images = iter([_PNG, _PNG_2, _PNG, _PNG_2])

    async def _gen(**kwargs: Any):
        calls.append(kwargs)
        return (next(images), None)

    async def _key(_ws: str) -> str:
        return "sk-tenant"

    monkeypatch.setattr("pocketpaw_ee.cloud.studio.service._proxy_generate_image", _gen)
    monkeypatch.setattr("pocketpaw_ee.cloud.studio.service._resolve_auth_key", _key)
    return calls


@pytest.fixture
def fal(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the fal video render. ``seen`` records the kwargs it was called with."""
    seen: dict = {}

    async def _run(**kwargs: Any):
        seen.update(kwargs)
        if seen.get("_raise"):
            raise RuntimeError("fal: upstream quota exceeded")
        return (_MP4, "video/mp4", _JPG, "image/jpeg")

    monkeypatch.setattr("pocketpaw_ee.cloud.studio.fal_video.run_fal_video", _run)
    return seen


async def _seed_generation(
    workspace_id: str, gen_id: str, *, kind: str, assets: list[dict], status: str = "succeeded"
) -> None:
    from pocketpaw_ee.cloud.studio import schemas
    from pocketpaw_ee.cloud.studio import service as studio_service

    await studio_service.record_generation(
        workspace_id,
        schemas.Generation(
            id=gen_id,
            prompt="Golden hour over a quiet harbour",
            status=status,
            kind=kind,
            model="m",
            params=schemas.GenerationParams(kind=kind, model="m", aspectRatio="16:9", count=1),
            assets=[schemas.GeneratedAsset(**a) for a in assets],
            createdAt=1700000000000,
        ),
    )


# ── /assets/generate: images ────────────────────────────────────────────


async def test_generate_images_lands_on_the_public_rail_and_in_history(
    beanie_test_db, rail, proxy
) -> None:
    from pocketpaw_ee.cloud.studio import service as studio_service

    async with _client() as c:
        resp = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/generate",
            json={"kind": "image", "prompt": "A calm hero", "count": 2, "aspect_ratio": "1:1"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["assets"]) == 2
    assert body["poster"] is None and body["errors"] == []
    for asset in body["assets"]:
        assert set(asset) == {"key", "url", "mime", "size", "filename"}
        assert asset["key"].startswith(f"sites-assets/{WORKSPACE}/{POCKET}/")
        assert asset["url"].startswith(BASE) and "/api/v1/media/" not in asset["url"]
        assert asset["mime"] == "image/png"
        assert asset["key"] in rail.objects
    assert len(proxy) == 2 and proxy[0]["size"] == "1024x1024"

    history = await studio_service.list_generations(WORKSPACE, source="sites", pocket_id=POCKET)
    assert [g.id for g in history] == [body["generation_id"]]
    assert [a.url for a in history[0].assets] == [a["url"] for a in body["assets"]]


async def test_a_pocket_of_another_workspace_is_404_and_spends_nothing(
    beanie_test_db, rail, proxy
) -> None:
    async with _client() as c:
        foreign = await c.post(
            "/api/v1/sites/by-pocket/pk-foreign/assets/generate",
            json={"kind": "image", "prompt": "x"},
        )
        unknown = await c.post(
            "/api/v1/sites/by-pocket/pk-nope/assets/generate",
            json={"kind": "image", "prompt": "x"},
        )

    assert foreign.status_code == 404
    assert unknown.status_code == 404
    assert proxy == [], "the proxy must not be called for a pocket the caller does not own"
    assert rail.objects == {}


async def test_no_public_store_is_503(beanie_test_db, monkeypatch, proxy) -> None:
    monkeypatch.setattr("pocketpaw_ee.sites.public_assets.public_asset_store", lambda: None)

    async with _client() as c:
        resp = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/generate",
            json={"kind": "image", "prompt": "x"},
        )

    assert resp.status_code == 503
    assert "S3_PUBLIC_BUCKET" in resp.json()["detail"]
    assert proxy == []


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"kind": "audio", "prompt": "x"}, 422),
        ({"kind": "image", "prompt": ""}, 422),
        ({"kind": "image", "prompt": "   "}, 422),
        ({"kind": "image", "prompt": "x" * 2001}, 422),
        ({"kind": "image", "prompt": "x", "count": 5}, 422),
        ({"kind": "image", "prompt": "x", "count": 0}, 422),
        ({"kind": "video", "prompt": "x", "duration": 30}, 422),
        ({"kind": "image", "prompt": "x", "aspect_ratio": "21:9"}, 400),
        ({"kind": "image", "prompt": "x", "duration": 5}, 400),
        ({"kind": "video", "prompt": "x", "count": 2}, 400),
        ({"kind": "video", "prompt": "x", "aspect_ratio": "4:3"}, 400),
    ],
)
async def test_bad_input_is_refused_before_any_spend(
    beanie_test_db, rail, proxy, fal, payload, status
) -> None:
    async with _client() as c:
        resp = await c.post(f"/api/v1/sites/by-pocket/{POCKET}/assets/generate", json=payload)

    assert resp.status_code == status, resp.text
    assert proxy == [] and fal == {}


async def test_a_provider_failure_is_502_with_the_reason(beanie_test_db, rail, monkeypatch) -> None:
    async def _gen(**_kw: Any):
        return (None, "upstream quota exceeded")

    async def _key(_ws: str) -> str:
        return "sk"

    monkeypatch.setattr("pocketpaw_ee.cloud.studio.service._proxy_generate_image", _gen)
    monkeypatch.setattr("pocketpaw_ee.cloud.studio.service._resolve_auth_key", _key)

    async with _client() as c:
        resp = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/generate",
            json={"kind": "image", "prompt": "x"},
        )

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "quota" in detail and "Try again" in detail
    assert rail.objects == {}


# ── /assets/generate: video ─────────────────────────────────────────────


async def test_generate_video_from_a_site_asset_returns_clip_and_poster(
    beanie_test_db, rail, fal
) -> None:
    """image_url is one of this site's own assets: read back from the rail (not
    fetched) and sent to fal as a data: URL on the image-to-video model."""
    from pocketpaw_ee.cloud.studio import fal_video
    from pocketpaw_ee.sites.public_assets import PublicAssetStore

    source = await PublicAssetStore(rail).put(
        _PNG, filename="owner-photo.png", workspace_id=WORKSPACE, pocket_id=POCKET
    )

    async with _client() as c:
        resp = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/generate",
            json={
                "kind": "video",
                "prompt": "slow dolly in",
                "image_url": source.url,
                "duration": 10,
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [a["mime"] for a in body["assets"]] == ["video/mp4"]
    assert body["assets"][0]["url"].startswith(BASE)
    assert body["poster"]["mime"] == "image/jpeg"
    assert body["generation_id"].startswith("site-vid-")
    assert fal["model"] == fal_video.DEFAULT_IMAGE_TO_VIDEO_MODEL
    assert fal["duration_sec"] == 10
    assert fal["image_urls"][0].startswith("data:image/png;base64,")


async def test_video_image_url_outside_the_site_is_refused_and_never_fetched(
    beanie_test_db, rail, fal, monkeypatch
) -> None:
    """SSRF guard: an arbitrary URL, another pocket's asset, or another tenant's
    asset are all 400 before fal (or any HTTP fetch) runs. The foreign assets
    really exist on the rail, so the refusal is the prefix check, not a miss."""
    from pocketpaw_ee.sites.public_assets import PublicAssetStore

    async def _no_fetch(*_a: Any, **_kw: Any):
        raise AssertionError("the route must never fetch a client-supplied URL")

    monkeypatch.setattr("httpx.AsyncClient.get", _no_fetch)
    store = PublicAssetStore(rail)
    other_pocket = await store.put(
        _PNG, filename="x.png", workspace_id=WORKSPACE, pocket_id="other-pocket"
    )
    other_tenant = await store.put(_PNG, filename="x.png", workspace_id=OTHER_WS, pocket_id=POCKET)
    refused = [
        "http://169.254.169.254/latest/meta-data",
        "https://evil.example/x.png",
        other_pocket.url,
        other_tenant.url,
        f"{BASE}/sites-assets/{WORKSPACE}/{POCKET}/../../x.png",
    ]
    async with _client() as c:
        for url in refused:
            resp = await c.post(
                f"/api/v1/sites/by-pocket/{POCKET}/assets/generate",
                json={"kind": "video", "prompt": "x", "image_url": url},
            )
            assert resp.status_code == 400, url

    assert fal == {}


async def test_video_image_url_may_be_a_workspace_media_file(
    beanie_test_db, rail, fal, media
) -> None:
    from pocketpaw_ee.cloud.media import storage as media_storage

    media.objects[media_storage.media_key("1700000000000-abc.png")] = _PNG
    foreign_capture = f"{media_storage.capture_name_prefix(OTHER_WS)}1-x.png"
    media.objects[media_storage.media_key(foreign_capture)] = _PNG

    async with _client() as c:
        ok = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/generate",
            json={
                "kind": "video",
                "prompt": "x",
                "image_url": "/api/v1/media/1700000000000-abc.png",
            },
        )
        assert ok.status_code == 200, ok.text
        assert fal["image_urls"][0].startswith("data:image/png;base64,")

        fal.clear()
        bad = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/generate",
            json={"kind": "video", "prompt": "x", "image_url": f"/api/v1/media/{foreign_capture}"},
        )
    assert bad.status_code == 400
    assert fal == {}


async def test_a_failed_render_is_502(beanie_test_db, rail, fal) -> None:
    fal["_raise"] = True
    async with _client() as c:
        resp = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/generate",
            json={"kind": "video", "prompt": "x"},
        )
    assert resp.status_code == 502
    assert "quota" in resp.json()["detail"]


# ── /assets/from-studio ─────────────────────────────────────────────────


async def test_copy_an_image_generation_is_public_and_idempotent(
    beanie_test_db, rail, media
) -> None:
    from pocketpaw_ee.cloud.media import storage as media_storage

    media.objects[media_storage.media_key("1700-a.png")] = _PNG
    media.objects[media_storage.media_key("1700-b.png")] = _PNG_2
    await _seed_generation(
        WORKSPACE,
        "gen_img",
        kind="image",
        assets=[
            {"id": "a", "url": "/api/v1/media/1700-a.png", "mime": "image/png"},
            {"id": "b", "url": "/api/v1/media/1700-b.png", "mime": "image/png"},
        ],
    )

    async with _client() as c:
        first = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/from-studio",
            json={"generation_id": "gen_img"},
        )
        second = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/from-studio",
            json={"generation_id": "gen_img"},
        )

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["generation_id"] == "gen_img"
    assert len(body["assets"]) == 2 and body["poster"] is None
    for asset in body["assets"]:
        assert asset["url"].startswith(BASE)
        assert asset["key"].startswith(f"sites-assets/{WORKSPACE}/{POCKET}/")
    assert rail.objects[body["assets"][0]["key"]] == _PNG
    # Content-addressed: the repeat lands on the same keys, no duplicates.
    assert [a["key"] for a in second.json()["assets"]] == [a["key"] for a in body["assets"]]
    assert len(rail.objects) == 2


async def test_copy_a_video_generation_brings_its_poster(beanie_test_db, rail, media) -> None:
    from pocketpaw_ee.cloud.media import storage as media_storage

    media.objects[media_storage.media_key("1700-v.mp4")] = _MP4
    media.objects[media_storage.media_key("1700-v-poster.jpg")] = _JPG
    await _seed_generation(
        WORKSPACE,
        "gen_vid",
        kind="video",
        assets=[
            {
                "id": "v",
                "url": "/api/v1/media/1700-v.mp4",
                "mime": "video/mp4",
                "posterUrl": "/api/v1/media/1700-v-poster.jpg",
            }
        ],
    )

    async with _client() as c:
        resp = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/from-studio",
            json={"generation_id": "gen_vid"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [a["mime"] for a in body["assets"]] == ["video/mp4"]
    assert body["poster"]["mime"] == "image/jpeg"
    assert body["poster"]["url"].startswith(BASE)


async def test_another_workspaces_generation_is_404(beanie_test_db, rail, media) -> None:
    from pocketpaw_ee.cloud.media import storage as media_storage

    media.objects[media_storage.media_key("1700-x.png")] = _PNG
    await _seed_generation(
        OTHER_WS,
        "gen_theirs",
        kind="image",
        assets=[{"id": "x", "url": "/api/v1/media/1700-x.png", "mime": "image/png"}],
    )

    async with _client() as c:
        theirs = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/from-studio",
            json={"generation_id": "gen_theirs"},
        )
        missing = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/from-studio",
            json={"generation_id": "gen_nope"},
        )

    assert theirs.status_code == 404
    assert missing.status_code == 404
    assert theirs.json()["detail"] == missing.json()["detail"]
    assert rail.objects == {}


async def test_a_generation_pointing_at_an_external_url_is_never_fetched(
    beanie_test_db, rail, media, monkeypatch
) -> None:
    async def _no_fetch(*_a: Any, **_kw: Any):
        raise AssertionError("from-studio must never fetch a URL")

    monkeypatch.setattr("httpx.AsyncClient.get", _no_fetch)
    await _seed_generation(
        WORKSPACE,
        "gen_ext",
        kind="image",
        assets=[{"id": "x", "url": "http://169.254.169.254/x.png", "mime": "image/png"}],
    )

    async with _client() as c:
        resp = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/from-studio",
            json={"generation_id": "gen_ext"},
        )

    assert resp.status_code == 404
    assert rail.objects == {}


async def test_an_unfinished_or_audio_generation_is_refused(beanie_test_db, rail, media) -> None:
    await _seed_generation(WORKSPACE, "gen_run", kind="video", assets=[], status="running")
    await _seed_generation(
        WORKSPACE,
        "gen_aud",
        kind="audio",
        assets=[{"id": "m", "url": "/api/v1/media/1700-m.mp3", "mime": "audio/mpeg"}],
    )

    async with _client() as c:
        running = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/from-studio",
            json={"generation_id": "gen_run"},
        )
        audio = await c.post(
            f"/api/v1/sites/by-pocket/{POCKET}/assets/from-studio",
            json={"generation_id": "gen_aud"},
        )

    assert running.status_code == 409
    assert audio.status_code == 400


async def test_from_studio_into_a_foreign_pocket_is_404(beanie_test_db, rail, media) -> None:
    async with _client() as c:
        resp = await c.post(
            "/api/v1/sites/by-pocket/pk-foreign/assets/from-studio",
            json={"generation_id": "gen_any"},
        )
    assert resp.status_code == 404
