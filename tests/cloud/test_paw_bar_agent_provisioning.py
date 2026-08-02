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
#   * Default booking action (2026-08-01 live regression): a widget MINTED by
#     ensure_site_widget carries one gated booking_request action (five str
#     args, "Book a service visit" label); an EXISTING widget's actions are
#     never modified by any ensure_site_widget pass (mint-only).

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


class TestPublishTimeTrigger:
    """``ensure_site_widget`` — the third trigger (2026-07-30 regression).

    A site created and published by the agent in ONE conversation goes through
    neither widget-create nor a concierge-enable transition, so before this
    trigger the publish-time embed found no widget and silently shipped the
    site bar-less with no dedicated agent.
    """

    @pytest.mark.asyncio
    async def test_publish_provisioning_mints_widget_and_agent(self, client) -> None:
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        _c, store = client
        site = await _site()

        # No widget exists for the pocket (the agent-created-site shape).
        assert await ap.site_widget(_POCKET, _WS) is None

        widget = await ap.ensure_site_widget(site, _WS)
        assert widget is not None
        assert widget.pocket_id == _POCKET
        assert widget.workspace_id == _WS
        assert widget.agent_id, "minted widget must be bound to a dedicated agent"

        # Idempotent: a second call returns the SAME widget, not a sibling.
        again = await ap.ensure_site_widget(site, _WS)
        assert again is not None and again.id == widget.id
        widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=10)
        assert len(widgets) == 1

    @pytest.mark.asyncio
    async def test_publish_provisioning_binds_existing_unbound_widget(self, client) -> None:
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        c, _store = client
        site = await _site()
        # An unbound widget exists (agent deleted / legacy row): reuse, don't mint.
        res = await c.post("/paw-bar/widgets", json=_create_payload(agent_id="agent-manual"))
        existing_id = res.json()["id"]

        widget = await ap.ensure_site_widget(site, _WS)
        assert widget is not None and widget.id == existing_id

    @pytest.mark.asyncio
    async def test_minted_widget_carries_default_booking_action(self, client) -> None:
        """A MINTED widget must declare the default gated booking action.

        Live regression (2026-08-01, hosted deploy): the minted spec shipped with
        ``actions=[]``, the concierge preamble rendered no form-card instructions
        (widgets with no gated-with-args actions render none), and every
        from-scratch published site got a concierge that could answer questions
        but declined every booking request.
        """
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        _c, _store = client
        site = await _site()

        widget = await ap.ensure_site_widget(site, _WS)
        assert widget is not None
        actions = widget.spec.actions
        assert len(actions) == 1, "a minted widget must carry exactly the default action"
        action = actions[0]
        assert action.verb == "booking_request"
        assert action.policy == "gated"
        assert action.args == {
            "name": "str",
            "phone": "str",
            "address": "str",
            "issue": "str",
            "preferred_window": "str",
        }
        assert action.label == "Book a service visit"

    @pytest.mark.asyncio
    async def test_existing_widget_actions_are_never_modified(self, client) -> None:
        """The default is mint-only: an EXISTING widget's actions stay untouched.

        An owner may have deliberately removed or customized the actions, so a
        re-publish (a second ``ensure_site_widget`` pass, bound or unbound) must
        never re-add or reshape them.
        """
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        _c, store = client
        site = await _site()
        # Owner stripped the actions (spec has none) and the widget is unbound —
        # the pass that DOES bind an agent must still leave actions alone.
        existing = await store.create_widget(_widget(agent_id="", spec=_spec()))

        widget = await ap.ensure_site_widget(site, _WS)
        assert widget is not None and widget.id == existing.id
        assert widget.agent_id, "the unbound existing widget gains an agent"
        assert widget.spec.actions == [], "but its actions are never touched"

        # Second pass on the now-bound widget (the idempotent early return).
        again = await ap.ensure_site_widget(site, _WS)
        assert again is not None and again.id == existing.id
        assert again.spec.actions == []


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


