# tests/cloud/test_paw_bar_agent_provisioning.py — auto-provision a DEDICATED
# concierge agent per site (feat/site-dedicated-agent).
#
# Created 2026-07-23: covers ensure_site_agent + the two triggers.
#   * Pure helpers: slug/name/persona derivation + conversation-starter rules
#     (catalog, gated-action labels, generic fallback, cap 4).
#   * Widget-create trigger: a widget with NO agent_id on a site-backed pocket
#     auto-creates a dedicated agent named "<Site> Concierge", binds it, and emits
#     AgentCreated (created THROUGH the agents service, not a direct write).
#   * Idempotency: a re-run adopts the same agent; a manual agent_id is honored and
#     never replaced; a plain (non-site) widget stays unbound.
#   * Concierge-enable trigger: flipping the kill switch ON provisions an unbound
#     site widget.
#   * Regression: an unbound widget's chat still 409s (no fallback-to-universal).
#   * Identity seeding: welcome_message/starters degrade gracefully because the
#     ASG-1 identity fields are ABSENT on this branch (the created agent carries
#     neither field); starters ride the frame config payload.

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import (
    PawBarActionSpec,
    PawBarBlock,
    PawBarCatalogItem,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import PawBarStore

_WS = "ws-1"
_POCKET = "pocket-1"
_OWNER = "user:maya"
_VALID_KEY = "site_key_" + "a" * 24
_ORIGIN = "https://brewco.com"
_CUST = "cust-0001"


# --------------------------------------------------------------------------- #
# Spec / widget / site builders
# --------------------------------------------------------------------------- #


def _spec(
    *,
    with_catalog: bool = False,
    with_actions: bool = False,
    pocket_id: str = _POCKET,
) -> PawBarSpec:
    catalog = (
        [PawBarCatalogItem(id="oat_latte", name="Oat Milk Latte", price_cents=500)]
        if with_catalog
        else []
    )
    actions = (
        [
            PawBarActionSpec(verb="book_table", policy="gated", label="Book a table"),
            PawBarActionSpec(verb="request_quote", policy="gated", label="Request a quote"),
        ]
        if with_actions
        else []
    )
    return PawBarSpec(
        widget_id="pp_seed",
        pocket_id=pocket_id,
        blocks=[PawBarBlock(type="text", content="Hi from Brew & Co")],
        catalog=catalog,
        actions=actions,
    )


def _widget(**ov: Any) -> PawBarWidget:
    d: dict[str, Any] = dict(
        pocket_id=_POCKET,
        owner=_OWNER,
        name="Brew & Co",
        spec=_spec(),
        allowed_domains=["brewco.com"],
        agent_id="",
        workspace_id=_WS,
        rate_limit_per_min=60,
        per_customer_limit_per_min=10,
    )
    d.update(ov)
    return PawBarWidget(**d)


async def _site(**ov: Any):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace=_WS,
        pocket_id=_POCKET,
        owner=_OWNER,
        name="Brew & Co",
        signed_key=_VALID_KEY,
        allowed_origins=["brewco.com"],
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


def _create_payload(**ov: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pocket_id": _POCKET,
        "owner": _OWNER,
        "name": "Brew & Co",
        "spec": _spec(with_catalog=True, with_actions=True).model_dump(),
        "allowed_domains": ["brewco.com"],
        "rate_limit_per_min": 60,
        "per_customer_limit_per_min": 10,
    }
    payload.update(ov)
    return payload


@pytest_asyncio.fixture
async def client(tmp_path, mongo_db):
    """A public+admin app client backed by a tmp paw-bar store (widget) + Beanie
    (Site + Agent). ``current_workspace_id`` is pinned to ws-1. ``get_paw_bar_store``
    is patched at the source so BOTH the router and the provisioning module resolve
    the SAME tmp store. Yields ``(client, store)``."""
    from unittest.mock import patch

    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[current_workspace_id] = lambda: _WS

    store = PawBarStore(tmp_path / "provisioning.db")
    with patch("pocketpaw_ee.api.get_paw_bar_store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c, store


# --------------------------------------------------------------------------- #
# Pure helpers — naming + conversation-starter derivation
# --------------------------------------------------------------------------- #


class TestHelpers:
    def test_name_and_slug_and_persona(self) -> None:
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        assert ap.concierge_name("Brew & Co") == "Brew & Co Concierge"
        assert ap.concierge_name("") == "Site Concierge"
        assert ap.concierge_name("  ") == "Site Concierge"
        assert ap.concierge_slug("abc123") == "concierge-abc123"
        assert "Brew & Co" in ap.concierge_persona("Brew & Co")
        assert "this site" in ap.concierge_persona("")

    def test_starters_from_catalog_and_gated_actions(self) -> None:
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        widget = _widget(spec=_spec(with_catalog=True, with_actions=True))
        starters = ap.derive_conversation_starters(widget)
        assert starters[0] == "What do you sell?"
        assert "Book a table?" in starters
        assert "Request a quote?" in starters

    def test_starters_generic_fallback_when_empty(self) -> None:
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        widget = _widget(spec=_spec())  # no catalog, no actions
        assert ap.derive_conversation_starters(widget) == ["What can you help me with?"]

    def test_starters_capped_at_four(self) -> None:
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        actions = [
            PawBarActionSpec(verb=f"do_{i}", policy="gated", label=f"Action {i}") for i in range(6)
        ]
        spec = PawBarSpec(
            widget_id="pp_seed",
            pocket_id=_POCKET,
            catalog=[PawBarCatalogItem(id="x", name="X")],
            actions=actions,
        )
        starters = ap.derive_conversation_starters(_widget(spec=spec))
        assert len(starters) == 4
        assert starters[0] == "What do you sell?"

    def test_starters_ignore_unlabeled_or_auto_actions(self) -> None:
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        spec = PawBarSpec(
            widget_id="pp_seed",
            pocket_id=_POCKET,
            actions=[
                PawBarActionSpec(verb="add_to_cart", policy="auto", label="Add to cart"),
                PawBarActionSpec(verb="silent_verb", policy="gated", label=""),
            ],
        )
        # auto action + label-less gated action → neither yields a starter → generic.
        assert ap.derive_conversation_starters(_widget(spec=spec)) == ["What can you help me with?"]


# --------------------------------------------------------------------------- #
# Widget-create trigger
# --------------------------------------------------------------------------- #


class TestWidgetCreateTrigger:
    @pytest.mark.asyncio
    async def test_create_on_site_pocket_auto_provisions_and_binds(
        self, client, recording_bus
    ) -> None:
        from pocketpaw_ee.cloud._core.realtime.events import AgentCreated
        from pocketpaw_ee.cloud.agents import service as agents_service

        c, _store = client
        site = await _site()

        res = await c.post("/paw-bar/widgets", json=_create_payload())
        assert res.status_code == 201
        body = res.json()
        agent_id = body["agent_id"]
        assert agent_id, "widget should be bound to an auto-provisioned agent"

        # The agent exists, is named for the site, and was created via the service.
        agent = await agents_service.get(agent_id)
        assert agent.name == "Brew & Co Concierge"
        assert agent.slug == f"concierge-{site.id}"
        assert agent.workspace_id == _WS
        assert agent.owner == _OWNER
        assert agent.config.soul_archetype == "The Site Concierge"
        assert "Brew & Co" in agent.config.soul_persona

        created = [e for e in recording_bus.events if isinstance(e, AgentCreated)]
        assert any(e.data.get("agent_id") == agent_id for e in created)

    @pytest.mark.asyncio
    async def test_manual_agent_id_is_respected_not_replaced(self, client) -> None:
        c, _store = client
        await _site()
        res = await c.post("/paw-bar/widgets", json=_create_payload(agent_id="agent-manual"))
        assert res.status_code == 201
        assert res.json()["agent_id"] == "agent-manual"

    @pytest.mark.asyncio
    async def test_create_without_site_stays_unbound(self, client) -> None:
        # No Site inserted for this pocket → provisioning is skipped silently.
        c, _store = client
        res = await c.post("/paw-bar/widgets", json=_create_payload(pocket_id="pocket-orphan"))
        assert res.status_code == 201
        assert res.json()["agent_id"] == ""

    @pytest.mark.asyncio
    async def test_idempotent_second_create_adopts_same_agent(self, client) -> None:
        c, _store = client
        await _site()
        first = (await c.post("/paw-bar/widgets", json=_create_payload())).json()
        second = (await c.post("/paw-bar/widgets", json=_create_payload())).json()
        # Deterministic slug → both widgets bind to the SAME dedicated agent.
        assert first["agent_id"] == second["agent_id"]

    @pytest.mark.asyncio
    async def test_provisioning_failure_is_soft_returns_unbound(self, client) -> None:
        from unittest.mock import patch

        c, _store = client
        await _site()
        with patch(
            "pocketpaw_ee.paw_bar.agent_provisioning.ensure_site_agent",
            side_effect=RuntimeError("boom"),
        ):
            res = await c.post("/paw-bar/widgets", json=_create_payload())
        # A provisioning error must NOT 500 the create — the widget returns unbound.
        assert res.status_code == 201
        assert res.json()["agent_id"] == ""


# --------------------------------------------------------------------------- #
# Concierge-enable trigger
# --------------------------------------------------------------------------- #


class TestConciergeEnableTrigger:
    @pytest.mark.asyncio
    async def test_enabling_provisions_unbound_widget(self, client) -> None:
        c, store = client
        site = await _site(concierge_enabled=False)
        widget = await store.create_widget(_widget(agent_id=""))

        res = await c.patch(
            f"/paw-bar/admin/site/{site.id}/settings",
            json={"concierge_enabled": True},
        )
        assert res.status_code == 200

        bound = await store.get_widget(widget.id, workspace_id=_WS)
        assert bound is not None and bound.agent_id, "enabling should provision + bind an agent"

    @pytest.mark.asyncio
    async def test_enabling_leaves_manual_bind_untouched(self, client) -> None:
        c, store = client
        site = await _site(concierge_enabled=False)
        widget = await store.create_widget(_widget(agent_id="agent-manual"))

        res = await c.patch(
            f"/paw-bar/admin/site/{site.id}/settings",
            json={"concierge_enabled": True},
        )
        assert res.status_code == 200
        bound = await store.get_widget(widget.id, workspace_id=_WS)
        assert bound is not None and bound.agent_id == "agent-manual"

    @pytest.mark.asyncio
    async def test_re_patch_enabled_on_already_enabled_site_provisions_unbound(
        self, client
    ) -> None:
        """The E2 one-click hook: a site that is ALREADY enabled with an UNBOUND
        widget still provisions on a re-PATCH of concierge_enabled=true (no
        false->true transition required)."""
        c, store = client
        site = await _site(concierge_enabled=True)  # already on
        widget = await store.create_widget(_widget(agent_id=""))

        res = await c.patch(
            f"/paw-bar/admin/site/{site.id}/settings",
            json={"concierge_enabled": True},
        )
        assert res.status_code == 200
        bound = await store.get_widget(widget.id, workspace_id=_WS)
        assert bound is not None and bound.agent_id, "re-enable should provision + bind"

    @pytest.mark.asyncio
    async def test_re_patch_enabled_on_bound_site_is_noop(self, client) -> None:
        """A re-PATCH of concierge_enabled=true on an already-BOUND site leaves the
        agent untouched (idempotent, unbound-only)."""
        c, store = client
        site = await _site(concierge_enabled=True)
        widget = await store.create_widget(_widget(agent_id="agent-manual"))

        res = await c.patch(
            f"/paw-bar/admin/site/{site.id}/settings",
            json={"concierge_enabled": True},
        )
        assert res.status_code == 200
        bound = await store.get_widget(widget.id, workspace_id=_WS)
        assert bound is not None and bound.agent_id == "agent-manual"


# --------------------------------------------------------------------------- #
# Regression — unbound chat still 409s (no fallback-to-universal)
# --------------------------------------------------------------------------- #


class TestUnboundChatStill409:
    @pytest.mark.asyncio
    async def test_unbound_widget_chat_is_409(self, client) -> None:
        c, store = client
        await _site()  # enabled, key resolves
        widget = await store.create_widget(_widget(agent_id=""))
        res = await c.post(
            "/paw-bar/chat",
            json={
                "widget_id": widget.id,
                "signed_key": _VALID_KEY,
                "customer_ref": _CUST,
                "message": "What time do you open?",
            },
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 409
        assert "no concierge agent" in res.text


# --------------------------------------------------------------------------- #
# ASG-1 identity fields absent on this branch + starters ride the frame config
# --------------------------------------------------------------------------- #


class TestIdentityAndFrameStarters:
    @pytest.mark.asyncio
    async def test_asg_identity_fields_absent_agent_has_neither(self, client) -> None:
        """welcome_message + conversation_starters are ASG-1 fields NOT present on
        this branch's Agent model — the provisioned agent carries neither, and the
        seeding path degraded gracefully (no crash, agent still created + bound)."""
        from pocketpaw_ee.cloud.agents import service as agents_service

        c, _store = client
        await _site(concierge_greeting="Welcome to Brew & Co!")
        agent_id = (await c.post("/paw-bar/widgets", json=_create_payload())).json()["agent_id"]
        agent = await agents_service.get(agent_id)
        assert not hasattr(agent.config, "welcome_message")
        assert not hasattr(agent.config, "conversation_starters")

    def test_frame_config_carries_starters_capped_four(self) -> None:
        from pocketpaw_ee.paw_bar.router import _pawbar_frame_config

        cfg = _pawbar_frame_config(
            site_key="k",
            widget_id="w",
            api_base="",
            parent_origin="",
            greeting="",
            starters=["a", "b", "c", "d", "e"],
        )
        assert cfg["starters"] == ["a", "b", "c", "d"]

    def test_frame_config_starters_default_empty(self) -> None:
        from pocketpaw_ee.paw_bar.router import _pawbar_frame_config

        cfg = _pawbar_frame_config(
            site_key="k", widget_id="w", api_base="", parent_origin="", greeting=""
        )
        assert cfg["starters"] == []

    @pytest.mark.asyncio
    async def test_public_frame_includes_starters_key(self, client) -> None:
        c, _store = client
        await _site()
        res = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
        assert res.status_code == 200
        assert "starters" in res.text
