# test_content_search.py — POST /files/search: the join, the filters, the route.
#
# Created 2026-08-29 (T3 "Files content search").
#
# Three classes of failure this file exists to catch, in the order they hurt:
#
# 1. THE ROUTE STRING. A new endpoint's client and its test can pin the SAME
#    wrong path and both stay green — that has happened here before. So the
#    route tests never import the path constant: they mount the routers in the
#    app's real order and POST the LITERAL "/api/v1/files/search", which is
#    also the literal the frontend test types. If the constant moves, one of
#    the two sides goes red.
# 2. THE TENANCY FILTERS. Search is a second door onto the same rows the
#    listing serves, so it has to enforce the same walls: workspace, soft
#    delete, hide-from-AI, and the pocket partition. Each gets a test that
#    seeds a row on the WRONG side of the wall and requires it to be absent.
#    (Verified by mutation: drop the filter, watch the test go red.)
# 3. THE HONESTY FIELD. An unreachable kb must report "kb_unavailable", not an
#    empty list — an empty list renders as "nothing in your files matches",
#    which is the shape every switched-off feature in this codebase has taken.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from beanie import init_beanie
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from pocketpaw_ee.cloud.files import content_search as cs
from pocketpaw_ee.cloud.uploads.models import FileUpload
from pocketpaw_ee.cloud.uploads.mongo_store import (
    LIST_WORKSPACE_ONLY,
    KbTrackedRecord,
    MongoFileStore,
)

from pocketpaw.uploads.file_store import FileRecord

# The literal every client must use. Typed out, not imported — see (1) above.
SEARCH_PATH = "/api/v1/files/search"


def _router_module():
    """The files router MODULE.

    ``import pocketpaw_ee.cloud.files.router`` does not give you the module:
    the package ``__init__`` does ``from ...router import router``, so the
    attribute ``router`` on the package is the APIRouter and shadows the
    submodule name. Go through the module registry instead.
    """
    import importlib

    return importlib.import_module("pocketpaw_ee.cloud.files.router")


@pytest.fixture(autouse=True)
def _no_real_storage(monkeypatch):
    """Make the process-wide uploads adapter unusable for the whole module.

    In a dev checkout ``uploads/router._ADAPTER`` is built at import time from
    ``load_dotenv()`` + ``POCKETPAW_UPLOAD_ADAPTER=s3``, so it is a LIVE S3
    client aimed at a real bucket — and mounting the /files surface pulls that
    module in transitively. Content search never touches blob storage (the
    join is metadata-only), and this fixture makes that a guarantee instead of
    an observation: any future test here that reaches for the adapter gets a
    loud AttributeError rather than a network write.
    """
    import importlib

    uploads_router = importlib.import_module("pocketpaw_ee.cloud.uploads.router")

    class _Detonate:
        def __getattr__(self, name):  # pragma: no cover — tripwire
            raise AssertionError(
                f"a files-search test reached the real uploads adapter (.{name}). "
                "Inject a fake adapter; never resolve the default."
            )

    monkeypatch.setattr(uploads_router, "_ADAPTER", _Detonate(), raising=False)
    yield


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The compiled_with probe is TTL-cached per scope at module level; a test
    must never inherit another test's corpus."""
    cs.reset_compiled_with_cache()
    yield
    cs.reset_compiled_with_cache()


@pytest.fixture()
async def beanie_files_db():
    db_name = f"test_files_search_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]
    await init_beanie(database=db, document_models=[FileUpload])
    yield db


async def _seed(
    workspace: str,
    *,
    name: str,
    article_id: str | None = None,
    scope: str | None = None,
    pocket_id: str | None = None,
    hide_from_ai: bool = False,
) -> str:
    store = MongoFileStore()
    rec = FileRecord(
        id=uuid.uuid4().hex,
        storage_key=f"keys/{uuid.uuid4().hex}",
        filename=name,
        mime="text/plain",
        size=11,
        owner_id="u1",
        chat_id=None,
        created=datetime.now(UTC),
    )
    await store.save_scoped(rec, workspace=workspace, pocket_id=pocket_id)
    if article_id:
        await store.set_kb_article(rec.id, workspace, article_id=article_id, scope=scope)
    if hide_from_ai:
        await store.set_library_metadata(rec.id, workspace, hide_from_ai=True)
    return rec.id


