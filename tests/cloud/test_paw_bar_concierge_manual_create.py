# tests/cloud/test_paw_bar_concierge_manual_create.py — a concierge exists only
# because its owner created it (CR-12, captain rule 2026-09-27).
#
# Created 2026-09-28 (feat/concierge-manual-create). Pins three things:
#   * NO TRIGGER CREATES ONE. Widget create, the enable PATCH, a first and a
#     second publish each leave the site with no agent and no
#     ``concierge_created_at`` marker. (Foreign attach and the empty-id rebind
#     are pinned in tests/cloud/sites/test_foreign_concierge_bind.py, beside the
#     fixtures that can buy a foreign concierge.)
#   * "NONE" READS AS "OFF" to a visitor: the frame is the dead shell, chat is a
#     403, and the embed snippet is "" — even with the switch on and a bound bar.
#   * THE EXPLICIT PAIR. ``POST /paw-bar/admin/site/{id}/concierge`` creates one
#     (off, legacy, widget minted with no default actions); ``DELETE`` removes it
#     and unbinds the agent without deleting it. Twice → 409, cross-tenant → 404,
#     a member without ``paw_bar.manage`` → 403.
#
# tests/mutations/concierge_manual_create.json restores each trigger in turn and
# names the test here that catches it.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import PawBarActionSpec, PawBarBlock, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

_WS = "ws-cr12"
_POCKET = "pocket-cr12"
_OWNER = "user:maya"
_VALID_KEY = "site_key_" + "c" * 24
_ORIGIN = "https://brewco.com"


def _spec(**ov: Any) -> PawBarSpec:
    d: dict[str, Any] = dict(
        widget_id="pp_seed",
        pocket_id=_POCKET,
        blocks=[PawBarBlock(type="text", content="Hi")],
    )
    d.update(ov)
    return PawBarSpec(**d)


def _widget(**ov: Any) -> PawBarWidget:
    d: dict[str, Any] = dict(
        pocket_id=_POCKET,
        owner=_OWNER,
        name="Brew & Co",
        spec=_spec(),
        allowed_domains=["brewco.com"],
        agent_id="",
        workspace_id=_WS,
    )
    d.update(ov)
    return PawBarWidget(**d)