# --------------------------------------------------------------------------- #
# Paw Bar inbox D5 — the "visible to site visitors" signal
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def bar_store(tmp_path, mongo_db):
    """A tmp paw-bar store patched in at the source (no HTTP app), plus Beanie for
    the Agent docs ``is_visible_to_site_visitors`` resolves the workspace from."""
    from unittest.mock import patch

    store = PawBarStore(tmp_path / "visibility.db")
    with patch("pocketpaw_ee.api.get_paw_bar_store", return_value=store):
        yield store


async def _agent_doc(**ov: Any):
    from pocketpaw_ee.cloud.models.agent import Agent

    d: dict[str, Any] = dict(
        workspace=_WS, name="Brew & Co Concierge", slug="concierge-site-1", owner=_OWNER
    )
    d.update(ov)
    agent = Agent(**d)
    await agent.insert()
    return agent


class TestSiteVisitorVisibility:
    """D5 consequence 1: a concierge run reads its own ``agent:<id>`` scope, so the
    agent's Knowledge surface must SAY that anything on it is publishable. The
    machine-readable half of that badge is ``visible_to_site_visitors`` on
    ``GET /agents/{id}/knowledge``, derived from the real widget→agent binding."""

    @pytest.mark.asyncio
    async def test_widget_for_agent_finds_the_bound_widget(self, bar_store) -> None:
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        widget = await bar_store.create_widget(_widget(agent_id="agent-bound"))
        assert (await ap.widget_for_agent("agent-bound", _WS)).id == widget.id

    @pytest.mark.asyncio
    async def test_widget_for_agent_ignores_an_unbound_or_sibling_widget(self, bar_store) -> None:
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        await bar_store.create_widget(_widget(agent_id=""))
        await bar_store.create_widget(_widget(pocket_id="pocket-2", agent_id="agent-other"))
        assert await ap.widget_for_agent("agent-bound", _WS) is None

    @pytest.mark.asyncio
    async def test_widget_for_agent_is_workspace_scoped(self, bar_store) -> None:
        """Another tenant's bar never answers for this workspace's agent."""
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        await bar_store.create_widget(_widget(agent_id="agent-bound", workspace_id="ws-other"))
        assert await ap.widget_for_agent("agent-bound", _WS) is None
        assert await ap.widget_for_agent("agent-bound", "ws-other") is not None

    @pytest.mark.asyncio
    async def test_widget_for_agent_refuses_an_unscoped_lookup(self, bar_store) -> None:
        """An empty agent id or workspace returns None instead of scanning — an
        unscoped list would hand back a sibling tenant's bar."""
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        await bar_store.create_widget(_widget(agent_id="agent-bound"))
        assert await ap.widget_for_agent("", _WS) is None
        assert await ap.widget_for_agent("agent-bound", "") is None

    @pytest.mark.asyncio
    async def test_site_bound_agent_is_flagged_visible(self, bar_store) -> None:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await _agent_doc()
        await bar_store.create_widget(_widget(agent_id=str(agent.id)))
        assert await agents_service.is_visible_to_site_visitors(str(agent.id)) is True

    @pytest.mark.asyncio
    async def test_internal_agent_is_not_flagged_visible(self, bar_store) -> None:
        """An ordinary workspace agent fronts no bar — no badge, no false alarm."""
        from pocketpaw_ee.cloud.agents import service as agents_service

        internal = await _agent_doc(slug="hr-assistant", name="HR Assistant")
        await bar_store.create_widget(_widget(agent_id="someone-else"))
        assert await agents_service.is_visible_to_site_visitors(str(internal.id)) is False

    @pytest.mark.asyncio
    async def test_visibility_of_an_unknown_agent_is_false(self, bar_store) -> None:
        """A missing / malformed agent id resolves no workspace, so there is
        nothing to scan and nothing to claim."""
        from pocketpaw_ee.cloud.agents import service as agents_service

        assert await agents_service.is_visible_to_site_visitors("") is False
        assert await agents_service.is_visible_to_site_visitors("not-an-object-id") is False

    @pytest.mark.asyncio
    async def test_visibility_is_failure_soft(self, bar_store) -> None:
        """The flag labels a surface, it does not guard one — an unreadable store
        yields False rather than 500-ing the owner's Knowledge tab."""
        from unittest.mock import patch

        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await _agent_doc()
        await bar_store.create_widget(_widget(agent_id=str(agent.id)))
        with patch(
            "pocketpaw_ee.paw_bar.agent_provisioning.widget_for_agent",
            side_effect=RuntimeError("store is gone"),
        ):
            assert await agents_service.is_visible_to_site_visitors(str(agent.id)) is False

    @pytest.mark.asyncio
    async def test_knowledge_read_carries_the_flag(self, bar_store) -> None:
        """The wire shape the frontend badge reads: ``GET /agents/{id}/knowledge``
        returns ``visible_to_site_visitors`` beside ``items``."""
        from unittest.mock import AsyncMock, patch

        from pocketpaw_ee.cloud.agents.router import list_knowledge

        agent = await _agent_doc()
        await bar_store.create_widget(_widget(agent_id=str(agent.id)))
        internal = await _agent_doc(slug="hr-assistant", name="HR Assistant")

        with patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.list_articles",
            new=AsyncMock(return_value=[{"id": "a1", "title": "Hours"}]),
        ):
            public = await list_knowledge(str(agent.id))
            private = await list_knowledge(str(internal.id))

        assert public["visible_to_site_visitors"] is True
        assert public["items"] == [{"id": "a1", "title": "Hours"}]
        assert private["visible_to_site_visitors"] is False