def _hit(article_id: str, *, scope: str = "", title: str = "T", summary: str = "S") -> dict:
    """One row shaped like kb-go's ``search --json`` output."""
    row = {"id": article_id, "title": title, "summary": summary, "concepts": []}
    if scope:
        row["scope"] = scope
    return row


class _FakeStore:
    """Stands in for MongoFileStore where the DB is not what's under test."""

    def __init__(self, rows: list[KbTrackedRecord]) -> None:
        self._rows = rows
        self.calls: list[dict] = []

    async def list_by_kb_articles(self, workspace, article_ids, *, pocket_id=None, limit=200):
        self.calls.append(
            {"workspace": workspace, "article_ids": list(article_ids), "pocket_id": pocket_id}
        )
        wanted = set(article_ids)
        return [r for r in self._rows if r.article_id in wanted]


def _tracked(article_id: str, *, file_id: str, name: str, scope: str | None = None):
    return KbTrackedRecord(
        article_id=article_id,
        scope=scope,
        record=FileRecord(
            id=file_id,
            storage_key=f"keys/{file_id}",
            filename=name,
            mime="text/plain",
            size=1,
            owner_id="u1",
            chat_id=None,
            created=datetime.now(UTC),
            summary="what this file is",
        ),
    )


# ---------------------------------------------------------------------------
# The route string
# ---------------------------------------------------------------------------


def _mount_files_surface(*, member_of: str | None = "w1", role: str = "member") -> FastAPI:
    """Mount the routers that share the /files prefix, in the order
    ``cloud/__init__.py`` mounts them (share → files → file_versions).

    FastAPI resolves by registration order, so mounting only our own router
    would prove nothing about whether a sibling swallows the path in the real
    app.

    The RBAC guard is NOT stubbed: ``current_active_user`` returns a fake user
    with a real membership list and the genuine ``check_workspace_action``
    runs, so ``member_of=None`` exercises an actual denial rather than a
    disabled check.
    """
    from types import SimpleNamespace

    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.files.router import router as files_router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import (
        current_active_user,
        current_user_id,
        current_workspace_id,
    )
    from pocketpaw_ee.cloud.uploads.share_router import share_router

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None

    async def _user_dep(x_user: str = Header(default="u1")) -> str:
        return x_user

    async def _workspace_dep(x_workspace: str = Header(default="w1")) -> str:
        return x_workspace

    async def _active_user():
        return SimpleNamespace(
            id="u1",
            active_workspace=member_of,
            workspaces=([SimpleNamespace(workspace=member_of, role=role)] if member_of else []),
        )

    app.dependency_overrides[current_user_id] = _user_dep
    app.dependency_overrides[current_workspace_id] = _workspace_dep
    app.dependency_overrides[current_active_user] = _active_user

    app.include_router(share_router, prefix="/api/v1")
    app.include_router(files_router, prefix="/api/v1")
    try:
        from pocketpaw_ee.cloud.file_versions.router import router as file_versions_router

        app.include_router(file_versions_router, prefix="/api/v1")
    except Exception:  # pragma: no cover — optional in a slim install
        pass
    return app


def test_the_search_route_exists_at_the_literal_path():
    """The path a client will type, asserted as a literal against the app's
    own route table. Not built from the constant the code uses."""
    app = _mount_files_surface()
    posts = {r.path for r in app.routes if "POST" in getattr(r, "methods", set()) or set()}
    assert SEARCH_PATH in posts, (
        f"No POST route at {SEARCH_PATH!r}. Mounted POST paths: {sorted(posts)}"
    )


