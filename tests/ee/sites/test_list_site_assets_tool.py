# tests/ee/sites/test_list_site_assets_tool.py — the agent-side asset tool.
# Created 2026-08-31 (feat/sites-public-asset-uploads). New file.
#
# Under test: ``sites_manager__list_site_assets`` — how the site-authoring agent
# discovers the images the OWNER uploaded, so it can embed a real logo instead of
# inventing a path or defaulting to stock photography.
#
# REGISTRATION IS NOT REACHABILITY, and that distinction is the reason this file
# exists. The /sites MCP allow-list is a hard whitelist: a tool can be built,
# named in ``tools=[...]`` and still be filtered out at the surface, in which case
# the agent simply never sees it and the failure looks like the model choosing not
# to call it. So one test asserts the id rides ``SITES_TOOL_IDS`` and a separate
# one asserts it survives ``_load_mcp_tool_ids().sites_allow``.
#
# What these prove:
#   * the tool is registered AND reachable on /sites;
#   * workspace comes from the per-stream ContextVars and NEVER from the tool
#     args — a prompt-injected ``workspace_id`` must not read another tenant;
#   * an unconfigured deployment gets a calm "no uploads, use stock" answer, not
#     an error the agent will retry and not silence it will fill with a guess;
#   * a missing pocket_id and a missing identity both fail closed.

from __future__ import annotations

import pytest
from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp
from pocketpaw_ee.agent.mcp_servers.sites import (
    LIST_SITE_ASSETS_TOOL_ID,
    SITES_TOOL_IDS,
    _list_site_assets_handler,
)
from pocketpaw_ee.sites.public_assets import PublicAsset


def _text(resp: dict) -> str:
    return "".join(part.get("text", "") for part in resp.get("content", []))


class FakeStore:
    def __init__(self, assets: list[PublicAsset] | None = None) -> None:
        self._assets = assets or []
        self.seen: list[tuple[str, str]] = []

    async def list(self, *, workspace_id: str, pocket_id: str) -> list[PublicAsset]:
        self.seen.append((workspace_id, pocket_id))
        return self._assets


def _asset(name: str) -> PublicAsset:
    return PublicAsset(
        key=f"sites-assets/ws1/pk1/{'a' * 16}-{name}",
        url=f"https://cdn.example.test/public/{name}",
        mime="image/png",
        size=1234,
        filename=name,
        sha256="a" * 16,
    )


# ── Registration and reachability are two different facts ───────────────


def test_the_tool_id_rides_the_sites_tool_ids_tuple() -> None:
    assert LIST_SITE_ASSETS_TOOL_ID in SITES_TOOL_IDS


def test_the_tool_is_actually_reachable_on_the_sites_surface() -> None:
    """The /sites allow-list is a hard whitelist — an absent id is filtered out."""
    from pocketpaw_ee.cloud.surface.surface_registry import _load_mcp_tool_ids

    ids = _load_mcp_tool_ids()
    assert ids.loaded, "the MCP allow-lists failed to load; scoping would be disabled"
    assert LIST_SITE_ASSETS_TOOL_ID in (ids.sites_allow or frozenset())


# ── Tenancy: identity comes from the stream, never from the args ────────


@pytest.mark.asyncio
async def test_a_workspace_id_in_the_args_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE injection guard: args are model-controlled, ContextVars are not."""
    store = FakeStore([_asset("logo.png")])
    monkeypatch.setattr(sites_mcp, "_identity", lambda: ("real-ws", "user-1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )

    await _list_site_assets_handler({"pocket_id": "pk1", "workspace_id": "victim-ws"})

    assert store.seen == [("real-ws", "pk1")]


@pytest.mark.asyncio
async def test_no_active_workspace_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sites_mcp, "_identity", lambda: (None, None))
    resp = await _list_site_assets_handler({"pocket_id": "pk1"})
    assert resp.get("is_error") is True
    assert "workspace" in _text(resp).lower()


@pytest.mark.asyncio
async def test_a_missing_pocket_id_is_refused() -> None:
    resp = await _list_site_assets_handler({})
    assert resp.get("is_error") is True
    assert "pocket_id" in _text(resp)


# ── The answers the agent has to be able to act on ──────────────────────


@pytest.mark.asyncio
async def test_assets_come_back_with_embeddable_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore([_asset("logo.png"), _asset("team.png")])
    monkeypatch.setattr(sites_mcp, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )

    resp = await _list_site_assets_handler({"pocket_id": "pk1"})
    body = _text(resp)

    assert resp.get("is_error") is not True
    assert "https://cdn.example.test/public/logo.png" in body
    assert '"count": 2' in body or '"count":2' in body


@pytest.mark.asyncio
async def test_an_unconfigured_deployment_answers_calmly_not_with_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error would invite a retry; silence would invite an invented URL."""
    monkeypatch.setattr(sites_mcp, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: None, raising=False
    )

    resp = await _list_site_assets_handler({"pocket_id": "pk1"})
    body = _text(resp)

    assert resp.get("is_error") is not True
    assert "not configured" in body
    # It must steer the agent somewhere real rather than leaving it to guess.
    assert "stock" in body.lower()


@pytest.mark.asyncio
async def test_an_empty_site_says_so_rather_than_returning_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore([])
    monkeypatch.setattr(sites_mcp, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: store, raising=False
    )

    resp = await _list_site_assets_handler({"pocket_id": "pk1"})
    assert resp.get("is_error") is not True
    assert "not uploaded any images" in _text(resp)


@pytest.mark.asyncio
async def test_a_store_failure_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Boom:
        async def list(self, **_kw):
            raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(sites_mcp, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(
        "pocketpaw_ee.sites.public_assets.public_asset_store", lambda: Boom(), raising=False
    )

    resp = await _list_site_assets_handler({"pocket_id": "pk1"})
    assert resp.get("is_error") is True
    assert "bucket unreachable" in _text(resp)
