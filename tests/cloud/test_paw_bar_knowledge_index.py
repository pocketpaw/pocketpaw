# tests/cloud/test_paw_bar_knowledge_index.py — the owner's view of everything a
# site's concierge answers from: GET …/knowledge/index, GET/DELETE
# …/knowledge/article/{id}, and the agents knowledge read naming the bound site.
#
# Pins: the index lists every page in ``kb_page_index`` with its sections (old
# entries without crawl time or size still read), the pages the last sync left
# out and why, pocket articles no page or source holds, and the bound agent's
# own articles; the article read resolves its scope from the site and refuses an
# id that scope does not list; only an agent article can be removed one by one;
# tenancy is one 404 and the write needs the manage role.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.cloud.test_paw_bar_concierge_v2 import _site

_INDEX = "/paw-bar/admin/site/{sid}/knowledge/index"
_ARTICLE = "/paw-bar/admin/site/{sid}/knowledge/article/{aid}"


def _app(role: str) -> FastAPI:
    from pocketpaw_ee.paw_bar.knowledge_routes import router

    from tests.cloud.conftest import override_workspace_role

    app = FastAPI()
    app.include_router(router)
    override_workspace_role(app, role=role, workspace_id="ws-1")
    return app


@pytest_asyncio.fixture
async def owner(mongo_db):
    async with AsyncClient(transport=ASGITransport(app=_app("admin")), base_url="http://t") as c:
        yield c


@pytest_asyncio.fixture
async def member(mongo_db):
    async with AsyncClient(transport=ASGITransport(app=_app("member")), base_url="http://t") as c:
        yield c


class _Kb:
    """kb-go per scope: ``scopes`` maps a scope to its articles."""

    def __init__(self) -> None:
        self.scopes: dict[str, list[dict[str, Any]]] = {}
        self.shown: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []

    async def list(self, scope: str) -> list[dict]:
        return [
            {"id": a["id"], "title": a["title"], "word_count": a.get("words", 0)}
            for a in self.scopes.get(scope, [])
        ]

    async def show(self, scope: str, article_id: str) -> dict:
        self.shown.append((scope, article_id))
        for a in self.scopes.get(scope, []):
            if a["id"] == article_id:
                return {"id": a["id"], "title": a["title"], "summary": "", "content": a["body"]}
        raise RuntimeError("no such article")

    async def remove(self, scope: str, article_id: str) -> bool:
        self.removed.append((scope, article_id))
        return True


@pytest.fixture
def kb(monkeypatch) -> _Kb:
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    fake = _Kb()
    monkeypatch.setattr(KnowledgeService, "list_articles_for_scope", staticmethod(fake.list))
    monkeypatch.setattr(KnowledgeService, "get_article_for_scope", staticmethod(fake.show))
    monkeypatch.setattr(KnowledgeService, "remove_article", staticmethod(fake.remove))
    return fake


@pytest.fixture
def bound(monkeypatch):
    """Bind ``agent-xyz`` to the site's bar (or nothing, with ``bound(None)``)."""
    from pocketpaw_ee.paw_bar import agent_provisioning

    seen: list[tuple[str, str]] = []

    def _bind(agent_id: str | None = "agent-xyz") -> list[tuple[str, str]]:
        async def _widget(pocket_id: str, workspace_id: str):
            seen.append((pocket_id, workspace_id))
            return SimpleNamespace(agent_id=agent_id) if agent_id else None

        monkeypatch.setattr(agent_provisioning, "site_widget", _widget)
        return seen

    return _bind