def test_a_post_to_the_literal_path_reaches_the_content_search_handler(monkeypatch):
    """Existence in the table is not reachability — a sibling router mounted
    earlier can swallow the path. Send a real request and require OUR
    handler's response envelope back."""
    called: list[str] = []

    async def fake_search(**kwargs):
        called.append(kwargs["query"])
        return cs.ContentSearchResult(matches=[], scopes=["workspace:w1"], degraded=None)

    files_router_mod = _router_module()

    monkeypatch.setattr(files_router_mod, "search_file_contents", fake_search)

    client = TestClient(_mount_files_surface())
    resp = client.post(
        SEARCH_PATH,
        json={"query": "quarterly revenue"},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert called == ["quarterly revenue"], "the request did not reach the search handler"
    # The envelope the panel reads. `degraded` present-and-null is part of the
    # contract: absent would be indistinguishable from an older backend.
    assert set(body) >= {"files", "scopes", "degraded", "query", "workspace_id"}
    assert body["degraded"] is None


# ---------------------------------------------------------------------------
# The join: kb hits → file rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hits_come_back_as_file_rows_in_the_search_ranking():
    """kb ranks; we keep that order. The rows are listing rows — the panel
    renders them with the components it already has."""
    store = _FakeStore(
        [
            _tracked("art-b", file_id="f-b", name="b.pdf", scope="workspace:w1"),
            _tracked("art-a", file_id="f-a", name="a.pdf", scope="workspace:w1"),
        ]
    )

    async def kb_search(query, scopes, limit):
        return [_hit("art-b", title="Budget"), _hit("art-a", title="Alpha")]

    res = await cs.search_file_contents(
        workspace_id="w1",
        user_id="u1",
        query="revenue",
        store=store,
        kb_search=kb_search,
        kb_list=lambda scope: _none(),
    )
    assert [m.file.filename for m in res.matches] == ["b.pdf", "a.pdf"]
    assert res.degraded is None
    row = res.matches[0].to_json()
    # The listing row, whole — not a narrowed projection.
    assert row["filename"] == "b.pdf"
    assert row["summary"] == "what this file is"
    assert row["match"]["article_id"] == "art-b"
    assert row["match"]["title"] == "Budget"


async def _none():
    return []


@pytest.mark.asyncio
async def test_a_hit_with_no_file_behind_it_is_simply_not_a_result():
    """kb holds articles that were never files (URL ingests, chat memories).
    Those are not answers to 'which of my files says this'."""
    store = _FakeStore([_tracked("art-a", file_id="f-a", name="a.pdf")])

    async def kb_search(query, scopes, limit):
        return [_hit("art-url"), _hit("art-a"), _hit("art-other")]

    res = await cs.search_file_contents(
        workspace_id="w1",
        user_id="u1",
        query="revenue",
        store=store,
        kb_search=kb_search,
        kb_list=lambda scope: _none(),
    )
    assert [m.file.filename for m in res.matches] == ["a.pdf"]


@pytest.mark.asyncio
async def test_one_file_matched_twice_appears_once():
    """A file re-ingested under two article ids must not double in the list —
    a duplicate row reads as a rendering bug, not as two matches."""
    store = _FakeStore(
        [
            _tracked("art-1", file_id="f-a", name="a.pdf"),
            _tracked("art-2", file_id="f-a", name="a.pdf"),
        ]
    )

    async def kb_search(query, scopes, limit):
        return [_hit("art-1"), _hit("art-2")]

    res = await cs.search_file_contents(
        workspace_id="w1",
        user_id="u1",
        query="revenue",
        store=store,
        kb_search=kb_search,
        kb_list=lambda scope: _none(),
    )
    assert len(res.matches) == 1


@pytest.mark.asyncio
async def test_an_empty_query_never_shells_out_to_kb():
    """The debounced box will send whitespace. That must cost nothing."""
    calls: list[str] = []

    async def kb_search(query, scopes, limit):
        calls.append(query)
        return []

    res = await cs.search_file_contents(
        workspace_id="w1", user_id="u1", query="   ", store=_FakeStore([]), kb_search=kb_search
    )
    assert calls == []
    assert res.matches == []
    assert res.degraded is None


@pytest.mark.asyncio
async def test_the_search_asks_every_scope_the_caller_can_read_in_one_call():
    """One subprocess, all scopes, most-specific first — the same precedence
    the chat path uses."""
    seen: list[list[str]] = []

    async def kb_search(query, scopes, limit):
        seen.append(list(scopes))
        return []

    await cs.search_file_contents(
        workspace_id="w1",
        user_id="u1",
        query="revenue",
        pocket_id="p9",
        store=_FakeStore([]),
        kb_search=kb_search,
    )
    assert seen == [["user:u1", "pocket:p9", "workspace:w1"]]


# ---------------------------------------------------------------------------
# The honesty field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreachable_kb_says_so_instead_of_returning_no_matches():
    """An empty list would render as 'nothing in your files matches'. That is
    a lie when the search never ran, and it is exactly the shape a
    switched-off feature takes."""

    async def kb_search(query, scopes, limit):
        raise RuntimeError("kb binary not found at 'kb-go'")

    res = await cs.search_file_contents(
        workspace_id="w1", user_id="u1", query="revenue", store=_FakeStore([]), kb_search=kb_search
    )
    assert res.matches == []
    assert res.degraded == cs.DEGRADED_KB_UNAVAILABLE


@pytest.mark.asyncio
async def test_a_verbatim_article_is_reported_as_degraded_matching():
    """kb-go's keyless fallback stores raw text as the 'article'. Matching
    against one is literal, not compiled. The ingest funnel rejects these now,
    but FL-11b tracking predates that hardening, so tracked verbatim rows
    exist in the wild."""
    store = _FakeStore([_tracked("art-a", file_id="f-a", name="a.pdf", scope="workspace:w1")])

    async def kb_search(query, scopes, limit):
        return [_hit("art-a", scope="workspace:w1")]

    async def kb_list(scope):
        return [{"id": "art-a", "compiled_with": "none (fallback)"}]

    res = await cs.search_file_contents(
        workspace_id="w1",
        user_id="u1",
        query="revenue",
        store=store,
        kb_search=kb_search,
        kb_list=kb_list,
    )
    assert res.degraded == cs.DEGRADED_VERBATIM
    assert res.matches[0].verbatim is True
    assert res.matches[0].to_json()["match"]["verbatim"] is True


@pytest.mark.asyncio
async def test_a_compiled_article_is_not_reported_as_degraded():
    """The complement — without it, the verbatim test passes on a function
    that hardcodes True."""
    store = _FakeStore([_tracked("art-a", file_id="f-a", name="a.pdf", scope="workspace:w1")])

    async def kb_search(query, scopes, limit):
        return [_hit("art-a", scope="workspace:w1")]

    async def kb_list(scope):
        return [{"id": "art-a", "compiled_with": "claude-sonnet"}]

    res = await cs.search_file_contents(
        workspace_id="w1",
        user_id="u1",
        query="revenue",
        store=store,
        kb_search=kb_search,
        kb_list=kb_list,
    )
    assert res.degraded is None
    assert res.matches[0].verbatim is False


@pytest.mark.asyncio
async def test_a_failing_compiled_with_probe_does_not_fail_the_search():
    """We could not read the corpus metadata. That is not a reason to throw
    away results we already have."""
    store = _FakeStore([_tracked("art-a", file_id="f-a", name="a.pdf", scope="workspace:w1")])

    async def kb_search(query, scopes, limit):
        return [_hit("art-a", scope="workspace:w1")]

    async def kb_list(scope):
        raise RuntimeError("kb list blew up")

    res = await cs.search_file_contents(
        workspace_id="w1",
        user_id="u1",
        query="revenue",
        store=store,
        kb_search=kb_search,
        kb_list=kb_list,
    )
    assert [m.file.filename for m in res.matches] == ["a.pdf"]
    assert res.degraded is None


# ---------------------------------------------------------------------------
# The tenancy walls (real store, real Mongo shapes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_join_is_pinned_to_the_callers_workspace(beanie_files_db):
    """Article ids are title slugs, so two tenants can hold the same id. The
    workspace filter is the only thing between them."""
    await _seed("w1", name="mine.pdf", article_id="shared-slug", scope="workspace:w1")
    await _seed("w2", name="theirs.pdf", article_id="shared-slug", scope="workspace:w2")

    rows = await MongoFileStore().list_by_kb_articles("w1", ["shared-slug"])
    assert [r.record.filename for r in rows] == ["mine.pdf"]


@pytest.mark.asyncio
async def test_a_file_hidden_from_ai_never_surfaces_in_content_search(beanie_files_db):
    """Hiding a file also purges its article — but a purge that failed leaves
    the tracking behind, and then the hidden file is one query from being
    read back out. Defence in depth, not decoration."""
    await _seed("w1", name="visible.pdf", article_id="a-visible")
    await _seed("w1", name="hidden.pdf", article_id="a-hidden", hide_from_ai=True)

    rows = await MongoFileStore().list_by_kb_articles("w1", ["a-visible", "a-hidden"])
    assert [r.record.filename for r in rows] == ["visible.pdf"]


@pytest.mark.asyncio
async def test_a_legacy_row_with_no_hide_from_ai_key_still_matches(beanie_files_db):
    """Rows written before FL-1 have NO hide_from_ai key in Mongo. A
    ``{"hide_from_ai": False}`` filter matches none of them, so the whole
    pre-FL-1 library would be silently unsearchable — invisible, green, and
    wrong. This is why the filter is ``$ne: True``."""
    raw = beanie_files_db[FileUpload.get_settings().name]
    await raw.insert_one(
        {
            "file_id": "legacy-1",
            "storage_key": "keys/legacy-1",
            "filename": "ancient.pdf",
            "mime": "application/pdf",
            "size": 9,
            "workspace": "w1",
            "owner": "u1",
            "createdAt": datetime.now(UTC),
            "kb_article_id": "a-legacy",
            # No hide_from_ai. No tags. No collections. Like the real rows.
        }
    )

    rows = await MongoFileStore().list_by_kb_articles("w1", ["a-legacy"])
    assert [r.record.filename for r in rows] == ["ancient.pdf"]


@pytest.mark.asyncio
async def test_a_deleted_file_is_gone_from_search_too(beanie_files_db):
    file_id = await _seed("w1", name="gone.pdf", article_id="a-gone")
    await MongoFileStore().soft_delete_scoped(file_id, "w1")

    rows = await MongoFileStore().list_by_kb_articles("w1", ["a-gone"])
    assert rows == []


@pytest.mark.asyncio
async def test_pocket_files_do_not_bleed_into_a_workspace_search(beanie_files_db):
    """The listing's privacy contract, enforced at the search door too."""
    await _seed("w1", name="ws.pdf", article_id="a-ws")
    await _seed("w1", name="secret.pdf", article_id="a-pocket", pocket_id="P")

    rows = await MongoFileStore().list_by_kb_articles(
        "w1", ["a-ws", "a-pocket"], pocket_id=LIST_WORKSPACE_ONLY
    )
    assert [r.record.filename for r in rows] == ["ws.pdf"]


@pytest.mark.asyncio
async def test_a_pocket_search_returns_that_pockets_files_only(beanie_files_db):
    await _seed("w1", name="ws.pdf", article_id="a-ws")
    await _seed("w1", name="p-secret.pdf", article_id="a-p", pocket_id="P")
    await _seed("w1", name="q-secret.pdf", article_id="a-q", pocket_id="Q")

    rows = await MongoFileStore().list_by_kb_articles("w1", ["a-ws", "a-p", "a-q"], pocket_id="P")
    assert [r.record.filename for r in rows] == ["p-secret.pdf"]


@pytest.mark.asyncio
async def test_the_workspace_search_asks_for_workspace_only_rows():
    """The partition is chosen by the service, not by the store's default.
    Passing pocket_id=None would apply NO filter and leak pocket rows."""
    store = _FakeStore([])

    async def kb_search(query, scopes, limit):
        return [_hit("art-a")]

    await cs.search_file_contents(
        workspace_id="w1", user_id="u1", query="revenue", store=store, kb_search=kb_search
    )
    assert store.calls[0]["pocket_id"] is LIST_WORKSPACE_ONLY


@pytest.mark.asyncio
async def test_a_pocket_search_passes_the_pocket_through():
    store = _FakeStore([])

    async def kb_search(query, scopes, limit):
        return [_hit("art-a")]

    await cs.search_file_contents(
        workspace_id="w1",
        user_id="u1",
        query="revenue",
        pocket_id="P",
        store=store,
        kb_search=kb_search,
    )
    assert store.calls[0]["pocket_id"] == "P"


# ---------------------------------------------------------------------------
# The endpoint's own ACL
# ---------------------------------------------------------------------------


def test_a_non_member_cannot_search_a_pocket(monkeypatch):
    """The same 403 the listing raises, from the same helper."""
    files_router_mod = _router_module()

    async def not_a_member(pocket_id, user_id):
        return False

    monkeypatch.setattr(files_router_mod, "_pocket_readable", not_a_member)

    client = TestClient(_mount_files_surface())
    resp = client.post(
        SEARCH_PATH,
        json={"query": "revenue", "pocket_id": "P"},
        headers={"x-user": "bob", "x-workspace": "w1"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "files.pocket_forbidden"


def test_the_endpoint_is_behind_the_kb_read_guard(monkeypatch):
    """The match block carries kb titles and summaries — kb content. Without
    the ``kb.read`` guard this is a second, ungated door to what
    ``POST /kb/search`` protects. A caller with no membership in the active
    workspace must never reach the handler."""
    reached: list[str] = []

    async def fake_search(**kwargs):
        reached.append(kwargs["query"])
        return cs.ContentSearchResult(matches=[], scopes=[], degraded=None)

    monkeypatch.setattr(_router_module(), "search_file_contents", fake_search)

    client = TestClient(_mount_files_surface(member_of=None))
    resp = client.post(
        SEARCH_PATH,
        json={"query": "revenue"},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert resp.status_code == 403, resp.text
    assert reached == [], "the guard let an outsider reach the search handler"


def test_the_endpoint_accepts_no_scope_override(monkeypatch):
    """/kb/search takes a client scope and binds it to the caller. This
    surface takes none at all — a scope in the body must be ignored, never
    threaded into the kb call."""
    seen: list[dict] = []

    async def fake_search(**kwargs):
        seen.append(kwargs)
        return cs.ContentSearchResult(matches=[], scopes=[], degraded=None)

    files_router_mod = _router_module()

    monkeypatch.setattr(files_router_mod, "search_file_contents", fake_search)

    client = TestClient(_mount_files_surface())
    resp = client.post(
        SEARCH_PATH,
        json={"query": "revenue", "scope": "user:victim"},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert resp.status_code == 200, resp.text
    assert "scope" not in seen[0]
    assert seen[0]["workspace_id"] == "w1"
    assert seen[0]["user_id"] == "u1"


# ---------------------------------------------------------------------------
# Seam assertions
# ---------------------------------------------------------------------------
#
# The design doc's non-negotiable (a): every new integration ships a check that
# the API it calls actually EXISTS with the shape it is called with. Content
# search wraps three of its four seams in `except Exception`, which is correct
# for a search box and lethal for a signature drift — a renamed helper would
# not raise, it would render "couldn't search inside your files" forever, and
# the feature would read as switched off rather than broken. These tests fail
# at the rename instead.


def test_the_kb_seam_exists_with_the_shape_content_search_calls():
    """``_kb`` is called positionally as ``_kb("search", q, "--scope", s,
    "--limit", n)`` and ``_kb("list", "--scope", s)``, through
    ``asyncio.to_thread``. A signature change here surfaces as a permanent
    ``kb_unavailable``."""
    import inspect

    from pocketpaw_ee.cloud.agents.knowledge import _kb

    sig = inspect.signature(_kb)
    params = list(sig.parameters.values())
    assert params[0].kind is inspect.Parameter.VAR_POSITIONAL, (
        "_kb no longer takes *args; content_search passes its kb argv positionally"
    )
    # Keyword-only extras content_search relies on being optional.
    for name in ("input_text", "timeout"):
        assert name in sig.parameters, f"_kb lost its {name!r} keyword"


def test_the_scope_seam_exists_and_still_orders_user_pocket_workspace():
    """``resolve_kb_scopes`` builds a ``ScopeContext`` by keyword and reads the
    precedence out of ``_kb_scopes_for_context``. If that module renames a
    field, the constructor raises — but if it silently reorders, tenancy
    changes with no error at all. Pin the order, not just the import."""
    assert cs.resolve_kb_scopes(workspace_id="w1", user_id="u1", pocket_id=None) == [
        "user:u1",
        "workspace:w1",
    ]
    assert cs.resolve_kb_scopes(workspace_id="w1", user_id="u1", pocket_id="p9") == [
        "user:u1",
        "pocket:p9",
        "workspace:w1",
    ]


def test_the_pocket_membership_seam_takes_the_keywords_the_gate_passes():
    """``_pocket_readable`` calls ``is_member(pocket_id=…, user_id=…)`` inside
    an ``except Exception: return False``. A renamed keyword would therefore
    deny EVERY pocket search silently — the switched-off shape exactly."""
    import inspect

    from pocketpaw_ee.cloud.pockets import service as pockets_service

    sig = inspect.signature(pockets_service.is_member)
    assert {"pocket_id", "user_id"} <= set(sig.parameters), (
        f"is_member no longer accepts pocket_id/user_id: {sig}"
    )


@pytest.mark.asyncio
async def test_the_store_seam_accepts_the_pocket_sentinel(beanie_files_db):
    """``search_file_contents`` hands ``LIST_WORKSPACE_ONLY`` — a sentinel
    OBJECT, not a string — to the store. A store that started treating an
    unknown type as "no filter" would leak pocket rows into the workspace
    panel without erroring."""
    await _seed("w1", name="ws.pdf", article_id="a-ws")
    await _seed("w1", name="p.pdf", article_id="a-p", pocket_id="P")

    rows = await MongoFileStore().list_by_kb_articles(
        "w1", ["a-ws", "a-p"], pocket_id=LIST_WORKSPACE_ONLY
    )
    assert [r.record.filename for r in rows] == ["ws.pdf"]
