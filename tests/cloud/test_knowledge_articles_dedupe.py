# test_knowledge_articles_dedupe.py — GET /knowledge/articles?exclude_upload_derived.
#
# Created: 2026-08-31 (FX-kb "one document, one row"). The Files panel merges
# GET /files with GET /knowledge/articles on the client, so an uploaded PDF and
# the kb-go article compiled out of it render as two separate documents. The
# route can now be asked to drop the derived twin.
#
# These tests are written against the failure mode that matters, which is NOT
# "the duplicate survives". It is OVER-suppression: a filter that hides a
# little too much makes knowledge disappear from the Library with no error
# anywhere — it reads as "switched off", not "broken". So every negative case
# here (standalone article, hidden file, deleted file, pocket file, a same
# slug in a second scope) carries a POSITIVE CONTROL in the same response: an
# article that IS dropped. Without the control a fixture that never reaches
# the suppression branch at all would pass every assertion for the wrong
# reason.
#
# Two layers, deliberately:
#   * the matching rule as a pure function, where the (id, scope) pair can be
#     driven directly;
#   * the route end-to-end against a real Mongo shape (mongomock + beanie), so
#     the deleted/hidden/pocket filters are the ones ``list_by_kb_articles``
#     actually issues rather than a fake's approximation of them.
"""The Files panel can ask for KB rows minus their upload twins."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pocketpaw_ee.cloud.kb.knowledge_router as knowledge_router_module
import pytest
import pytest_asyncio
from beanie import init_beanie
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.kb.knowledge_router import _drop_upload_derived
from pocketpaw_ee.cloud.kb.workspace_aggregator import AggregatedArticle
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.uploads.models import FileUpload
from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

from pocketpaw.uploads.file_store import FileRecord

WORKSPACE = "ws-alpha"
WS_SCOPE = f"workspace:{WORKSPACE}"
AGENT_SCOPE = "agent:agent-1"


# ---------------------------------------------------------------------------
# The matching rule, on its own
# ---------------------------------------------------------------------------


def _article(article_id: str, scope: str) -> AggregatedArticle:
    return AggregatedArticle(
        id=article_id,
        title=article_id,
        source="",
        scope=scope,
        agent_id=None,
        updated_at="2026-08-30T10:00:00Z",
    )


def _claim(article_id: str, scope: str | None) -> SimpleNamespace:
    """Stand-in for a ``KbTrackedRecord``. Only ``article_id`` and ``scope``
    take part in matching; ``record`` is what the caller would render, and this
    filter never renders anything."""
    return SimpleNamespace(article_id=article_id, scope=scope, record=None)


def test_no_claims_means_no_articles_are_touched() -> None:
    articles = [_article("a", WS_SCOPE), _article("b", AGENT_SCOPE)]
    assert _drop_upload_derived(articles, []) == articles


def test_a_claim_drops_the_article_it_names() -> None:
    articles = [_article("book", WS_SCOPE), _article("chat-note", WS_SCOPE)]
    kept = _drop_upload_derived(articles, [_claim("book", WS_SCOPE)])
    assert [a.id for a in kept] == ["chat-note"]


def test_the_same_slug_in_another_scope_is_not_collateral() -> None:
    """kb-go slugs an article id off its title, so ``hello`` can exist in a
    workspace scope and an agent scope at the same time. Matching on the id
    alone would delete the agent's standalone article because somebody
    uploaded a same-named file to the workspace.

    Mutation that must break this: drop ``a.scope`` from the key so the
    comparison is ``a.id not in claimed_ids``.
    """
    articles = [_article("hello", WS_SCOPE), _article("hello", AGENT_SCOPE)]
    kept = _drop_upload_derived(articles, [_claim("hello", WS_SCOPE)])
    assert [(a.id, a.scope) for a in kept] == [("hello", AGENT_SCOPE)]


def test_a_claim_that_names_no_scope_matches_any_scope() -> None:
    """A row that recorded an article but not where it went. Read as a
    wildcard, matching the two precedents that already exist for a null
    ``kb_scope`` — ``has_article`` in this router and ``content_search``'s
    candidate narrowing. Close to unreachable in practice: ``kb_article_id``
    and ``kb_scope`` were one field-add and ``set_kb_article`` writes both.
    """
    articles = [_article("hello", WS_SCOPE), _article("hello", AGENT_SCOPE)]
    assert _drop_upload_derived(articles, [_claim("hello", None)]) == []


def test_a_claim_for_an_article_nobody_listed_changes_nothing() -> None:
    """An upload whose article was deleted out of kb, or lives in a scope this
    listing does not aggregate. It must not shift the rows that ARE here."""
    articles = [_article("chat-note", WS_SCOPE)]
    kept = _drop_upload_derived(articles, [_claim("gone-from-kb", WS_SCOPE)])
    assert [a.id for a in kept] == ["chat-note"]


# ---------------------------------------------------------------------------
# The route, against real Mongo shapes
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def beanie_db():
    db_name = f"test_kb_dedupe_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]
    await init_beanie(database=db, document_models=[FileUpload])
    yield db


async def _seed_upload(
    *,
    name: str,
    article_id: str | None = None,
    scope: str | None = None,
    pocket_id: str | None = None,
    hide_from_ai: bool = False,
    deleted: bool = False,
) -> str:
    """One upload row, optionally claiming *article_id* in *scope*."""
    store = MongoFileStore()
    rec = FileRecord(
        id=uuid.uuid4().hex,
        storage_key=f"keys/{uuid.uuid4().hex}",
        filename=name,
        mime="application/pdf",
        size=1_100_000,
        owner_id="user-1",
        chat_id=None,
        created=datetime.now(UTC),
    )
    await store.save_scoped(rec, workspace=WORKSPACE, pocket_id=pocket_id)
    if article_id:
        await store.set_kb_article(rec.id, WORKSPACE, article_id=article_id, scope=scope)
    if hide_from_ai:
        await store.set_library_metadata(rec.id, WORKSPACE, hide_from_ai=True)
    if deleted:
        await store.soft_delete_scoped(rec.id, WORKSPACE)
    return rec.id


def _build_client(monkeypatch, rows_by_scope: dict[str, list[dict]]) -> TestClient:
    """Mount the knowledge router with kb-go and RBAC faked.

    Same shape as ``tests/cloud/test_knowledge_router.py``'s fixture; the kb
    rows are a per-test parameter here because these cases turn on which
    scopes hold which slugs.
    """

    def fake_kb_list(scope: str) -> list[dict]:
        return rows_by_scope.get(scope, [])

    async def fake_agent_ids(workspace_id: str) -> list[str]:
        return ["agent-1"] if workspace_id == WORKSPACE else []

    monkeypatch.setattr(knowledge_router_module, "_call_kb_list", fake_kb_list)
    monkeypatch.setattr(knowledge_router_module, "_list_workspace_agent_ids", fake_agent_ids)

    from pocketpaw_ee.guards import deps as guards_deps

    monkeypatch.setattr(guards_deps, "check_workspace_action", lambda *a, **k: None)

    fake_user = SimpleNamespace(
        id="user-1",
        active_workspace=WORKSPACE,
        workspaces=[SimpleNamespace(workspace=WORKSPACE, role="owner")],
    )

    async def fake_current_active_user():
        return fake_user

    from pocketpaw_ee.cloud._core.http import add_error_handler

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = fake_current_active_user
    app.include_router(knowledge_router_module.router, prefix="/api/v1")
    return TestClient(app)


def _row(article_id: str, title: str) -> dict:
    return {"id": article_id, "title": title, "updated_at": "2026-08-30T10:00:00Z"}


BOOK = "the-art-of-public-speaking-esenwein-carnagey-1915"
NOTE = "i-beheld-a-godless-marrow"


@pytest.mark.asyncio
async def test_the_upload_twin_is_dropped_and_the_standalone_article_is_not(
    beanie_db, monkeypatch
) -> None:
    """The whole feature in one request.

    ``BOOK`` was compiled out of a 1.1 MB PDF that GET /files already lists —
    two rows, one document. ``NOTE`` was ingested from chat and has no file
    behind it; it is the only copy of that content in the panel and must
    survive. If this test ever passes with NOTE missing, the feature has
    become a knowledge shredder.
    """
    await _seed_upload(name="public-speaking.pdf", article_id=BOOK, scope=WS_SCOPE)
    client = _build_client(
        monkeypatch, {WS_SCOPE: [_row(BOOK, "The Art of Public Speaking"), _row(NOTE, "A note")]}
    )

    body = client.get("/api/v1/knowledge/articles?exclude_upload_derived=true").json()

    assert [a["id"] for a in body["articles"]] == [NOTE]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_without_the_flag_the_listing_is_unchanged(beanie_db, monkeypatch) -> None:
    """The default path pins the wiki. /knowledge, /knowledge-lab and the
    command palette all read this route, and there the compiled article IS the
    thing being read and repaired — suppressing for them would make
    upload-derived knowledge vanish from the wiki.

    The claim below is live and would suppress; only the absent flag stops it.
    """
    await _seed_upload(name="public-speaking.pdf", article_id=BOOK, scope=WS_SCOPE)
    client = _build_client(
        monkeypatch, {WS_SCOPE: [_row(BOOK, "The Art of Public Speaking"), _row(NOTE, "A note")]}
    )

    default = client.get("/api/v1/knowledge/articles").json()
    explicit_off = client.get("/api/v1/knowledge/articles?exclude_upload_derived=false").json()

    assert {a["id"] for a in default["articles"]} == {BOOK, NOTE}
    assert default["total"] == 2
    assert default == explicit_off


@pytest.mark.asyncio
async def test_a_file_hidden_from_ai_does_not_suppress_its_article(beanie_db, monkeypatch) -> None:
    """Hiding a file from AI takes it out of the panel's AI reach; the article
    is then the only surviving copy of that content, so it must stay listed.

    The control is the point: ``BOOK`` is dropped in this same response, which
    proves the request really did run the suppression branch and that ``NOTE``
    survived on its merits rather than because the filter never ran.
    """
    await _seed_upload(name="public-speaking.pdf", article_id=BOOK, scope=WS_SCOPE)
    await _seed_upload(name="hidden.pdf", article_id=NOTE, scope=WS_SCOPE, hide_from_ai=True)
    client = _build_client(monkeypatch, {WS_SCOPE: [_row(BOOK, "Book"), _row(NOTE, "Hidden")]})

    body = client.get("/api/v1/knowledge/articles?exclude_upload_derived=true").json()

    assert [a["id"] for a in body["articles"]] == [NOTE]


@pytest.mark.asyncio
async def test_a_soft_deleted_file_does_not_suppress_its_article(beanie_db, monkeypatch) -> None:
    """A deleted upload is gone from the panel. Letting its tracking row keep
    suppressing would delete the article from the Library too — the file is
    gone AND the knowledge it produced is gone, from one delete."""
    await _seed_upload(name="public-speaking.pdf", article_id=BOOK, scope=WS_SCOPE)
    await _seed_upload(name="deleted.pdf", article_id=NOTE, scope=WS_SCOPE, deleted=True)
    client = _build_client(monkeypatch, {WS_SCOPE: [_row(BOOK, "Book"), _row(NOTE, "Deleted")]})

    body = client.get("/api/v1/knowledge/articles?exclude_upload_derived=true").json()

    assert [a["id"] for a in body["articles"]] == [NOTE]


@pytest.mark.asyncio
async def test_a_pocket_upload_does_not_suppress_a_workspace_article(
    beanie_db, monkeypatch
) -> None:
    """A pocket-scoped upload is not listed on the workspace panel, so it is
    not the twin of anything here. Its article would be the only copy."""
    await _seed_upload(name="public-speaking.pdf", article_id=BOOK, scope=WS_SCOPE)
    await _seed_upload(name="pocket.pdf", article_id=NOTE, scope=WS_SCOPE, pocket_id="P")
    client = _build_client(monkeypatch, {WS_SCOPE: [_row(BOOK, "Book"), _row(NOTE, "Pocket")]})

    body = client.get("/api/v1/knowledge/articles?exclude_upload_derived=true").json()

    assert [a["id"] for a in body["articles"]] == [NOTE]


@pytest.mark.asyncio
async def test_an_agent_article_sharing_a_slug_with_an_upload_survives(
    beanie_db, monkeypatch
) -> None:
    """``hello`` in two scopes, one of them claimed. End-to-end version of the
    pure-function case, so the scope actually written to Mongo by
    ``set_kb_article`` is the one being compared."""
    await _seed_upload(name="hello.txt", article_id="hello", scope=WS_SCOPE)
    client = _build_client(
        monkeypatch,
        {WS_SCOPE: [_row("hello", "Hello")], AGENT_SCOPE: [_row("hello", "Hello")]},
    )

    body = client.get("/api/v1/knowledge/articles?exclude_upload_derived=true").json()

    assert [(a["id"], a["scope"]) for a in body["articles"]] == [("hello", AGENT_SCOPE)]


@pytest.mark.asyncio
async def test_totals_describe_the_filtered_set(beanie_db, monkeypatch) -> None:
    """Suppression runs BEFORE the offset slice. If it ran after, ``total``
    would count rows the caller can never reach and ``has_more`` would offer a
    page that comes back short."""
    await _seed_upload(name="public-speaking.pdf", article_id=BOOK, scope=WS_SCOPE)
    client = _build_client(
        monkeypatch,
        {WS_SCOPE: [_row(BOOK, "Book"), _row(NOTE, "Note"), _row("third", "Third")]},
    )

    body = client.get(
        "/api/v1/knowledge/articles?exclude_upload_derived=true&limit=2&offset=0"
    ).json()

    assert body["total"] == 2
    assert body["has_more"] is False
    assert len(body["articles"]) == 2


@pytest.mark.asyncio
async def test_a_failed_claim_lookup_leaves_the_listing_intact(beanie_db, monkeypatch) -> None:
    """The store is unreachable. A de-duplication preference must not turn
    into a failed listing — a duplicate row is visible and recoverable, a 500
    on the Library is not."""
    await _seed_upload(name="public-speaking.pdf", article_id=BOOK, scope=WS_SCOPE)
    client = _build_client(monkeypatch, {WS_SCOPE: [_row(BOOK, "Book"), _row(NOTE, "Note")]})

    async def boom(*_a, **_kw):
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(knowledge_router_module, "_upload_claims", boom)

    response = client.get("/api/v1/knowledge/articles?exclude_upload_derived=true")

    assert response.status_code == 200
    assert {a["id"] for a in response.json()["articles"]} == {BOOK, NOTE}


@pytest.mark.asyncio
async def test_the_claim_lookup_is_one_query_for_the_whole_page(beanie_db, monkeypatch) -> None:
    """The listing is hot and paged, so the join must not be per-row. Counts
    calls rather than trusting the shape of the code."""
    await _seed_upload(name="a.pdf", article_id=BOOK, scope=WS_SCOPE)
    client = _build_client(
        monkeypatch,
        {
            WS_SCOPE: [_row(BOOK, "Book"), _row(NOTE, "Note"), _row("third", "Third")],
            AGENT_SCOPE: [_row("fourth", "Fourth")],
        },
    )

    calls: list[list[str]] = []
    real = knowledge_router_module._upload_claims

    async def counting(workspace_id: str, article_ids: list[str]):
        calls.append(list(article_ids))
        return await real(workspace_id, article_ids)

    monkeypatch.setattr(knowledge_router_module, "_upload_claims", counting)

    body = client.get("/api/v1/knowledge/articles?exclude_upload_derived=true").json()

    assert len(calls) == 1
    assert set(calls[0]) == {BOOK, NOTE, "third", "fourth"}
    assert {a["id"] for a in body["articles"]} == {NOTE, "third", "fourth"}
