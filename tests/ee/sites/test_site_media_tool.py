# tests/ee/sites/test_site_media_tool.py — the site-authoring image generator.
#
# Created 2026-09-09 (feat/site-media-tools, SM-1). Sibling of
# test_list_site_assets_tool.py and pinned on the same three axes, because this
# tool has the same two ways to be silently wrong:
#
#   * REGISTRATION IS NOT REACHABILITY. The /sites allow-list is a hard
#     whitelist; an id absent from ``sites_allow`` is filtered out and the tool
#     never fires, with no error anywhere.
#   * THE SINK IS THE WHOLE POINT. studio's media server writes the PRIVATE
#     adapter and returns a backend-relative ``/api/v1/media/<name>``, which
#     resolves against the published site's own domain and 404s. If this tool
#     ever returns one of those, every generated image is a broken box on the
#     live page — and it would look fine in every test that only checks ``ok``.
#
# MUTATION: point the handler at ``media_storage.save_generated`` instead of the
# public store and the URL-shape test must fail.

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from pocketpaw_ee.agent.mcp_servers import site_media
from pocketpaw_ee.agent.mcp_servers.site_media import (
    GENERATE_SITE_IMAGE_TOOL_ID,
    SITE_MEDIA_TOOL_IDS,
    _generate_site_image_handler,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _text(resp: dict) -> str:
    return resp["content"][0]["text"]


def _body(resp: dict) -> dict:
    return json.loads(_text(resp))


@dataclass
class _FakeAsset:
    url: str
    mime: str = "image/png"
    size: int = 1234
    filename: str = "hero-1.png"
    key: str = "sites-assets/ws/pk/abc-hero-1.png"


class FakeStore:
    """Records what the handler asked it to store."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str]] = []

    async def put(self, data, *, filename, workspace_id, pocket_id):  # noqa: ANN001
        self.seen.append((workspace_id, pocket_id, filename))
        return _FakeAsset(
            url=f"https://cdn.example.com/sites-assets/{workspace_id}/{pocket_id}/x.png"
        )


def _patch_generation(monkeypatch, *, image=_PNG, err=None):
    """Stub studio's proxy call — no network, no spend."""

    async def _gen(**kwargs):
        return (image, err)

    async def _key(_ws):
        return "sk-tenant"

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.studio.service._proxy_generate_image", _gen, raising=False
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.studio.service._resolve_auth_key", _key, raising=False)


def _patch_history(monkeypatch) -> list[dict]:
    """Capture history writes without needing Mongo."""
    recorded: list[dict] = []

    async def _record(workspace_id, generation, *, source="studio", pocket_id=None):  # noqa: ANN001
        recorded.append(
            {"workspace": workspace_id, "source": source, "pocket": pocket_id, "gen": generation}
        )

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.studio.service.record_generation", _record, raising=False
    )
    return recorded


# ── Registration and reachability are two different facts ───────────────


def test_the_tool_id_rides_the_site_media_tool_ids_tuple() -> None:
    assert GENERATE_SITE_IMAGE_TOOL_ID in SITE_MEDIA_TOOL_IDS


def test_the_tool_is_actually_reachable_on_the_sites_surface() -> None:
    """An id absent from the hard whitelist is filtered out and never fires."""
    from pocketpaw_ee.cloud.surface.surface_registry import _load_mcp_tool_ids

    ids = _load_mcp_tool_ids()
    assert ids.loaded, "the MCP allow-lists failed to load; scoping would be disabled"
    assert GENERATE_SITE_IMAGE_TOOL_ID in (ids.sites_allow or frozenset())


def test_the_tool_is_not_reachable_on_an_unrelated_surface() -> None:
    """Generation costs money; it belongs to /sites, not to every surface."""
    from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

    allow = resolve_profile(SurfaceKind.CHAT, SurfaceMeta()).allow_mcp_tool_ids
    if allow is not None:
        assert GENERATE_SITE_IMAGE_TOOL_ID not in allow


