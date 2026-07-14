# tests/ee/agent/test_sites_mcp_server/test_create_html_site.py
# Created: 2026-07-12 (feat/sites-html-create-tool, HE-6) — coverage for the Paw
# Sites "html track" create tool ``create_html_site`` on the in-process
# ``pocketpaw_sites_manager`` server. It is the 4th create tool, mirroring
# ``create_svelte_site`` (the agent IS the author; a raw {path: contents} source
# map is persisted verbatim via ``agent_create`` with ``engine="html"`` +
# ``ripple_spec=None`` + ``trusted=True``) but the map is raw HTML/CSS/JS with no
# SvelteKit scaffold. Three layers:
#   1. source-map validation (``_missing_html_keys``) — the entry ``index.html``
#      is required (the generator/deploy serve it), so the create fails CLOSED on
#      a map with no entry document instead of persisting an unservable site.
#   2. Registration — the tool id rides the SAME server allowlist as publish +
#      the other three create tools.
#   3. End-to-end handler — against a real (mongomock) Beanie DB it persists the
#      source map via ``agent_create`` and reads the PERSISTED _PocketDoc back to
#      confirm engine=="html", source==<map>, type=="site", pattern=="landing"
#      (ground truth in Mongo, NOT agent narration), and NO rippleSpec.
"""Tests for the html-track create tool (create_html_site)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(autouse=True)
def _default_sites_plan():
    """create_html_site calls the shared Sites plan gate
    (sites.service.require_sites_plan) before agent_create. These tests use
    synthetic workspace ids with no seeded Workspace doc, so default the plan to
    one that unlocks Sites ("go") to exercise the create mechanics. Plan-gate
    denial is covered separately in tests/ee/sites/test_plan_gate.py."""
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


# A representative raw-HTML source map (paths -> file contents). Multi-file to
# prove the whole map persists, not just the entry document.
def _sample_source() -> dict[str, str]:
    return {
        "index.html": (
            '<!doctype html>\n<html lang="en">\n<head>\n'
            '  <meta charset="utf-8" />\n  <link rel="stylesheet" href="styles.css" />\n'
            "  <title>Bright Smile Dental</title>\n</head>\n<body>\n"
            "  <h1>Care that fits your whole family</h1>\n</body>\n</html>\n"
        ),
        "styles.css": ":root { --ink: #17130f; }\nbody { color: var(--ink); }\n",
    }


# ---------------------------------------------------------------------------
# source-map validation (pure — no identity / Mongo needed)
# ---------------------------------------------------------------------------


class TestSourceMapValidation:
    def test_complete_map_has_no_missing_keys(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _missing_html_keys

        assert _missing_html_keys(_sample_source()) == []

    def test_required_keys_are_the_entry_document(self) -> None:
        """Pin the required entry key so the contract can't silently drift — the
        edge serves index.html, so a map without it is unservable."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import HTML_REQUIRED_KEYS

        assert set(HTML_REQUIRED_KEYS) == {"index.html"}

    def test_missing_entry_document_is_reported(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _missing_html_keys

        no_entry = {k: v for k, v in _sample_source().items() if k != "index.html"}
        assert "index.html" in _missing_html_keys(no_entry)

    def test_entry_only_map_is_valid(self) -> None:
        """A single index.html with no extra assets is a complete html site."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import _missing_html_keys

        assert _missing_html_keys({"index.html": "<h1>hi</h1>"}) == []


# ---------------------------------------------------------------------------
# Registration — the tool rides the shared sites_manager allowlist
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_id_on_shared_server_allowlist(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import (
            CREATE_HTML_SITE_TOOL_ID,
            SITES_TOOL_IDS,
        )

        assert CREATE_HTML_SITE_TOOL_ID == "mcp__pocketpaw_sites_manager__create_html_site"
        assert CREATE_HTML_SITE_TOOL_ID in SITES_TOOL_IDS

    def test_create_module_exports_matching_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import CREATE_HTML_SITE_TOOL_ID
        from pocketpaw_ee.agent.mcp_servers.sites_create import (
            CREATE_HTML_SITE_TOOL_ID as CREATE_ID,
        )
        from pocketpaw_ee.agent.mcp_servers.sites_create import SITES_CREATE_TOOL_IDS

        assert CREATE_ID == CREATE_HTML_SITE_TOOL_ID
        assert CREATE_ID in SITES_CREATE_TOOL_IDS

    def test_provider_advertises_html_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import CREATE_HTML_SITE_TOOL_ID
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert CREATE_HTML_SITE_TOOL_ID in CloudSitesMcpProvider().tool_ids()


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


class TestCreateHtmlSiteEndToEnd:
    @pytest.mark.asyncio
    async def test_persists_html_pocket_with_source_map(
        self, beanie_test_db, recording_bus
    ) -> None:
        """Drive the handler against a real (mongomock) Beanie DB and read the
        persisted _PocketDoc back. Proves a pocket lands with engine=="html",
        source==<map>, type=="site", pattern=="landing" — and NO rippleSpec."""
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
            out = await sites_create_mcp._create_html_site_handler(
                {"source": source, "name": "Bright Smile"}
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        assert body["pocket"]["engine"] == "html"
        pocket_id = body["pocket_id"]
        assert pocket_id

        # Ground truth: read the persisted doc straight from Mongo.
        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        assert doc.type == "site"
        assert doc.pattern == "landing"
        assert doc.engine == "html"
        assert doc.source == source
        # The html path persists NO rippleSpec.
        assert doc.rippleSpec is None

    @pytest.mark.asyncio
    async def test_missing_identity_is_error(self) -> None:
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
            out = await sites_create_mcp._create_html_site_handler({"source": _sample_source()})

        assert out.get("is_error") is True
        assert "workspace and user context" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_source_is_error(self) -> None:
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
            out = await sites_create_mcp._create_html_site_handler({"name": "X"})

        assert out.get("is_error") is True
        assert "`source`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_entry_document_is_error_naming_index_html(self) -> None:
        """A map with no index.html fails closed and names it — so the agent can
        fix it rather than persist a site the edge can't serve."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

        no_entry = {k: v for k, v in _sample_source().items() if k != "index.html"}
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
            out = await sites_create_mcp._create_html_site_handler({"source": no_entry})

        assert out.get("is_error") is True
        assert "index.html" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_non_string_file_value_is_rejected(self) -> None:
        """Every value in an html source map is file contents — a non-string value
        is rejected (html has no binding siblings; the whole map is {path: str})."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

        bad = _sample_source()
        bad["data.json"] = {"not": "a string"}
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
            out = await sites_create_mcp._create_html_site_handler({"source": bad})

        assert out.get("is_error") is True
        assert "data.json" in out["content"][0]["text"]
