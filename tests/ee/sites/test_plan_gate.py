# tests/ee/sites/test_plan_gate.py
# Created: 2026-06-17 (fix/sites-plan-gate-asymmetry) — reproduce-first coverage
# for the Sites plan-gate asymmetry bug. Sites is a plan-gated feature: the REST
# router gates every endpoint with require_plan_feature("sites"), but the chat
# agent creates + publishes sites IN-PROCESS (the sites_manager MCP tools call the
# create handlers + publish_pocket directly), bypassing the HTTP router. Net bug:
# on a denied-plan workspace the agent happily created and deployed a live site,
# but GET /sites 403'd — a created-but-invisible resource (write path ungated,
# read path gated). This suite asserts the in-process write paths are now gated at
# the SERVICE chokepoint, identically to HTTP:
#   1. publish_pocket() / publish() raise Forbidden("plan.feature_denied") on a
#      free plan, and succeed on go/pro/pro_max/enterprise.
#   2. The create MCP handlers (landing / svelte) surface the same
#      plan.feature_denied as an MCP error on a free plan, and create on go.
# (origin/dev ships the landing + svelte create tools; the dynamic create tool is
# not yet on dev, so it is out of scope for this PR.)
# The plan is controlled by patching workspace_service.get_workspace_plan (same
# technique as tests/cloud/test_plan_feature_gate.py) so no plan-seeding is needed.
# Updated 2026-06-25 (feat/consumer-plan-ladder): rekeyed to the consumer ladder.
# Updated 2026-06-25 (decouple-sites-from-fabric): Sites now gates on the dedicated
#   "sites" flag (go+), NOT the overloaded "fabric" flag. The consumer ladder gives
#   Paw Go a site, so the gated/ungated split is {free DENIED, go ALLOWED} — go is
#   now allowed (previously denied under the fabric coupling). The Fabric ONTOLOGY
#   stays on "fabric" (enterprise-only) and is covered by its own suite.

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


# A minimal landing copy object — enough for assemble_landing_spec on the
# pro-plan success path. The go-plan path is rejected before assembly.
def _landing_content() -> dict:
    return {
        "brand": "Acme",
        "hero": {"title": "Get paid faster", "subtitle": "Invoicing for freelancers"},
        "footer": {"copyright": "Acme"},
    }


def _patch_plan(plan: str):
    """Patch workspace_service.get_workspace_plan to return a fixed plan tier for
    BOTH the sites service and the create MCP handlers (they both read the plan
    via workspace_service.get_workspace_plan). Returns the patch context manager."""
    return patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value=plan),
    )


def _patch_identity(workspace_id: str, user_id: str):
    """Patch the per-stream identity ContextVars the create MCP handlers read."""
    return (
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            return_value=workspace_id,
        ),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            return_value=user_id,
        ),
    )


@pytest.fixture()
def recording_bus():
    """Install a recording EventBus so agent_create's emit(PocketCreated) doesn't
    raise (the real bus is only wired by init_realtime() at boot). Mirrors
    tests/ee/agent/test_sites_mcp_server/test_create_dynamic_site.py."""
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


# ---------------------------------------------------------------------------
# publish() / publish_pocket() — the shared publish chokepoint (REST + MCP)
# ---------------------------------------------------------------------------