async def _site(**ov: Any):
    from pocketpaw_ee.cloud.models.site import Site

    d: dict[str, Any] = dict(
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


async def _reload(site):
    from pocketpaw_ee.cloud.models.site import Site

    return await Site.find_one({"_id": site.id})


async def _concierge_agents() -> list[Any]:
    from pocketpaw_ee.cloud.models.agent import Agent

    docs = await Agent.find(Agent.workspace == _WS).to_list()
    return [d for d in docs if d.slug.startswith("concierge-")]


def _app(role: str = "admin", workspace_id: str = _WS) -> FastAPI:
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    from tests.cloud.conftest import override_workspace_role

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    override_workspace_role(app, role=role, workspace_id=workspace_id, user_id=_OWNER)
    return app


@pytest_asyncio.fixture
async def store(tmp_path, mongo_db):  # noqa: ARG001 — mongo_db initialises Beanie
    from unittest.mock import patch

    s = PawBarStore(tmp_path / "cr12.db")
    with patch("pocketpaw_ee.api.get_paw_bar_store", return_value=s):
        yield s


@pytest_asyncio.fixture
async def client(store):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        yield c


# --------------------------------------------------------------------------- #
# 1. No trigger creates a concierge
# --------------------------------------------------------------------------- #


class TestNoTriggerCreatesAConcierge:
    @pytest.mark.asyncio
    async def test_widget_create_on_a_site_pocket_creates_no_concierge(self, client) -> None:
        site = await _site()
        res = await client.post(
            "/paw-bar/widgets",
            json={
                "pocket_id": _POCKET,
                "owner": _OWNER,
                "name": "Brew & Co",
                "spec": _spec().model_dump(),
                "allowed_domains": ["brewco.com"],
            },
        )
        assert res.status_code == 201
        assert res.json()["agent_id"] == ""
        assert await _concierge_agents() == []
        assert (await _reload(site)).concierge_created_at is None

    @pytest.mark.asyncio
    async def test_the_enable_patch_creates_no_concierge(self, client, store) -> None:
        site = await _site()
        widget = await store.create_widget(_widget())

        res = await client.patch(
            f"/paw-bar/admin/site/{site.id}/settings", json={"concierge_enabled": True}
        )
        assert res.status_code == 200
        assert res.json()["concierge_exists"] is False
        after = await store.get_widget(widget.id, workspace_id=_WS)
        assert after is not None and after.agent_id == ""
        assert await _concierge_agents() == []
        assert (await _reload(site)).concierge_created_at is None

    @pytest.mark.asyncio
    async def test_a_first_publish_creates_no_concierge_and_embeds_no_bar(
        self, store, tmp_path
    ) -> None:
        from bson import ObjectId
        from pocketpaw_ee.sites import service as sites_service

        page = tmp_path / "index.html"
        page.write_text("<html><body>hi</body></html>", encoding="utf-8")

        # No Site doc yet: the first-publish state.
        await sites_service._embed_concierge_bar(
            workspace_id=_WS,
            pocket_id=_POCKET,
            site_id=str(ObjectId()),
            signed_key=_VALID_KEY,
            project_dir=str(tmp_path),
            engine="html",
        )

        assert await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=5) == []
        assert await _concierge_agents() == []
        assert "pawbar" not in page.read_text(encoding="utf-8").lower()

    @pytest.mark.asyncio
    async def test_a_second_publish_creates_no_concierge(self, store, tmp_path) -> None:
        from pocketpaw_ee.sites import service as sites_service

        site = await _site(concierge_enabled=True)
        await store.create_widget(_widget())
        page = tmp_path / "index.html"
        page.write_text("<html><body>hi</body></html>", encoding="utf-8")

        await sites_service._embed_concierge_bar(
            workspace_id=_WS,
            pocket_id=_POCKET,
            site_id=str(site.id),
            signed_key=_VALID_KEY,
            project_dir=str(tmp_path),
            engine="html",
        )

        widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=5)
        assert [w.agent_id for w in widgets] == [""]
        assert await _concierge_agents() == []
        assert (await _reload(site)).concierge_created_at is None
        # Switch on and a bar present: only the missing marker keeps it off the page.
        assert _VALID_KEY not in page.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_publish_of_a_created_concierge_embeds_its_bar(self, store, tmp_path) -> None:
        """The other half of the snippet gate: the marker, not an agent, earns the bar."""
        from pocketpaw_ee.sites import service as sites_service

        site = await _site(concierge_enabled=True, concierge_created_at=datetime.now(UTC))
        await store.create_widget(_widget(agent_id=""))
        page = tmp_path / "index.html"
        page.write_text("<html><body>hi</body></html>", encoding="utf-8")

        await sites_service._embed_concierge_bar(
            workspace_id=_WS,
            pocket_id=_POCKET,
            site_id=str(site.id),
            signed_key=_VALID_KEY,
            project_dir=str(tmp_path),
            engine="html",
        )
        assert _VALID_KEY in page.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 2. A site with no concierge shows visitors nothing
# --------------------------------------------------------------------------- #


