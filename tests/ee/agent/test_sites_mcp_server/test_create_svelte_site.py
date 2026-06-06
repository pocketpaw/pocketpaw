# tests/ee/agent/test_sites_mcp_server/test_create_svelte_site.py
# Created: 2026-06-04 (feat/sites-svelte-engine) — coverage for the Paw Sites
# "Svelte track" create tool ``create_svelte_site`` on the in-process
# ``pocketpaw_sites_manager`` server. Three layers:
#   1. §4.3 source-map validation (``_missing_source_keys``) — the required
#      SvelteKit keys are enforced (composition root, +layout.svelte, +page.ts,
#      app.css, at least one src/lib/components/*.svelte) so the create fails
#      CLOSED on a half-authored map instead of persisting an unbuildable site.
#   2. Registration — the tool id rides the SAME server allowlist as publish +
#      create_landing_site.
#   3. End-to-end handler — against a real (mongomock) Beanie DB it persists the
#      source map via ``agent_create`` and reads the PERSISTED _PocketDoc back to
#      confirm engine=="svelte", source==<map>, type=="site", pattern=="landing"
#      (ground truth in Mongo, NOT agent narration).
"""Tests for the svelte-track create tool (create_svelte_site)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pocketpaw_ee")


# A representative §4.3-complete source map (paths -> file contents).
def _sample_source() -> dict[str, str]:
    return {
        "src/routes/+page.svelte": (
            "<script>\n  import Hero from '$lib/components/Hero.svelte';\n"
            "  import Pricing from '$lib/components/Pricing.svelte';\n</script>\n"
            "<Hero />\n<Pricing />\n"
        ),
        "src/routes/+layout.svelte": (
            "<script>\n  import '../app.css';\n  let { children } = $props();\n</script>\n"
            "{@render children()}\n"
        ),
        "src/routes/+page.ts": "export const prerender = true;\n",
        "src/app.css": ":root { --ink: #17130f; --green: #2ee08a; }\n",
        "src/lib/components/Hero.svelte": (
            '<section class="hero"><h1>Get paid faster</h1></section>\n'
        ),
        "src/lib/components/Pricing.svelte": ('<section id="pricing"><h2>Plans</h2></section>\n'),
        "src/lib/reveal.js": "export function reveal(node) { return {}; }\n",
    }


# ---------------------------------------------------------------------------
# §4.3 source-map validation (pure — no identity / Mongo needed)
# ---------------------------------------------------------------------------


class TestSourceMapValidation:
    def test_complete_map_has_no_missing_keys(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _missing_source_keys

        assert _missing_source_keys(_sample_source()) == []

    def test_required_exact_keys_are_the_43_contract(self) -> None:
        """Pin the §4.3 required exact keys so the contract can't silently drift."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import SVELTE_REQUIRED_EXACT_KEYS

        assert set(SVELTE_REQUIRED_EXACT_KEYS) == {
            "src/routes/+page.svelte",
            "src/routes/+layout.svelte",
            "src/routes/+page.ts",
            "src/app.css",
        }

    @pytest.mark.parametrize(
        "drop_key",
        [
            "src/routes/+page.svelte",
            "src/routes/+layout.svelte",
            "src/routes/+page.ts",
            "src/app.css",
        ],
    )
    def test_missing_exact_key_is_reported(self, drop_key: str) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _missing_source_keys

        partial = {k: v for k, v in _sample_source().items() if k != drop_key}
        assert drop_key in _missing_source_keys(partial)

    def test_missing_components_section_is_reported(self) -> None:
        """A map with no src/lib/components/*.svelte fails — there's no page to
        render without at least one section component."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import _missing_source_keys

        no_components = {
            k: v for k, v in _sample_source().items() if not k.startswith("src/lib/components/")
        }
        missing = _missing_source_keys(no_components)
        assert any("src/lib/components/" in m for m in missing)


# ---------------------------------------------------------------------------
# Registration — the tool rides the shared sites_manager allowlist
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_id_on_shared_server_allowlist(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import (
            CREATE_SVELTE_SITE_TOOL_ID,
            SITES_TOOL_IDS,
        )

        assert CREATE_SVELTE_SITE_TOOL_ID == "mcp__pocketpaw_sites_manager__create_svelte_site"
        assert CREATE_SVELTE_SITE_TOOL_ID in SITES_TOOL_IDS

    def test_provider_advertises_svelte_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import CREATE_SVELTE_SITE_TOOL_ID
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert CREATE_SVELTE_SITE_TOOL_ID in CloudSitesMcpProvider().tool_ids()


# ---------------------------------------------------------------------------
# End-to-end handler — persist + read back from Mongo (ground truth)
# ---------------------------------------------------------------------------


@pytest.fixture()
def recording_bus():
    """Install a recording EventBus so ``agent_create``'s ``emit(PocketCreated)``
    doesn't raise (the real bus is only wired by ``init_realtime()`` at boot).
    Mirrors ``tests/cloud/conftest.py``, which isn't visible from this package."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


class TestCreateSvelteSiteEndToEnd:
    @pytest.mark.asyncio
    async def test_persists_svelte_pocket_with_source_map(
        self, beanie_test_db, recording_bus
    ) -> None:
        """Drive the handler against a real (mongomock) Beanie DB and read the
        persisted _PocketDoc back. Proves a pocket lands with engine=="svelte",
        source==<map>, type=="site", pattern=="landing" — and NO rippleSpec."""
        from unittest.mock import patch

        from bson import ObjectId
        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        workspace_id = str(ObjectId())
        user_id = str(ObjectId())
        source = _sample_source()

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value=workspace_id,
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value=user_id,
            ),
        ):
            out = await sites_create_mcp._create_svelte_site_handler(
                {"source": source, "name": "Tally Svelte"}
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        assert body["pocket"]["engine"] == "svelte"
        pocket_id = body["pocket_id"]
        assert pocket_id

        # Ground truth: read the persisted doc straight from Mongo.
        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        assert doc.type == "site"
        assert doc.pattern == "landing"
        assert doc.engine == "svelte"
        assert doc.source == source
        # The svelte path persists NO rippleSpec.
        assert doc.rippleSpec is None

    @pytest.mark.asyncio
    async def test_missing_identity_is_error(self) -> None:
        from unittest.mock import patch

        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value=None,
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value=None,
            ),
        ):
            out = await sites_create_mcp._create_svelte_site_handler({"source": _sample_source()})

        assert out.get("is_error") is True
        assert "workspace and user context" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_source_is_error(self) -> None:
        from unittest.mock import patch

        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value="ws_1",
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value="u_1",
            ),
        ):
            out = await sites_create_mcp._create_svelte_site_handler({"name": "X"})

        assert out.get("is_error") is True
        assert "`source`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_incomplete_source_map_is_error_naming_missing_file(self) -> None:
        """A map missing a §4.3 required file fails closed and names the file —
        so the agent can fix it rather than persist an unbuildable site."""
        from unittest.mock import patch

        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

        partial = {k: v for k, v in _sample_source().items() if k != "src/routes/+page.ts"}
        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value="ws_1",
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value="u_1",
            ),
        ):
            out = await sites_create_mcp._create_svelte_site_handler({"source": partial})

        assert out.get("is_error") is True
        assert "src/routes/+page.ts" in out["content"][0]["text"]
