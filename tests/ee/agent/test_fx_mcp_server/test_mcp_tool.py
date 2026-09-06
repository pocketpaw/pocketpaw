# tests/ee/agent/test_fx_mcp_server/test_mcp_tool.py
# Created: 2026-09-06 (feat/fx-mcp-server, FX-2) — coverage for the in-process
# ``pocketpaw_fx`` MCP server. Mirrors the icons test layout: registration
# assertions (tool id namespacing, provider allowlist publication, the three tools
# on the built server) plus handler tests against a tmp_path registry fixture:
# search ranking + filters, get_effect happy path, unknown name with suggestions,
# svelte + needs refusal, missing/malformed registry fail-open, path traversal
# rejection, and mtime-driven cache refresh.
"""MCP server registration + handler tests for the paw-fx effects registry."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.agent.mcp_servers import fx as fx_mcp  # noqa: E402,I001


def _decode(envelope: dict) -> dict:
    assert envelope["content"][0]["type"] == "text"
    return json.loads(envelope["content"][0]["text"])


def _item(name: str, category: str, tags: list[str], summary: str, needs: list[str]) -> dict:
    return {
        "name": name,
        "version": "0.1.0",
        "category": category,
        "tags": tags,
        "summary": summary,
        "needs": needs,
        "license": "MIT",
        "origin": {"repo": "r", "commit": "c", "path": "p"},
        "options": {},
        "files": [{"path": f"_fx/effects/{name}/index.js", "content": "export {}"}]
        + [{"path": f"_fx/vendor/{n}.js", "content": ""} for n in needs],
        "snippet": f"<section data-fx='{name}'></section>",
        "usage": "place the snippet",
    }


def _write_registry(root: Path, items: list[dict]) -> None:
    (root / "items").mkdir(parents=True, exist_ok=True)
    index = {
        "version": "0.1.0",
        "generatedAt": "2026-09-06",
        "items": [
            {k: it[k] for k in ("name", "category", "tags", "summary", "needs", "license")}
            for it in items
        ],
    }
    (root / "registry.json").write_text(json.dumps(index))
    for it in items:
        (root / "items" / f"{it['name']}.json").write_text(json.dumps(it))


@pytest.fixture
def registry(tmp_path, monkeypatch) -> Path:
    items = [
        _item("aurora-css", "backgrounds", ["gradient", "hero"], "Soft aurora gradient", []),
        _item("paper-waves", "backgrounds", ["waves"], "Flowing waves via paper.js", ["paper"]),
        _item("confetti", "particles", ["celebration", "hero"], "Confetti burst", ["tsparticles"]),
        _item("popper", "particles", ["burst"], "Pops confetti", ["tsparticles"]),
    ]
    _write_registry(tmp_path, items)
    monkeypatch.setenv("PAW_FX_REGISTRY_DIR", str(tmp_path))
    monkeypatch.delenv("PAW_FX_GALLERY_URL", raising=False)
    monkeypatch.setattr(fx_mcp, "_cache", None)
    monkeypatch.setattr(fx_mcp, "_warned", False)
    return tmp_path


class TestRegistration:
    def test_tool_id_namespacing(self) -> None:
        assert fx_mcp.SERVER_NAME == "pocketpaw_fx"
        assert fx_mcp.FX_TOOL_IDS == (
            "mcp__pocketpaw_fx__search_effects",
            "mcp__pocketpaw_fx__get_effect",
            "mcp__pocketpaw_fx__list_effect_categories",
        )

    def test_provider_advertises_tool_ids(self) -> None:
        from pocketpaw_ee.extensions import CloudFxMcpProvider

        assert CloudFxMcpProvider().tool_ids() == list(fx_mcp.FX_TOOL_IDS)

    @pytest.mark.asyncio
    async def test_built_server_lists_three_tools(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        from mcp import types
        from pocketpaw_ee.extensions import CloudFxMcpProvider

        name, server = CloudFxMcpProvider().build_server()
        assert name == "pocketpaw_fx"
        handler = server["instance"].request_handlers[types.ListToolsRequest]
        result = await handler(types.ListToolsRequest(method="tools/list"))
        names = {t.name for t in result.root.tools}
        assert names == {"search_effects", "get_effect", "list_effect_categories"}

    def test_entry_point_registered(self) -> None:
        from importlib.metadata import entry_points

        eps = {ep.name for ep in entry_points(group="pocketpaw.mcp_servers")}
        assert "fx" in eps


class TestSearch:
    @pytest.mark.asyncio
    async def test_ranking_exact_name_then_tag_then_summary(self, registry) -> None:
        body = _decode(await fx_mcp._search_handler({"query": "hero"}))
        names = [i["name"] for i in body["items"]]
        # both carry the "hero" tag; ties break alphabetically
        assert names == ["aurora-css", "confetti"]
        body = _decode(await fx_mcp._search_handler({"query": "confetti"}))
        # exact name beats a summary hit
        assert [i["name"] for i in body["items"]] == ["confetti", "popper"]
        assert body["items"][0]["preview_url"] is None
        body = _decode(await fx_mcp._search_handler({"query": "nothing-here"}))
        assert body["items"] == []

    @pytest.mark.asyncio
    async def test_filters_and_preview_url(self, registry, monkeypatch) -> None:
        monkeypatch.setenv("PAW_FX_GALLERY_URL", "https://fx.example/gallery")
        body = _decode(
            await fx_mcp._search_handler(
                {"query": "a", "category": "backgrounds", "needs_js": False}
            )
        )
        assert [i["name"] for i in body["items"]] == ["aurora-css"]
        assert body["items"][0]["preview_url"] == "https://fx.example/gallery#aurora-css"
        body = _decode(await fx_mcp._search_handler({"query": "a", "needs_js": True}))
        assert {i["name"] for i in body["items"]} == {"paper-waves", "confetti", "popper"}

    @pytest.mark.asyncio
    async def test_missing_dir_is_empty_not_error(self, tmp_path, monkeypatch, caplog) -> None:
        monkeypatch.setenv("PAW_FX_REGISTRY_DIR", str(tmp_path / "nope"))
        monkeypatch.setattr(fx_mcp, "_cache", None)
        monkeypatch.setattr(fx_mcp, "_warned", False)
        out = await fx_mcp._search_handler({"query": "aurora"})
        assert not out.get("is_error")
        assert _decode(out) == {"items": []}
        await fx_mcp._search_handler({"query": "aurora"})
        assert sum("fx:" in r.message for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_malformed_index_is_empty(self, tmp_path, monkeypatch) -> None:
        (tmp_path / "registry.json").write_text("{not json")
        monkeypatch.setenv("PAW_FX_REGISTRY_DIR", str(tmp_path))
        monkeypatch.setattr(fx_mcp, "_cache", None)
        monkeypatch.setattr(fx_mcp, "_warned", False)
        assert _decode(await fx_mcp._search_handler({"query": "x"})) == {"items": []}
        assert _decode(await fx_mcp._categories_handler({})) == {"categories": []}

    @pytest.mark.asyncio
    async def test_cache_refreshes_on_mtime_change(self, registry) -> None:
        assert _decode(await fx_mcp._search_handler({"query": "glitch"}))["items"] == []
        _write_registry(registry, [_item("glitch", "text", [], "Glitch text", [])])
        os.utime(registry / "registry.json", (0, 4_000_000_000))
        assert (
            _decode(await fx_mcp._search_handler({"query": "glitch"}))["items"][0]["name"]
            == "glitch"
        )


class TestGetEffect:
    @pytest.mark.asyncio
    async def test_returns_fixture_item_for_html(self, registry) -> None:
        body = _decode(await fx_mcp._get_handler({"name": "paper-waves"}))
        assert body["engine"] == "html"
        assert body["needs"] == ["paper"]
        assert body["snippet"].startswith("<section")
        assert {f["path"] for f in body["files"]} == {
            "_fx/effects/paper-waves/index.js",
            "_fx/vendor/paper.js",
        }
        assert "note" not in body

    @pytest.mark.asyncio
    async def test_unknown_name_gives_suggestions(self, registry) -> None:
        out = await fx_mcp._get_handler({"name": "aurora"})
        assert out["is_error"]
        body = _decode(out)
        assert body["error"] == "unknown_effect"
        assert body["name"] == "aurora"
        assert body["suggestions"][0] == "aurora-css"
        assert len(body["suggestions"]) == 3

    @pytest.mark.asyncio
    async def test_svelte_refuses_needs(self, registry) -> None:
        body = _decode(await fx_mcp._get_handler({"name": "paper-waves", "engine": "svelte"}))
        assert body == {
            "error": "needs_unsupported_on_engine",
            "needs": ["paper"],
            "engine": "svelte",
        }
        body = _decode(await fx_mcp._get_handler({"name": "aurora-css", "engine": "react"}))
        assert body["engine"] == "react"
        assert body["note"].startswith("svelte/react shells not yet available")

    @pytest.mark.asyncio
    async def test_bad_engine_is_error(self, registry) -> None:
        assert (await fx_mcp._get_handler({"name": "aurora-css", "engine": "vue"}))["is_error"]

    @pytest.mark.asyncio
    async def test_path_traversal_item_rejected(self, registry) -> None:
        evil = _item("evil", "cursor", [], "bad", [])
        evil["files"] = [
            {"path": "_fx/../../etc/passwd", "content": ""},
            {"path": "index.html", "content": ""},
        ]
        _write_registry(registry, [evil])
        os.utime(registry / "registry.json", (0, 4_000_000_001))
        body = _decode(await fx_mcp._get_handler({"name": "evil"}))
        assert body["error"] == "unsafe_item_paths"
        assert set(body["paths"]) == {"_fx/../../etc/passwd", "index.html"}


class TestCategories:
    @pytest.mark.asyncio
    async def test_counts(self, registry) -> None:
        body = _decode(await fx_mcp._categories_handler({}))
        assert body["categories"] == [
            {"category": "backgrounds", "count": 2},
            {"category": "particles", "count": 2},
        ]


class TestBrowseMode:
    """An agent asking "what can I use on svelte" has no search word.

    The amended design-taste skill tells it to filter with ``needs_js=false``, so
    that call has to work without a query. A bare call with neither a query nor a
    filter still errors, because dumping the whole registry is not a search.
    """

    @pytest.mark.asyncio
    async def test_needs_js_filter_alone_browses(self, registry) -> None:
        body = _decode(await fx_mcp._search_handler({"needs_js": False}))
        assert body["items"], "needs_js=False with no query must list dependency-free effects"
        assert all(not i["needs"] for i in body["items"])

    @pytest.mark.asyncio
    async def test_category_filter_alone_browses(self, registry) -> None:
        body = _decode(await fx_mcp._search_handler({"category": "backgrounds"}))
        assert body["items"]
        assert all(i["category"] == "backgrounds" for i in body["items"])

    @pytest.mark.asyncio
    async def test_empty_query_with_no_filter_is_error(self, registry) -> None:
        assert (await fx_mcp._search_handler({}))["is_error"]
        assert (await fx_mcp._search_handler({"query": "   "}))["is_error"]

    @pytest.mark.asyncio
    async def test_query_still_narrows_when_filter_present(self, registry) -> None:
        wide = _decode(await fx_mcp._search_handler({"category": "backgrounds"}))
        narrow = _decode(
            await fx_mcp._search_handler({"query": "aurora", "category": "backgrounds"})
        )
        assert len(narrow["items"]) <= len(wide["items"])
