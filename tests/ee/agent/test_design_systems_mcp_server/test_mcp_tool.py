# tests/ee/agent/test_design_systems_mcp_server/test_mcp_tool.py
# Created: 2026-07-06 (feat/sites-crew-design-systems, SC-7b) — coverage for the
# in-process ``pocketpaw_design_systems`` MCP server (the retriever over the
# bundled DESIGN.md design-system library). Mirrors the icons / palette test
# layout: registration assertions (server name, tool id namespacing, build
# shape, provider allowlist publication) plus per-handler tests against the REAL
# bundled files (no network — the retriever is a pure local read). A content-
# quality guard loops every bundled system and parses its DESIGN.md YAML
# front-matter + manifest.json.
"""MCP server registration + handler tests for the design-system retriever."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pocketpaw_ee")
yaml = pytest.importorskip("yaml")

from pocketpaw_ee.agent.mcp_servers import design_systems as ds_mcp  # noqa: E402,I001


# Required keys the manifest index must carry for the authoring agent to choose.
_REQUIRED_MANIFEST_KEYS = {"slug", "name", "description", "aesthetic", "industries", "page_types"}


def _decode_payload(envelope: dict) -> dict:
    """MCP responses pack the JSON body into ``content[0].text``. Decode it so
    the tests can assert on dict fields without re-encoding."""
    assert "content" in envelope
    assert envelope["content"][0]["type"] == "text"
    return json.loads(envelope["content"][0]["text"])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestDesignSystemsMcpServerRegistration:
    def test_server_name_and_tool_id_namespacing(self) -> None:
        assert ds_mcp.SERVER_NAME == "pocketpaw_design_systems"
        # Tool ids must use the exact ``mcp__<server>__<tool>`` form so the
        # Claude Code allowlist machinery matches them.
        assert (
            ds_mcp.LIST_DESIGN_SYSTEMS_TOOL_ID
            == "mcp__pocketpaw_design_systems__list_design_systems"
        )
        assert (
            ds_mcp.GET_DESIGN_SYSTEM_TOOL_ID == "mcp__pocketpaw_design_systems__get_design_system"
        )
        assert ds_mcp.DESIGN_SYSTEM_TOOL_IDS == (
            ds_mcp.LIST_DESIGN_SYSTEMS_TOOL_ID,
            ds_mcp.GET_DESIGN_SYSTEM_TOOL_ID,
        )

    def test_extension_provider_advertises_both_tool_ids(self) -> None:
        """The entry-point provider's ``tool_ids()`` feeds the claude_sdk
        allowlist loop — both tool ids must come through it."""
        from pocketpaw_ee.extensions import CloudDesignSystemsMcpProvider

        advertised = CloudDesignSystemsMcpProvider().tool_ids()
        assert list(ds_mcp.DESIGN_SYSTEM_TOOL_IDS) == advertised
        assert len(advertised) == 2

    def test_provider_build_server_matches_shape(self) -> None:
        """The provider's ``build_server`` returns ``(name, server)`` when the
        Claude Agent SDK is installed (the ee group), or ``None`` otherwise."""
        from pocketpaw_ee.extensions import CloudDesignSystemsMcpProvider

        out = CloudDesignSystemsMcpProvider().build_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_design_systems"
            assert server is not None

    def test_build_server_returns_object(self) -> None:
        out = ds_mcp.build_design_systems_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_design_systems"
            assert server is not None

    def test_provider_is_ambient_not_opt_in(self) -> None:
        """The design-systems server must NOT be opt-in — otherwise the bundled
        site skill couldn't reach it without an explicit per-agent opt-in."""
        from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS

        assert "pocketpaw_design_systems" not in OPT_IN_MCP_SERVERS


# ---------------------------------------------------------------------------
# Handler — list_design_systems
# ---------------------------------------------------------------------------


class TestListDesignSystemsHandler:
    @pytest.mark.asyncio
    async def test_list_returns_at_least_five_with_required_keys(self) -> None:
        """The bundled library ships >=5 systems; each manifest carries the
        index keys the authoring agent selects on."""
        out = await ds_mcp._list_handler({})

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["ok"] is True
        assert body["count"] >= 5
        assert len(body["design_systems"]) == body["count"]

        slugs = {m["slug"] for m in body["design_systems"]}
        # slugs are unique
        assert len(slugs) == len(body["design_systems"])
        for manifest in body["design_systems"]:
            missing = _REQUIRED_MANIFEST_KEYS - set(manifest)
            assert not missing, f"{manifest.get('slug')} missing keys: {missing}"
            assert isinstance(manifest["aesthetic"], list) and manifest["aesthetic"]
            assert isinstance(manifest["industries"], list) and manifest["industries"]

    @pytest.mark.asyncio
    async def test_list_takes_no_required_args(self) -> None:
        """list ignores extra/empty args and still returns the catalogue."""
        out = await ds_mcp._list_handler({"unused": 1})
        assert not out.get("is_error")
        assert _decode_payload(out)["count"] >= 5


# ---------------------------------------------------------------------------
# Handler — get_design_system
# ---------------------------------------------------------------------------