# ── Tenancy: identity comes from the stream, never from the args ────────


@pytest.mark.asyncio
async def test_a_workspace_id_in_the_args_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE injection guard: args are model-controlled, ContextVars are not."""
    store = FakeStore()
    monkeypatch.setattr(site_media, "_identity", lambda: ("real-ws", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )
    _patch_generation(monkeypatch)
    _patch_history(monkeypatch)

    await _generate_site_image_handler(
        {"pocket_id": "pk1", "prompt": "a hero", "workspace_id": "victim-ws"}
    )

    assert [s[0] for s in store.seen] == ["real-ws"]


@pytest.mark.asyncio
async def test_no_active_workspace_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_media, "_identity", lambda: (None, None))
    resp = await _generate_site_image_handler({"pocket_id": "pk1", "prompt": "a hero"})
    assert resp.get("is_error") is True
    assert "workspace" in _text(resp).lower()


# ── The sink: a site URL, not a dashboard one ───────────────────────────


@pytest.mark.asyncio
async def test_the_returned_url_is_public_and_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend-relative /api/v1/media/<name> resolves against the SITE's own
    domain and 404s, so returning one would break every generated image."""
    store = FakeStore()
    monkeypatch.setattr(site_media, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )
    _patch_generation(monkeypatch)
    _patch_history(monkeypatch)

    resp = await _generate_site_image_handler({"pocket_id": "pk1", "prompt": "a calm hero"})
    body = _body(resp)

    assert body["ok"] is True
    url = body["assets"][0]["url"]
    assert url.startswith("https://")
    assert "/api/v1/media/" not in url


@pytest.mark.asyncio
async def test_an_unconfigured_store_names_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail with a reason the agent can act on, so it uses stock instead of
    retrying or inventing a URL."""
    monkeypatch.setattr(site_media, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: None, raising=False
    )

    resp = await _generate_site_image_handler({"pocket_id": "pk1", "prompt": "a hero"})

    assert resp.get("is_error") is True
    assert "search_stock_images" in _text(resp)


@pytest.mark.asyncio
async def test_a_generation_failure_never_returns_a_phantom_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    monkeypatch.setattr(site_media, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )
    _patch_generation(monkeypatch, image=None, err="upstream quota exceeded")
    _patch_history(monkeypatch)

    resp = await _generate_site_image_handler({"pocket_id": "pk1", "prompt": "a hero"})

    assert resp.get("is_error") is True
    assert "quota" in _text(resp)
    assert store.seen == [], "nothing should have been stored"


# ── Provenance: the asset shows up in /studio, tagged ───────────────────


@pytest.mark.asyncio
async def test_the_generation_is_recorded_against_the_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One asset, one history, both surfaces — tagged so the /studio gallery can
    tell a site agent's output from the user's own."""
    store = FakeStore()
    monkeypatch.setattr(site_media, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )
    _patch_generation(monkeypatch)
    recorded = _patch_history(monkeypatch)

    await _generate_site_image_handler({"pocket_id": "pk1", "prompt": "a hero"})

    assert len(recorded) == 1
    assert recorded[0]["workspace"] == "ws1"
    assert recorded[0]["source"] == "sites"
    assert recorded[0]["pocket"] == "pk1"
    # The recorded asset carries the PUBLIC url, so /studio and the page agree.
    assert recorded[0]["gen"].assets[0].url.startswith("https://")


@pytest.mark.asyncio
async def test_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each image costs money; an agent asking for 50 gets the ceiling."""
    store = FakeStore()
    monkeypatch.setattr(site_media, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )
    _patch_generation(monkeypatch)
    _patch_history(monkeypatch)

    await _generate_site_image_handler({"pocket_id": "pk1", "prompt": "a hero", "count": 50})

    assert len(store.seen) == site_media._MAX_IMAGES_PER_CALL