class TestPublishPlanGate:
    @pytest.mark.asyncio
    async def test_publish_pocket_on_free_plan_is_forbidden(self) -> None:
        """The shared publish path must reject a free-plan workspace with
        plan.feature_denied, BEFORE reading the pocket or invoking the generator
        (so this proves the gate, not an incidental missing-pocket error)."""
        from bson import ObjectId
        from pocketpaw_ee.cloud._core.errors import Forbidden
        from pocketpaw_ee.sites import service as sites_service

        workspace_id = str(ObjectId())
        user_id = str(ObjectId())

        with _patch_plan("free"):
            with pytest.raises(Forbidden) as ei:
                await sites_service.publish_pocket(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    pocket_id=str(ObjectId()),
                )
        assert ei.value.code == "plan.feature_denied"
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_publish_on_free_plan_is_forbidden(self) -> None:
        """publish() (the inner funnel the REST endpoint also reaches) is gated
        too, so a direct service caller can't slip past the publish_pocket guard."""
        from bson import ObjectId
        from pocketpaw_ee.cloud._core.errors import Forbidden
        from pocketpaw_ee.sites import service as sites_service

        with _patch_plan("free"):
            with pytest.raises(Forbidden) as ei:
                await sites_service.publish(
                    workspace_id=str(ObjectId()),
                    user_id=str(ObjectId()),
                    pocket_id=str(ObjectId()),
                    ripple_spec={"version": 1, "state": {}, "ui": {"type": "container"}},
                    theme={},
                )
        assert ei.value.code == "plan.feature_denied"

    @pytest.mark.asyncio
    async def test_publish_pocket_on_go_plan_passes_the_gate(
        self, beanie_test_db, recording_bus
    ) -> None:
        """A go-plan workspace passes the gate and publishes. The generator
        + Cloudflare client are faked so this runs without Bun/workerd/CF; the
        assertion is that the gate did NOT fire (the publish proceeds and persists
        a Site), not the deploy mechanics."""
        from bson import ObjectId
        from pocketpaw_ee.cloud.pockets.service import agent_create
        from pocketpaw_ee.sites import service as sites_service

        workspace_id = str(ObjectId())
        user_id = str(ObjectId())

        # Seed a real source pocket to publish (a minimal landing site spec).
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        ripple_spec = assemble_landing_spec(_landing_content())
        view, pocket_id, err = await agent_create(
            workspace_id=workspace_id,
            owner_id=user_id,
            name="Acme",
            type_="site",
            pattern="landing",
            ripple_spec=ripple_spec,
            trusted=True,
        )
        assert err is None and pocket_id is not None, err

        class _FakeBuild:
            project_dir = "/tmp/does-not-matter"

        class _FakeGenerator:
            async def build(self, **_kwargs):  # noqa: ANN003
                return _FakeBuild()

        class _FakeCloudflare:
            async def put_worker(self, **_kwargs):  # noqa: ANN003
                return None

        with _patch_plan("go"):
            doc = await sites_service.publish_pocket(
                workspace_id=workspace_id,
                user_id=user_id,
                pocket_id=pocket_id,
                _generator=_FakeGenerator(),
                _cloudflare=_FakeCloudflare(),
                _bundle_reader=lambda _d: b"worker-bundle",
            )
        assert doc is not None
        assert doc.workspace == workspace_id
        assert doc.pocket_id == pocket_id


# ---------------------------------------------------------------------------
# create MCP handlers — the in-process create bypass (landing / svelte / dynamic)
# ---------------------------------------------------------------------------


def _svelte_source() -> dict:
    return {
        "src/routes/+page.svelte": (
            "<script>import Hero from '$lib/components/Hero.svelte';</script><Hero />"
        ),
        "src/routes/+layout.svelte": "<script>import '../app.css';</script>",
        "src/routes/+page.ts": "export const prerender = true;",
        "src/app.css": ":root{}",
        "src/lib/components/Hero.svelte": "<section><h1>Hi</h1></section>",
    }


class TestCreateHandlersPlanGate:
    @pytest.mark.asyncio
    async def test_create_landing_site_on_free_plan_is_error(self, recording_bus) -> None:
        # recording_bus is installed so that, on the BUGGY (pre-fix) code, the
        # handler proceeds past the absent gate and into agent_create instead of
        # raising "EventBus not initialized" — making the failure a clean
        # "missing plan.feature_denied" rather than a noisy crash. After the fix
        # the guard fires before agent_create, so the bus is never reached.
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        workspace_id, user_id = "ws_free", "u_1"
        pw, pu = _patch_identity(workspace_id, user_id)
        with _patch_plan("free"), pw, pu:
            out = await mcp._create_landing_site_handler({"content": _landing_content()})
        assert out.get("is_error") is True
        assert "plan.feature_denied" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_create_svelte_site_on_free_plan_is_error(self, recording_bus) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        pw, pu = _patch_identity("ws_free", "u_1")
        with _patch_plan("free"), pw, pu:
            out = await mcp._create_svelte_site_handler({"source": _svelte_source()})
        assert out.get("is_error") is True
        assert "plan.feature_denied" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_create_landing_site_on_go_plan_creates(
        self, beanie_test_db, recording_bus
    ) -> None:
        """Go plan passes the gate and the landing site is created (ground
        truth: a pocket lands and the handler returns ok)."""
        from bson import ObjectId
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        workspace_id = str(ObjectId())
        user_id = str(ObjectId())
        pw, pu = _patch_identity(workspace_id, user_id)
        with _patch_plan("go"), pw, pu:
            out = await mcp._create_landing_site_handler(
                {"content": _landing_content(), "name": "Acme"}
            )
        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        doc = await _PocketDoc.get(ObjectId(body["pocket_id"]))
        assert doc is not None
        assert doc.type == "site"
