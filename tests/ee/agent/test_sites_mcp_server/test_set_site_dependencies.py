# tests/ee/agent/test_sites_mcp_server/test_set_site_dependencies.py — the agent-facing
# half of author npm packages (PP-1).
#
# Created: 2026-09-24 (feat/sites-author-dependencies). Covers the new
# ``set_site_dependencies`` tool (registration, argument shape, result body), the
# ``dependencies`` param on ``create_{svelte,react,html}_site`` (resolved before the
# persist, refusals reported while the create still succeeds, the manifest lands in
# Mongo), the create-time refusal of a hand-written manifest or svelte build shell,
# and the ``edit_svelte_component`` body for a pocket whose preview is sandbox-only.

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import dependency_resolver as dr  # noqa: E402


@pytest.fixture(autouse=True)
def _default_sites_plan():
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


@pytest.fixture()
def recording_bus():
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _Bus:
        async def publish(self, event) -> None:
            return None

        def subscribe(self, event_type, handler) -> None:  # noqa: ARG002
            return None

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _Bus()  # type: ignore[attr-defined]
    yield
    bus_mod._bus = prev  # type: ignore[attr-defined]


def _identity(ws: str = "ws_1", user: str = "u_1"):
    return (
        patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value=ws),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value=user),
    )


def _body(out: dict) -> dict:
    assert not out.get("is_error"), out
    return json.loads(out["content"][0]["text"])


def _fake_resolve(resolved: dict[str, dict], rejected: list[dr.Rejection] | None = None):
    async def resolve(requests, engine, *, already_declared=()):
        result = dr.ResolveResult()
        for req in requests:
            if req.name in resolved:
                result.packages[req.name] = dr.ResolvedPackage(name=req.name, **resolved[req.name])
        result.rejected = list(rejected or [])
        return result

    return resolve


_SVELTE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css'</script><slot/>",
    "src/routes/+page.ts": "export const prerender = true",
    "src/app.css": ":root{}",
    "src/lib/components/Hero.svelte": "<h1>Hi</h1>",
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _listed_tools():
    from mcp import types
    from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

    built = build_sites_manager_server()
    if built is None:
        pytest.skip("claude_agent_sdk not installed")
    _name, server = built
    handler = server["instance"].request_handlers[types.ListToolsRequest]
    listed = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    return {t.name: t for t in listed.root.tools}