class TestGetDesignSystemHandler:
    @pytest.mark.asyncio
    async def test_get_known_slug_returns_design_md_tokens_css_manifest(self) -> None:
        """A known slug returns non-empty DESIGN.md + tokens.css + a manifest."""
        out = await ds_mcp._get_handler({"slug": "clean-saas"})

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["ok"] is True
        assert body["slug"] == "clean-saas"
        assert body["name"]  # non-empty display name
        # DESIGN.md carries YAML front-matter + prose
        assert body["design_md"].startswith("---")
        assert "## Colors" in body["design_md"]
        # tokens.css carries the compiled custom properties
        assert body["tokens_css"].strip()
        assert "--color-primary-500" in body["tokens_css"]
        assert ":root" in body["tokens_css"]
        # manifest round-trips the index metadata
        assert body["manifest"]["slug"] == "clean-saas"
        assert set(_REQUIRED_MANIFEST_KEYS) <= set(body["manifest"])

    @pytest.mark.asyncio
    async def test_get_every_listed_slug_resolves(self) -> None:
        """Every slug the list advertises can be fetched — the two tools agree."""
        listed = _decode_payload(await ds_mcp._list_handler({}))["design_systems"]
        for manifest in listed:
            got = await ds_mcp._get_handler({"slug": manifest["slug"]})
            assert not got.get("is_error"), manifest["slug"]
            body = _decode_payload(got)
            assert body["design_md"].startswith("---")
            assert "--color-primary-500" in body["tokens_css"]

    @pytest.mark.asyncio
    async def test_unknown_slug_soft_errors_and_lists_valid_slugs(self) -> None:
        """An unknown slug returns an ``_error_response`` (never raises) whose
        message lists the valid slugs so the agent can recover."""
        out = await ds_mcp._get_handler({"slug": "does-not-exist"})

        assert out.get("is_error") is True
        text = out["content"][0]["text"]
        assert "unknown design system" in text
        # the error names at least one real slug so the agent can retry
        assert "clean-saas" in text

    @pytest.mark.asyncio
    async def test_path_traversal_slug_rejected(self) -> None:
        """A crafted traversal slug is not in the valid set → soft error, never a
        file read outside the bundle."""
        out = await ds_mcp._get_handler({"slug": "../../etc/passwd"})
        assert out.get("is_error") is True
        assert "unknown design system" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_empty_slug_returns_error(self) -> None:
        out = await ds_mcp._get_handler({"slug": "   "})
        assert out.get("is_error") is True
        assert "non-empty" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_slug_returns_error(self) -> None:
        out = await ds_mcp._get_handler({})
        assert out.get("is_error") is True
        assert "slug" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# Content-quality guard — every bundled system parses (YAML + JSON)
# ---------------------------------------------------------------------------


class TestBundledContentQuality:
    def test_every_bundled_system_parses_and_is_consistent(self) -> None:
        """Loop the bundled dir: every DESIGN.md front-matter parses as YAML,
        every manifest.json parses, tokens.css exists, slugs are unique, and the
        DESIGN.md/manifest/dir slugs agree. This guards content quality — a
        malformed system fails CI, not a generated site."""
        from pocketpaw.bundled_design_systems import bundled_design_systems_dir

        root = bundled_design_systems_dir()
        assert root.is_dir(), root
        system_dirs = sorted(d for d in root.iterdir() if d.is_dir())
        assert len(system_dirs) >= 5

        seen_slugs: set[str] = set()
        for system_dir in system_dirs:
            design_md = (system_dir / "DESIGN.md").read_text(encoding="utf-8")
            assert design_md.startswith("---"), system_dir.name
            # front-matter is the text between the first two --- delimiters
            _, front_matter, _body = design_md.split("---", 2)
            data = yaml.safe_load(front_matter)
            assert isinstance(data, dict), system_dir.name

            manifest = json.loads((system_dir / "manifest.json").read_text(encoding="utf-8"))
            tokens_css = (system_dir / "tokens.css").read_text(encoding="utf-8")
            assert tokens_css.strip()

            # slug agreement across dir name, front-matter, and manifest
            assert data["slug"] == system_dir.name == manifest["slug"], system_dir.name
            assert system_dir.name not in seen_slugs, f"duplicate slug {system_dir.name}"
            seen_slugs.add(system_dir.name)

            # full 50–900 role scales for primary/secondary/neutral (the taste bar)
            for role in ("primary", "secondary", "neutral"):
                scale = data["colors"][role]
                for step in ("50", "100", "500", "900"):
                    assert step in scale, f"{system_dir.name}/{role} missing {step}"
                    assert str(scale[step]).startswith("#")

            # a real display→caption type scale + font pairing
            typ = data["typography"]
            assert {"display", "body"} <= set(typ["fonts"])
            assert {"display", "caption"} <= set(typ["scale"])

            # component tokens with states for button/card/input
            comps = data["components"]
            assert {"button", "card", "input"} <= set(comps)
            assert "hover" in comps["button"] and "focus" in comps["button"]
