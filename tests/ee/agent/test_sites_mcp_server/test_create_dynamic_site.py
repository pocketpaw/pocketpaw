# tests/ee/agent/test_sites_mcp_server/test_create_dynamic_site.py
# Created: 2026-06-14 (feat/dynamic-sites-authoring) — coverage for the Paw Sites
# "Dynamic track" create tool ``create_dynamic_site`` (RFC 12 A2) on the
# in-process ``pocketpaw_sites_manager`` server. Three layers:
#   1. Dynamic-spec validation (``_validate_dynamic_spec``) — a spec must declare
#      a `ui` tree, an `objects` block, AND at least one live binding
#      (sources/actions/auth), with every source/action referencing a declared
#      object; the create fails CLOSED otherwise so a static / malformed spec
#      can't slip through the dynamic tool.
#   2. Registration — the tool id rides the SAME server allowlist as publish +
#      create_landing_site + create_svelte_site, and the provider advertises it.
#   3. End-to-end handler — against a real (mongomock) Beanie DB it persists the
#      dynamic rippleSpec via ``agent_create`` and reads the PERSISTED _PocketDoc
#      back to confirm type=="site", pattern=="dynamic", and — the load-bearing
#      assertion — that the dynamic blocks (objects/sources/actions) SURVIVED
#      normalization on the persisted rippleSpec (ground truth in Mongo, NOT agent
#      narration). If the normalizer ever strips them the publish path silently
#      generates a static site, so this guards the whole dynamic pipeline.
# Updated: rebase onto dev — create_dynamic_site now runs the shared Sites plan
# gate (require_sites_plan) before agent_create, like the landing + svelte create
# handlers. Added the autouse ``_default_sites_plan`` fixture (mirrors
# test_create_svelte_site.py) defaulting the plan to "go" so the end-to-end create
# test exercises the persist mechanics; gate DENIAL is covered in
# tests/ee/sites/test_plan_gate.py.
"""Tests for the dynamic-track create tool (create_dynamic_site)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(autouse=True)
def _default_sites_plan():
    """create_dynamic_site now calls the shared Sites plan gate
    (sites.service.require_sites_plan) before agent_create — same gate the landing
    + svelte create handlers run (fix/sites-plan-gate-asymmetry, then
    decouple-sites-from-fabric moved it onto the dedicated "sites" flag). These
    tests use synthetic workspace ids with no seeded Workspace doc, so default the
    plan to one that unlocks Sites ("go") to exercise the create mechanics. Plan-
    gate denial is covered separately in tests/ee/sites/test_plan_gate.py. Mirrors
    the autouse fixture in test_create_svelte_site.py."""
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


# A representative guestbook dynamic spec: read source + write action over one
# D1 object. Mirrors paw-sites/tests/fixtures/guestbook-spec.json.
def _guestbook_spec() -> dict:
    return {
        "ui": {
            "type": "container",
            "children": [
                {"type": "heading", "props": {"text": "Guestbook"}},
                {"type": "table", "props": {"data": "{entries}"}},
            ],
        },
        "objects": [
            {
                "name": "entry",
                "fields": {"id": "text", "name": "text", "message": "text"},
                "primaryKey": "id",
            }
        ],
        "sources": [
            {
                "name": "entries",
                "kind": "data",
                "object": "entry",
                "orderBy": "name",
                "refresh": "pocket_open",
            }
        ],
        "actions": [
            {"name": "sign", "object": "entry", "op": "insert", "confirm": "Sign the guestbook?"}
        ],
    }


# ---------------------------------------------------------------------------
# Dynamic-spec validation (pure — no identity / Mongo needed)
# ---------------------------------------------------------------------------


class TestDynamicSpecValidation:
    def test_complete_guestbook_spec_is_valid(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _validate_dynamic_spec

        assert _validate_dynamic_spec(_guestbook_spec()) == []

    def test_auth_only_spec_is_valid(self) -> None:
        """auth:true with objects but no sources/actions is still dynamic."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import _validate_dynamic_spec

        spec = {
            "ui": {"type": "container", "children": []},
            "objects": [{"name": "x", "fields": {"id": "text"}}],
            "auth": True,
        }
        assert _validate_dynamic_spec(spec) == []

    def test_static_spec_no_bindings_is_rejected(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _validate_dynamic_spec

        spec = {
            "ui": {"type": "container", "children": []},
            "objects": [{"name": "entry", "fields": {"id": "text"}}],
        }
        problems = _validate_dynamic_spec(spec)
        assert any("live binding" in p for p in problems)

    def test_missing_objects_is_rejected(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _validate_dynamic_spec

        spec = {
            "ui": {"type": "container", "children": []},
            "sources": [{"name": "s", "kind": "data", "object": "entry", "refresh": "pocket_open"}],
        }
        problems = _validate_dynamic_spec(spec)
        assert any("objects" in p for p in problems)

    def test_missing_ui_is_rejected(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _validate_dynamic_spec

        spec = {k: v for k, v in _guestbook_spec().items() if k != "ui"}
        problems = _validate_dynamic_spec(spec)
        assert any("`ui`" in p for p in problems)

    def test_source_referencing_undeclared_object_is_rejected(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _validate_dynamic_spec

        spec = _guestbook_spec()
        spec["sources"][0]["object"] = "ghost"
        problems = _validate_dynamic_spec(spec)
        assert any("undeclared object" in p and "ghost" in p for p in problems)


# ---------------------------------------------------------------------------
# Registration — the tool rides the shared sites_manager allowlist
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_id_on_shared_server_allowlist(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import (
            CREATE_DYNAMIC_SITE_TOOL_ID,
            SITES_TOOL_IDS,
        )

        assert CREATE_DYNAMIC_SITE_TOOL_ID == "mcp__pocketpaw_sites_manager__create_dynamic_site"
        assert CREATE_DYNAMIC_SITE_TOOL_ID in SITES_TOOL_IDS

    def test_provider_advertises_dynamic_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import CREATE_DYNAMIC_SITE_TOOL_ID
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert CREATE_DYNAMIC_SITE_TOOL_ID in CloudSitesMcpProvider().tool_ids()


# ---------------------------------------------------------------------------
# End-to-end handler — persist + read back from Mongo (ground truth)
# ---------------------------------------------------------------------------


@pytest.fixture()
def recording_bus():
    """Install a recording EventBus so ``agent_create``'s ``emit(PocketCreated)``
    doesn't raise (the real bus is only wired by ``init_realtime()`` at boot).
    Mirrors tests/ee/agent/test_sites_mcp_server/test_create_svelte_site.py."""
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


class TestCreateDynamicSiteEndToEnd:
    @pytest.mark.asyncio
    async def test_persists_dynamic_pocket_with_bindings_intact(
        self, beanie_test_db, recording_bus
    ) -> None:
        """Drive the handler against a real (mongomock) Beanie DB and read the
        persisted _PocketDoc back. Proves a pocket lands with type=="site",
        pattern=="dynamic", and the dynamic blocks SURVIVED on the rippleSpec —
        the load-bearing guarantee for the publish→generator path."""
        from unittest.mock import patch

        from bson import ObjectId
        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        workspace_id = str(ObjectId())
        user_id = str(ObjectId())

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
            out = await sites_create_mcp._create_dynamic_site_handler(
                {"spec": _guestbook_spec(), "name": "Guestbook"}
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        pocket_id = body["pocket_id"]
        assert pocket_id

        # Ground truth: read the persisted doc straight from Mongo.
        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        assert doc.type == "site"
        assert doc.pattern == "dynamic"
        # The dynamic blocks MUST survive normalization on the persisted rippleSpec
        # — publish carries the whole spec to the paw-sites generator, which only
        # scaffolds the D1 + remote fns when these keys are present.
        spec = doc.rippleSpec
        assert isinstance(spec, dict)
        assert [o["name"] for o in spec["objects"]] == ["entry"]
        assert [s["name"] for s in spec["sources"]] == ["entries"]
        assert [a["name"] for a in spec["actions"]] == ["sign"]
        # The source's table binding the UI reads survived too.
        assert spec["sources"][0]["object"] == "entry"

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
            out = await sites_create_mcp._create_dynamic_site_handler({"spec": _guestbook_spec()})

        assert out.get("is_error") is True
        assert "workspace and user context" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_spec_is_error(self) -> None:
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
            out = await sites_create_mcp._create_dynamic_site_handler({"name": "X"})

        assert out.get("is_error") is True
        assert "`spec`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_static_spec_is_error_pointing_at_landing(self) -> None:
        """A spec with no live bindings fails closed and steers the agent to
        create_landing_site instead of persisting a static page as 'dynamic'."""
        from unittest.mock import patch

        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

        static_spec = {
            "ui": {"type": "container", "children": []},
            "objects": [{"name": "entry", "fields": {"id": "text"}}],
        }
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
            out = await sites_create_mcp._create_dynamic_site_handler({"spec": static_spec})

        assert out.get("is_error") is True
        assert "create_landing_site" in out["content"][0]["text"]