class TestNoConciergeIsOff:
    @pytest.mark.asyncio
    async def test_the_frame_is_the_dead_shell(self, client, store) -> None:
        # Switch on, bar bound: only the missing marker says "none".
        await _site(concierge_enabled=True)
        widget = await store.create_widget(_widget(agent_id="agent-legacy"))

        res = await client.get(
            "/paw-bar/frame", params={"key": _VALID_KEY, "w": widget.id, "po": _ORIGIN}
        )
        assert res.status_code == 403
        assert "pawbar:dead" in res.text

    @pytest.mark.asyncio
    async def test_chat_is_refused_as_disabled(self, client, store) -> None:
        await _site(concierge_enabled=True)
        widget = await store.create_widget(_widget(agent_id="agent-legacy"))
        res = await client.post(
            "/paw-bar/chat",
            json={
                "widget_id": widget.id,
                "signed_key": _VALID_KEY,
                "customer_ref": "cust-0001",
                "message": "hello",
            },
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 403
        assert "concierge_disabled" in res.text

    @pytest.mark.asyncio
    async def test_the_snippet_is_empty(self, store) -> None:
        from pocketpaw_ee.paw_bar import embed

        await store.create_widget(_widget(agent_id="agent-legacy"))
        snippet = await embed.concierge_snippet(
            workspace_id=_WS,
            pocket_id=_POCKET,
            site_key=_VALID_KEY,
            api_base="https://api.example",
            concierge_enabled=True,
            concierge_entitled=True,
            concierge_exists=False,
        )
        assert snippet == ""

    @pytest.mark.asyncio
    async def test_the_settings_snippet_is_empty_without_a_marker(self, client, store) -> None:
        from unittest.mock import AsyncMock, patch

        site = await _site(concierge_enabled=True)
        await store.create_widget(_widget(agent_id="agent-legacy"))
        with patch("pocketpaw_ee.cloud.pockets.service.can_read", new=AsyncMock(return_value=True)):
            res = await client.get(f"/paw-bar/admin/site/{site.id}/settings")
        assert res.status_code == 200
        assert res.json()["concierge_exists"] is False
        assert res.json()["embed_snippet"] == ""

    def test_concierge_available_requires_the_marker(self) -> None:
        from pocketpaw_ee.cloud.auth.site_keys import concierge_available
        from pocketpaw_ee.cloud.models.site import Site

        on_but_none = Site(workspace=_WS, pocket_id=_POCKET, owner=_OWNER, concierge_enabled=True)
        created = Site(
            workspace=_WS,
            pocket_id=_POCKET,
            owner=_OWNER,
            concierge_enabled=True,
            concierge_created_at=datetime.now(UTC),
        )
        assert concierge_available(on_but_none) is False
        assert concierge_available(created) is True

    def test_a_new_site_defaults_to_off_with_no_concierge(self) -> None:
        from pocketpaw_ee.cloud.models.site import Site

        site = Site(workspace=_WS, pocket_id=_POCKET, owner=_OWNER)
        assert site.concierge_enabled is False
        assert site.concierge_created_at is None
        assert site.concierge_runtime == "legacy"


# --------------------------------------------------------------------------- #
# 3. The explicit create / delete pair
# --------------------------------------------------------------------------- #


class TestCreateAndDelete:
    @pytest.mark.asyncio
    async def test_create_then_delete_round_trips(self, client, store) -> None:
        site = await _site()

        res = await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["concierge_exists"] is True
        assert body["concierge_enabled"] is False, "a new concierge starts off"
        assert body["concierge_runtime"] == "legacy"

        stored = await _reload(site)
        assert stored.concierge_created_at is not None
        assert stored.concierge_enabled is False

        # Widget minted with no default actions and an empty catalog; the legacy
        # create binds its agent explicitly (owner-UX open question 1).
        widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=5)
        assert len(widgets) == 1
        assert widgets[0].spec.actions == []
        assert widgets[0].spec.catalog == []
        agent_id = widgets[0].agent_id
        assert agent_id
        assert [str(a.id) for a in await _concierge_agents()] == [agent_id]

        overview = await client.get(f"/paw-bar/admin/site/{site.id}/overview")
        assert overview.json()["concierge_exists"] is True
        assert overview.json()["concierge_runtime"] == "legacy"

        res = await client.delete(f"/paw-bar/admin/site/{site.id}/concierge")
        assert res.status_code == 200, res.text
        assert res.json()["concierge_exists"] is False
        assert res.json()["concierge_enabled"] is False

        stored = await _reload(site)
        assert stored.concierge_created_at is None
        after = await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=5)
        assert [w.agent_id for w in after] == [""], "the legacy agent is unbound"
        assert [str(a.id) for a in await _concierge_agents()] == [agent_id], "but never deleted"

        overview = await client.get(f"/paw-bar/admin/site/{site.id}/overview")
        assert overview.json()["concierge_exists"] is False

        # And it can be created again.
        again = await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
        assert again.status_code == 201

    @staticmethod
    def _gate(monkeypatch, answer: str) -> list[int]:
        """Install a stand-in for CR-6's ``concierge_gate`` (its own PR) that answers
        ``answer`` and records each call."""
        import sys
        import types

        calls: list[int] = []

        def default_concierge_runtime(settings=None) -> str:
            calls.append(1)
            return answer

        fake = types.ModuleType("pocketpaw_ee.paw_bar.concierge_gate")
        fake.default_concierge_runtime = default_concierge_runtime
        monkeypatch.setitem(sys.modules, "pocketpaw_ee.paw_bar.concierge_gate", fake)
        return calls

    @pytest.mark.asyncio
    async def test_an_open_gate_creates_a_v2_concierge_with_no_agent(
        self, client, store, monkeypatch
    ) -> None:
        calls = self._gate(monkeypatch, "v2")
        site = await _site()

        res = await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})

        assert res.status_code == 201, res.text
        assert calls == [1], "the gate is asked at create time"
        assert res.json()["concierge_runtime"] == "v2"
        assert (await _reload(site)).concierge_runtime == "v2"
        widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=5)
        assert [w.agent_id for w in widgets] == [""], "v2 needs no agent"
        assert await _concierge_agents() == []

    @pytest.mark.asyncio
    async def test_a_shut_gate_creates_a_legacy_concierge(self, client, monkeypatch) -> None:
        self._gate(monkeypatch, "legacy")
        site = await _site()

        res = await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})

        assert res.json()["concierge_runtime"] == "legacy"
        assert len(await _concierge_agents()) == 1

    @pytest.mark.asyncio
    async def test_an_unexpected_gate_answer_is_legacy(self, client, monkeypatch) -> None:
        self._gate(monkeypatch, "V2 ")
        site = await _site()

        res = await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})

        assert res.json()["concierge_runtime"] == "legacy"

    @pytest.mark.asyncio
    async def test_the_gate_never_touches_an_existing_concierge(self, client, monkeypatch) -> None:
        site = await _site(concierge_created_at=datetime.now(UTC), concierge_runtime="legacy")
        calls = self._gate(monkeypatch, "v2")

        res = await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})

        assert res.status_code == 409
        assert calls == []
        assert (await _reload(site)).concierge_runtime == "legacy"

    @pytest.mark.asyncio
    async def test_create_keeps_an_existing_widget_and_its_actions(self, client, store) -> None:
        site = await _site()
        actions = [PawBarActionSpec(verb="book_table", policy="gated", label="Book")]
        existing = await store.create_widget(_widget(spec=_spec(actions=actions)))

        res = await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
        assert res.status_code == 201
        widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=5)
        assert [w.id for w in widgets] == [existing.id]
        assert [a.verb for a in widgets[0].spec.actions] == ["book_table"]

    @pytest.mark.asyncio
    async def test_create_accepts_a_greeting(self, client) -> None:
        site = await _site()
        res = await client.post(
            f"/paw-bar/admin/site/{site.id}/concierge",
            json={"concierge_greeting": "Welcome in"},
        )
        assert res.status_code == 201
        assert res.json()["concierge_greeting"] == "Welcome in"

    @pytest.mark.asyncio
    async def test_creating_twice_is_a_409(self, client) -> None:
        site = await _site()
        assert (
            await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
        ).status_code == 201
        res = await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
        assert res.status_code == 409
        assert len(await _concierge_agents()) == 1

    @pytest.mark.asyncio
    async def test_deleting_none_is_a_404(self, client) -> None:
        site = await _site()
        res = await client.delete(f"/paw-bar/admin/site/{site.id}/concierge")
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_cross_tenant_create_and_delete_are_404(self, store) -> None:  # noqa: ARG002
        site = await _site()
        app = _app(workspace_id="ws-other")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            created = await c.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
            deleted = await c.delete(f"/paw-bar/admin/site/{site.id}/concierge")
        assert created.status_code == 404
        assert deleted.status_code == 404
        assert (await _reload(site)).concierge_created_at is None

    @pytest.mark.asyncio
    async def test_a_member_without_manage_is_403(self, store) -> None:  # noqa: ARG002
        site = await _site()
        app = _app(role="member")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            created = await c.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
            deleted = await c.delete(f"/paw-bar/admin/site/{site.id}/concierge")
        assert created.status_code == 403
        assert deleted.status_code == 403
        assert (await _reload(site)).concierge_created_at is None

    @pytest.mark.asyncio
    async def test_delete_clears_pinned_answers_through_the_cr8_hook(
        self, client, monkeypatch
    ) -> None:
        """CR-8 owns the FAQ store and exposes ``delete_faqs(site)``. It ships in
        its own PR, so a stand-in module is installed here; once CR-8 merges the
        real one is imported by the same line."""
        import sys
        import types

        cleared: list[str] = []

        async def delete_faqs(site) -> int:
            cleared.append(str(site.id))
            return 0

        fake = types.ModuleType("pocketpaw_ee.paw_bar.knowledge_routes")
        fake.delete_faqs = delete_faqs
        monkeypatch.setitem(sys.modules, "pocketpaw_ee.paw_bar.knowledge_routes", fake)

        site = await _site()
        await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
        res = await client.delete(f"/paw-bar/admin/site/{site.id}/concierge")

        assert res.status_code == 200
        assert cleared == [str(site.id)]

    @pytest.mark.asyncio
    async def test_delete_clears_uploaded_sources_through_the_cr9_hook(
        self, client, monkeypatch
    ) -> None:
        """CR-9 owns uploaded files and links and exposes ``delete_sources(site)``
        in the same module as CR-8's hook. The stand-in carries ONLY that name, so
        this also pins that each hook is looked up on its own: a module without
        ``delete_faqs`` must not stop the sources from being cleared."""
        import sys
        import types

        cleared: list[str] = []

        async def delete_sources(site) -> int:
            cleared.append(str(site.id))
            return 0

        fake = types.ModuleType("pocketpaw_ee.paw_bar.knowledge_routes")
        fake.delete_sources = delete_sources
        monkeypatch.setitem(sys.modules, "pocketpaw_ee.paw_bar.knowledge_routes", fake)

        site = await _site()
        await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
        res = await client.delete(f"/paw-bar/admin/site/{site.id}/concierge")

        assert res.status_code == 200
        assert cleared == [str(site.id)]

    @pytest.mark.asyncio
    async def test_delete_keeps_conversations_unless_asked(self, client, store) -> None:
        site = await _site()
        await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
        widget = (await store.list_widgets(pocket_id=_POCKET, workspace_id=_WS, limit=1))[0]
        await store.ensure_conversation(widget.id, "cust-0001", workspace_id=_WS)

        await client.delete(f"/paw-bar/admin/site/{site.id}/concierge")
        kept = await store.list_conversations(widget.id, workspace_id=_WS)
        assert len(kept) == 1

        await client.post(f"/paw-bar/admin/site/{site.id}/concierge", json={})
        res = await client.delete(
            f"/paw-bar/admin/site/{site.id}/concierge", params={"delete_conversations": "true"}
        )
        assert res.status_code == 200
        assert await store.list_conversations(widget.id, workspace_id=_WS) == []