async def _indexed_site(**ov: Any):
    from pocketpaw_ee.cloud.models.site import ConciergeKnowledgeSource

    now = datetime.now(UTC)
    d: dict[str, Any] = dict(
        kb_article_ids=["site-events-upcoming", "site-events-past", "site-home"],
        kb_synced_at=now,
        kb_page_index={
            "events": {
                "id": "site-events-upcoming",
                "title": "Events",
                "crawled_at": "2026-10-21T10:00:00+00:00",
                "chars": 5120,
                "sections": [
                    {
                        "id": "site-events-upcoming",
                        "title": "Events › Upcoming",
                        "source": "site-events#upcoming",
                        "hash": "h1",
                        "anchor": "upcoming",
                        "chars": 900,
                    },
                    {
                        "id": "site-events-past",
                        "title": "Events › Past",
                        "source": "site-events#past",
                        "hash": "h2",
                        "anchor": "past",
                        "chars": 400,
                    },
                ],
            },
            # Written before this change: no crawl time, size or sections.
            "": {"id": "site-home", "title": "Home"},
        },  # fmt: skip
        kb_crawl_report={
            "skips": [
                {"path": "about/index.html", "reason": "too_short", "detail": ""},
                {"path": "", "reason": "crawl", "detail": "skipped https://x/a — HTTP 404"},
            ],
            "page_cap": 25,
            "capped": True,
            "pages_failed": 1,
            "skipped_by_robots": 0,
        },
        concierge_sources=[
            ConciergeKnowledgeSource(
                id="src-1",
                kind="file",
                name="menu.pdf",
                status="ready",
                article_ids=["menu-part-1"],
                created_at=now,
                updated_at=now,
            )
        ],  # fmt: skip
    )
    d.update(ov)
    return await _site(**d)


@pytest.mark.asyncio
async def test_the_index_lists_pages_skips_and_every_other_article(owner, kb, bound):
    seen = bound()
    site = await _indexed_site()
    kb.scopes["pocket:pocket-1"] = [
        {"id": "site-home", "title": "Home"},
        {"id": "menu-part-1", "title": "menu.pdf part 1"},
        {"id": "flyer", "title": "Flyer", "words": 80},
    ]
    kb.scopes["agent:agent-xyz"] = [{"id": "faq-doc", "title": "Old FAQ", "words": 300}]

    r = await owner.get(_INDEX.format(sid=site.id))

    assert r.status_code == 200, r.text
    body = r.json()
    assert seen == [("pocket-1", "ws-1")]
    assert (body["page_count"], body["article_count"]) == (2, 3)
    assert (body["page_cap"], body["capped"], body["pages_failed"]) == (25, True, 1)
    home, events = body["pages"]
    assert events["path"] == "/events"
    assert events["crawled_at"] == "2026-10-21T10:00:00+00:00"
    assert events["chars"] == 5120
    assert [(s["id"], s["anchor"], s["chars"]) for s in events["sections"]] == [
        ("site-events-upcoming", "upcoming", 900),
        ("site-events-past", "past", 400),
    ]
    # An entry from before this change still reads, on the sync's time.
    assert home["path"] == "/"
    assert home["chars"] is None
    assert home["crawled_at"] == body["synced_at"]
    assert [s["id"] for s in home["sections"]] == ["site-home"]
    assert body["skipped"] == [
        {"path": "/about", "reason": "too_short", "detail": ""},
        {"path": "", "reason": "crawl", "detail": "skipped https://x/a — HTTP 404"},
    ]
    # Only what no page or source holds counts as "other".
    assert body["other_articles"] == [{"id": "flyer", "title": "Flyer", "words": 80}]
    assert body["agent_id"] == "agent-xyz"
    assert body["agent_articles"] == [{"id": "faq-doc", "title": "Old FAQ", "words": 300}]


@pytest.mark.asyncio
async def test_a_never_synced_site_with_no_agent_reads_empty(owner, kb, bound):
    bound(None)
    site = await _site()

    body = (await owner.get(_INDEX.format(sid=site.id))).json()

    assert body["status"] == "never_synced"
    assert body["pages"] == [] and body["skipped"] == []
    assert body["agent_id"] == "" and body["agent_articles"] == []


@pytest.mark.asyncio
async def test_a_kb_outage_degrades_the_index_instead_of_failing_it(owner, kb, bound, monkeypatch):
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    async def _down(scope: str) -> list:
        raise RuntimeError("kb binary missing")

    monkeypatch.setattr(KnowledgeService, "list_articles_for_scope", staticmethod(_down))
    bound()
    site = await _indexed_site()

    r = await owner.get(_INDEX.format(sid=site.id))

    assert r.status_code == 200
    assert r.json()["page_count"] == 2
    assert r.json()["other_articles"] == [] and r.json()["agent_articles"] == []