class TestRegistration:
    def test_the_setter_is_on_the_server_and_the_allow_list(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import (
            SET_SITE_DEPENDENCIES_TOOL_ID,
            SITES_TOOL_IDS,
        )
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert SET_SITE_DEPENDENCIES_TOOL_ID in SITES_TOOL_IDS
        assert SET_SITE_DEPENDENCIES_TOOL_ID in CloudSitesMcpProvider().tool_ids()
        tool = _listed_tools()["set_site_dependencies"]
        schema = tool.inputSchema or {}
        assert schema.get("required") == ["pocket_id"]
        assert set(schema["properties"]) == {"pocket_id", "add", "remove"}

    @pytest.mark.parametrize(
        "name", ["create_svelte_site", "create_react_site", "create_html_site"]
    )
    def test_every_source_engine_create_takes_dependencies(self, name) -> None:
        props = (_listed_tools()[name].inputSchema or {})["properties"]
        assert props["dependencies"]["type"] == "array"
        assert props["dependencies"]["items"]["required"] == ["name"]


# ---------------------------------------------------------------------------
# set_site_dependencies handler
# ---------------------------------------------------------------------------


class TestSetSiteDependenciesHandler:
    @pytest.mark.asyncio
    async def test_missing_identity_is_an_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        a, b = _identity(None, None)  # type: ignore[arg-type]
        with a, b:
            out = await mcp._set_site_dependencies_handler({"pocket_id": "p", "add": ["x"]})
        assert out.get("is_error")

    @pytest.mark.asyncio
    async def test_nothing_to_do_is_an_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        a, b = _identity()
        with a, b:
            out = await mcp._set_site_dependencies_handler({"pocket_id": "p"})
        assert out.get("is_error") and "`add`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_the_body_carries_packages_and_rejections(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        result = {
            "pocket_id": "p",
            "packages": {"three": {"version": "0.170.0"}},
            "rejected": [{"name": "svelte", "code": "toolchain_reserved", "reason": "provided"}],
            "changed": True,
        }
        service = AsyncMock(return_value=result)
        a, b = _identity()
        with a, b, patch("pocketpaw_ee.sites.service.set_site_dependencies", new=service):
            body = _body(
                await mcp._set_site_dependencies_handler(
                    {"pocket_id": "p", "add": '[{"name": "three"}, {"name": "svelte"}]'}
                )
            )
        service.assert_awaited_once_with(
            user_id="u_1",
            pocket_id="p",
            add=[{"name": "three"}, {"name": "svelte"}],
            remove=None,
        )
        assert body["ok"] is True
        assert body["packages"] == {"three": {"version": "0.170.0"}}
        assert body["rejected"][0]["name"] == "svelte"
        assert "refused" in body["message"]

    @pytest.mark.asyncio
    async def test_a_service_error_is_relayed(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud._core.errors import ValidationError

        service = AsyncMock(side_effect=ValidationError("site_deps.engine_unsupported", "nope"))
        a, b = _identity()
        with a, b, patch("pocketpaw_ee.sites.service.set_site_dependencies", new=service):
            out = await mcp._set_site_dependencies_handler({"pocket_id": "p", "add": ["three"]})
        assert out.get("is_error")
        assert "site_deps.engine_unsupported" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# create_* with dependencies
# ---------------------------------------------------------------------------


class TestCreateWithDependencies:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler", "engine", "source"),
        [
            ("_create_svelte_site_handler", "svelte", _SVELTE),
            ("_create_react_site_handler", "react", {"src/App.tsx": "export default () => null"}),
            ("_create_html_site_handler", "html", {"index.html": "<p>hi</p>"}),
        ],
    )
    async def test_resolved_packages_land_in_the_persisted_source(
        self, beanie_test_db, recording_bus, handler, engine, source
    ) -> None:
        """Mutation: skip ``_resolve_create_dependencies`` → no manifest in Mongo."""
        from bson import ObjectId
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        rej = dr.Rejection("left-pad", dr.LOW_DOWNLOADS, "too quiet")
        resolve = _fake_resolve({"three": {"version": "0.170.0"}}, [rej])
        a, b = _identity(str(ObjectId()), str(ObjectId()))
        with a, b, patch.object(dr, "resolve_dependencies", new=resolve):
            body = _body(
                await getattr(mcp, handler)(
                    {
                        "source": dict(source),
                        "dependencies": [{"name": "three"}, {"name": "left-pad"}],
                    }
                )
            )

        assert body["ok"] is True
        assert body["packages"] == {"three": {"version": "0.170.0"}}
        assert body["rejected"] == [rej.as_dict()]
        doc = await _PocketDoc.get(ObjectId(body["pocket_id"]))
        assert doc is not None and doc.engine == engine
        manifest = json.loads(doc.source["paw.dependencies.json"])
        assert manifest == {"schema": 1, "packages": {"three": {"version": "0.170.0"}}}

    @pytest.mark.asyncio
    async def test_all_refused_still_creates_without_a_manifest(
        self, beanie_test_db, recording_bus
    ) -> None:
        from bson import ObjectId
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        rej = dr.Rejection("svelte", dr.TOOLCHAIN_RESERVED, "provided")
        a, b = _identity(str(ObjectId()), str(ObjectId()))
        with a, b, patch.object(dr, "resolve_dependencies", new=_fake_resolve({}, [rej])):
            body = _body(
                await mcp._create_svelte_site_handler(
                    {"source": dict(_SVELTE), "dependencies": ["svelte"]}
                )
            )
        doc = await _PocketDoc.get(ObjectId(body["pocket_id"]))
        assert "paw.dependencies.json" not in doc.source
        assert body["packages"] == {} and body["rejected"][0]["code"] == dr.TOOLCHAIN_RESERVED

    @pytest.mark.asyncio
    async def test_without_dependencies_the_body_is_unchanged(
        self, beanie_test_db, recording_bus
    ) -> None:
        from bson import ObjectId
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        a, b = _identity(str(ObjectId()), str(ObjectId()))
        with a, b:
            body = _body(await mcp._create_html_site_handler({"source": {"index.html": "<p>"}}))
        assert "packages" not in body and "rejected" not in body

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler", "source"),
        [
            ("_create_svelte_site_handler", {**_SVELTE, "paw.dependencies.json": "{}"}),
            ("_create_react_site_handler", {"src/App.tsx": "x", "PAW.dependencies.json": "{}"}),
            ("_create_html_site_handler", {"index.html": "<p>", "./paw.dependencies.json": "{}"}),
        ],
    )
    async def test_a_hand_written_manifest_is_refused(self, handler, source) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        a, b = _identity()
        with a, b:
            out = await getattr(mcp, handler)({"source": source})
        assert out.get("is_error")
        assert "dependencies" in out["content"][0]["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path", ["package.json", "vite.config.js", "src/routes/+layout.ts", ".npmrc"]
    )
    async def test_svelte_create_refuses_the_reserved_build_shell(self, path) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        a, b = _identity()
        with a, b:
            out = await mcp._create_svelte_site_handler({"source": {**_SVELTE, path: "x"}})
        assert out.get("is_error")
        assert path in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# edit_svelte_component: the sandbox-only seam's response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_on_a_pocket_with_packages_says_no_preview_was_built() -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    a, b = _identity()
    with (
        a,
        b,
        patch(
            "pocketpaw_ee.sites.service.edit_svelte_component",
            new=AsyncMock(return_value=(None, False)),
        ),
    ):
        body = _body(
            await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "p",
                    "component_path": "src/lib/components/Hero.svelte",
                    "new_source": "<h1>x</h1>",
                }
            )
        )
    assert body["ok"] is True
    assert body["status"] == "draft" and body["is_live"] is False
    assert body["site"] is None and body["preview_built"] is False
    assert "sandbox" in body["message"]