class TestFirstPublishProvisioning:
    """The FIRST publish must provision too (audit finding, 2026-07-30).

    ``_embed_concierge_bar`` guarded provisioning on ``doc is not None``, but on
    a first publish the Site doc is inserted AFTER the embed step — so ``doc``
    was None, provisioning was skipped, the four-gate snippet check returned ""
    and the page shipped bar-less with no log line at all. A re-publish (doc now
    present) grew a bar, which is precisely why this read as working. These
    tests pin the transient-doc path used when no Site doc exists yet.
    """

    @pytest.mark.asyncio
    async def test_transient_doc_provisions_widget_and_agent(self, client) -> None:
        from bson import ObjectId
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        _c, store = client
        # No Site doc in the DB at all — the first-publish state.
        site_id = ObjectId()
        transient = Site(
            id=site_id,
            workspace=_WS,
            pocket_id=_POCKET,
            owner=_OWNER,
            name="Northwind Plumbing",
            signed_key=_VALID_KEY,
        )

        widget = await ap.ensure_site_widget(transient, _WS)

        assert widget is not None, "a first publish must still mint the widget"
        assert widget.agent_id, "and bind it to a dedicated agent"
        assert widget.pocket_id == _POCKET
        # The agent is named off the transient doc, not a DB re-read.
        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await agents_service.get(widget.agent_id)
        assert agent.name == "Northwind Plumbing Concierge"
        assert agent.slug == f"concierge-{site_id}"

    @pytest.mark.asyncio
    async def test_second_publish_adopts_the_first_publish_widget(self, client) -> None:
        """Idempotent across the first→second publish boundary: the real doc
        must adopt what the transient pass created, never mint a sibling."""
        from bson import ObjectId
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.paw_bar import agent_provisioning as ap

        _c, store = client
        site_id = ObjectId()
        transient = Site(
            id=site_id,
            workspace=_WS,
            pocket_id=_POCKET,
            owner=_OWNER,
            name="Northwind Plumbing",
            signed_key=_VALID_KEY,
        )
        first = await ap.ensure_site_widget(transient, _WS)

        # Now the doc really exists (the publish inserted it) and we publish again.
        await transient.insert()
        second = await ap.ensure_site_widget(transient, _WS)

        assert second is not None and first is not None
        assert second.id == first.id
        widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=10)
        assert len(widgets) == 1, "a second publish must not mint a sibling widget"