@pytest.mark.asyncio
async def test_an_article_reads_its_compiled_text_from_the_sites_own_scopes(owner, kb, bound):
    bound()
    site = await _indexed_site()
    kb.scopes["pocket:pocket-1"] = [{"id": "site-home", "title": "Home", "body": "Open 8-6."}]
    kb.scopes["agent:agent-xyz"] = [{"id": "faq-doc", "title": "FAQ", "body": "No dogs."}]

    page = await owner.get(_ARTICLE.format(sid=site.id, aid="site-home"))
    agent = await owner.get(_ARTICLE.format(sid=site.id, aid="faq-doc") + "?scope=agent")

    assert page.status_code == 200 and agent.status_code == 200
    assert page.json()["content"] == "Open 8-6." and page.json()["scope"] == "pocket"
    assert agent.json()["content"] == "No dogs." and agent.json()["scope"] == "agent"
    assert kb.shown == [("pocket:pocket-1", "site-home"), ("agent:agent-xyz", "faq-doc")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aid", "scope"),
    [
        ("faq-doc", "pocket"),  # an agent article asked for in the pocket
        ("site-home", "agent"),  # a pocket article asked for on the agent
        ("--scope", "pocket"),  # shaped like a kb flag
    ],
)
async def test_an_id_the_scope_does_not_list_is_never_read(owner, kb, bound, aid, scope):
    bound()
    site = await _indexed_site()
    kb.scopes["pocket:pocket-1"] = [{"id": "site-home", "title": "Home", "body": "x"}]
    kb.scopes["agent:agent-xyz"] = [{"id": "faq-doc", "title": "FAQ", "body": "y"}]

    r = await owner.get(_ARTICLE.format(sid=site.id, aid=aid) + f"?scope={scope}")

    assert r.status_code == 404
    assert kb.shown == []


@pytest.mark.asyncio
async def test_the_agent_scope_needs_a_bound_agent(owner, kb, bound):
    bound(None)
    site = await _indexed_site()

    r = await owner.get(_ARTICLE.format(sid=site.id, aid="faq-doc") + "?scope=agent")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_an_agent_article_can_be_removed_and_a_pocket_one_cannot(owner, kb, bound):
    bound()
    site = await _indexed_site()
    kb.scopes["agent:agent-xyz"] = [{"id": "faq-doc", "title": "FAQ", "body": "y"}]

    gone = await owner.delete(_ARTICLE.format(sid=site.id, aid="faq-doc") + "?scope=agent")
    page = await owner.delete(_ARTICLE.format(sid=site.id, aid="site-home") + "?scope=pocket")

    assert gone.status_code == 204
    assert page.status_code == 409
    assert kb.removed == [("agent:agent-xyz", "faq-doc")]


@pytest.mark.asyncio
async def test_a_foreign_site_is_one_404_and_a_member_cannot_remove(owner, member, kb, bound):
    bound()
    foreign = await _indexed_site(workspace="ws-2", pocket_id="pocket-2")
    mine = await _indexed_site(pocket_id="pocket-3")
    kb.scopes["agent:agent-xyz"] = [{"id": "faq-doc", "title": "FAQ", "body": "y"}]

    assert (await owner.get(_INDEX.format(sid=foreign.id))).status_code == 404
    assert (await owner.get(_INDEX.format(sid="not-an-id"))).status_code == 404
    denied = await member.delete(_ARTICLE.format(sid=mine.id, aid="faq-doc") + "?scope=agent")
    assert denied.status_code == 403
    assert kb.removed == []


@pytest.mark.asyncio
async def test_the_agent_knowledge_read_names_the_bound_site(mongo_db, monkeypatch):
    """The agent editor shows the site's knowledge when its agent fronts a site."""
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.paw_bar import agent_provisioning

    site = await _site(foreign_origin=True)

    async def _ws(agent_id: str) -> str:
        return "ws-1"

    async def _widget_for(agent_id: str, workspace_id: str):
        return SimpleNamespace(pocket_id="pocket-1") if agent_id == "agent-xyz" else None

    monkeypatch.setattr(agents_service, "get_workspace", _ws)
    monkeypatch.setattr(agent_provisioning, "widget_for_agent", _widget_for)

    assert await agents_service.bound_site("agent-xyz") == {
        "site_id": str(site.id),
        "foreign": True,
    }
    assert await agents_service.bound_site("agent-other") == {"site_id": "", "foreign": False}
