# tests/ee/sites/test_site_upgrade.py — "Upgrade this site" (``sites/site_upgrade.py``)
# end to end over the HTTP routes against a mongomock pocket: an html site (and an
# imported one) becomes a static project with the starter's manifest and AGENTS.md
# and a tokens file themed from its own CSS and linked from every page, its Site doc
# untouched and its first preview staged by the no-build lane; the
# refusals (starter files already there, no root index.html, a non-html engine); undo
# while untouched and its 409 after an edit; the site's CSS pointed at the tokens and
# restored byte for byte by undo; the opt-in re-run for a site already on the
# starter; and the auth matrix. The paw-sites CLI
# (``copy_template``) and the artifact / verify stores are faked.
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.pockets import service as pockets_service  # noqa: E402
from pocketpaw_ee.sites import engines, project_build, project_tools, site_tokens  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402

from tests.ee.sites.test_project_engine import _Records, _Store  # noqa: E402

TEMPLATE = {
    "slug": "static",
    "name": "Static HTML",
    "target": "assets",
    "backend": {"builtin": []},
    "secrets": [],
    "build": "none",
}
STARTER = {
    "paw.template.json": json.dumps(TEMPLATE),
    "AGENTS.md": "# Static site\nEdit index.html.\n",
    "index.html": "<html><body>starter</body></html>",
    "styles/tokens.css": ":root{}\n",
}
HTML = {
    "index.html": "<html><body><h1>Bakery</h1></body></html>",
    "about.html": "<html><body>About</body></html>",
    "styles.css": "h1{color:red}\n",
}
BASE = "/api/v1/sites/by-pocket/{pid}/upgrade"


class _Membership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _User:
    def __init__(self, user_id: str, workspaces: list[str]) -> None:
        self.id = user_id
        self.active_workspace = workspaces[0] if workspaces else "ws_none"
        self.workspaces = [_Membership(w) for w in workspaces]


def _app(user_id: str = "u1", workspace_id: str = "ws1", member_of: list[str] | None = None):
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router

    user = _User(user_id, [workspace_id] if member_of is None else member_of)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id="t",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture(autouse=True)
def _preview_base(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")


@pytest.fixture()
def starter(monkeypatch) -> list[str]:
    """``copy_template`` without the paw-sites CLI; records the slugs asked for."""
    asked: list[str] = []

    async def _copy(slug: str) -> dict[str, str]:
        asked.append(slug)
        return dict(STARTER)

    monkeypatch.setattr(project_tools, "copy_template", _copy)
    return asked


@pytest.fixture()
def stores(monkeypatch):
    """In-memory artifact + verify stores, so the first draft build is observable."""
    from pocketpaw_ee.sites import verify_store

    store, records = _Store(), _Records()
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: store)
    monkeypatch.setattr(verify_store, "default_verify_store", lambda: records)
    return store, records


async def _pocket(engine: str = "html", source: dict | None = None, **kw) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Bakery",
        type_="site",
        pattern=kw.get("pattern", "landing"),
        ripple_spec=kw.get("ripple_spec"),
        engine=engine,
        source=None if source is None and engine == "ripple" else dict(source or HTML),
        trusted=True,
    )
    assert err is None, err
    return pocket_id


async def _doc(pocket_id: str):
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.pocket import Pocket

    return await Pocket.get(ObjectId(pocket_id))


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["landing", "imported"])
async def test_upgrade_converts_an_html_site_in_place(
    beanie_test_db, starter, stores, pattern
) -> None:
    store, _records = stores
    pid = await _pocket(pattern=pattern)
    site = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id=pid, name="Bakery"
    )
    from pocketpaw_ee.cloud.models.site import Site

    site_before = (await Site.get(site.id)).model_dump()

    async with _client(_app()) as c:
        r = await c.post(BASE.format(pid=pid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert starter == ["static"]
    assert body["engine"] == "project" and body["template"] == "static"
    assert body["added"] == ["AGENTS.md", "paw.template.json", "styles/tokens.css"]
    assert body["upgraded_from"]["engine"] == "html"
    # The first preview went through the no-build lane, in-process.
    assert body["verification"]["status"] == "passed"
    assert body["verification"]["preview_mode"] == "full"

    doc = await _doc(pid)
    assert doc.engine == "project"
    assert doc.pattern == pattern
    # The html files are carried over; each page also links the new tokens file, and
    # the two starter files plus that tokens file join.
    assert set(doc.source) == {*HTML, "paw.template.json", "AGENTS.md", "styles/tokens.css"}
    assert doc.source["paw.template.json"] == STARTER["paw.template.json"]
    assert doc.source["AGENTS.md"] == STARTER["AGENTS.md"]
    assert doc.source["styles.css"] == HTML["styles.css"]
    for page in ("index.html", "about.html"):
        assert doc.source[page] == site_tokens.link_tokens(HTML[page])
        assert site_tokens.TOKENS_HREF in doc.source[page]
    assert ":root" in doc.source["styles/tokens.css"]
    assert doc.site_meta["project"] == {"template": "static", "framework": "static", "recipes": []}
    record = doc.site_meta["upgraded_from"]
    assert record["source_hash"] == body["upgraded_from"]["source_hash"]
    assert record["version_no"] == body["upgraded_from"]["version_no"]
    assert record["site_meta"] == {}
    content_hash = project_build.project_content_hash(doc.source)
    assert (pid, content_hash) in store.dist

    # The Site doc is the same site, untouched.
    after = await Site.get(site.id)
    assert after.model_dump() == site_before

    # The old source is kept as a version the owner can see in the history.
    from pocketpaw_ee.versions import service as versions_service

    rows = await versions_service.list_versions(scope_type="pocket", scope_id=pid)
    snapshot = next(r for r in rows if str(r.id) == record["version_id"])
    assert snapshot.content == HTML


async def test_upgrade_themes_the_tokens_file_from_the_site(beanie_test_db, starter, stores):
    source = {
        "index.html": "<html><head><style>h1 { font-family: Georgia, serif }</style></head>"
        "<body><h1>Bakery</h1></body></html>",
        "site.css": "body { background-color: #fdf6ec; color: #3b2f2f; "
        "font-family: 'Lora', serif }\n.btn { background: #c2410c }\n",
    }
    pid = await _pocket(source=source)
    async with _client(_app()) as c:
        r = await c.post(BASE.format(pid=pid))
    assert r.status_code == 200, r.text
    tokens = site_tokens.root_declarations((await _doc(pid)).source["styles/tokens.css"])
    assert tokens["--background"] == "#fdf6ec"
    assert tokens["--foreground"] == "#3b2f2f"
    assert tokens["--primary"] == "#c2410c"
    assert tokens["--font-sans"] == "'Lora', serif"
    assert tokens["--font-heading"] == "Georgia, serif"


async def test_upgrade_falls_back_to_the_starter_tokens(
    beanie_test_db, starter, stores, monkeypatch
):
    themed = (
        ":root {\n  color-scheme: light dark;\n  --background: #fefefe;\n  --space-1: 0.5rem;\n}\n"
    )
    monkeypatch.setitem(STARTER, "styles/tokens.css", themed)
    pid = await _pocket()
    async with _client(_app()) as c:
        assert (await c.post(BASE.format(pid=pid))).status_code == 200
    css = (await _doc(pid)).source["styles/tokens.css"]
    tokens = site_tokens.root_declarations(css)
    assert tokens["--background"] == "#fefefe"
    assert tokens["--space-1"] == "0.5rem"
    assert "color-scheme" not in css


async def test_upgrade_keeps_an_owner_tokens_file(beanie_test_db, starter, stores):
    source = {**HTML, "styles/tokens.css": ":root { --brand: red; }\n"}
    pid = await _pocket(source=source)
    async with _client(_app()) as c:
        r = await c.post(BASE.format(pid=pid))
    assert r.status_code == 200, r.text
    assert r.json()["added"] == ["AGENTS.md", "paw.template.json"]
    doc = await _doc(pid)
    assert doc.source["styles/tokens.css"] == source["styles/tokens.css"]
    assert site_tokens.TOKENS_HREF in doc.source["index.html"]


async def test_upgraded_site_reads_as_upgradable_no_longer(beanie_test_db, starter, stores):
    assert engines.site_capabilities("html", has_database=False)["upgradable"] is True
    pid = await _pocket()
    async with _client(_app()) as c:
        assert (await c.post(BASE.format(pid=pid))).status_code == 200
        r = await c.post(BASE.format(pid=pid))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "sites.upgrade_unsupported"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("taken", ["paw.template.json", "AGENTS.md", "./AGENTS.md"])
async def test_refuses_to_overwrite_an_owner_starter_file(
    beanie_test_db, starter, stores, taken
) -> None:
    source = {**HTML, taken: "mine"}
    pid = await _pocket(source=source)
    async with _client(_app()) as c:
        r = await c.post(BASE.format(pid=pid))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "sites.upgrade_file_exists"
    doc = await _doc(pid)
    assert doc.engine == "html" and doc.source == source


async def test_refuses_a_site_without_a_root_index(beanie_test_db, starter, stores) -> None:
    source = {"pages/index.html": "<h1>x</h1>", "styles.css": ""}
    pid = await _pocket(source=source)
    async with _client(_app()) as c:
        r = await c.post(BASE.format(pid=pid))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "sites.upgrade_no_index"
    assert (await _doc(pid)).engine == "html"


@pytest.mark.parametrize("engine", ["svelte", "react", "project", "ripple"])
async def test_refuses_every_engine_but_html(beanie_test_db, starter, stores, engine) -> None:
    source = (
        {"package.json": "{}", "index.html": "<h1>x</h1>"}
        if engine == "project"
        else {"src/routes/+page.svelte": "<h1>x</h1>"}
    )
    if engine == "ripple":
        pid = await _pocket(engine="ripple", ripple_spec={"ui": []})
    else:
        pid = await _pocket(engine=engine, source=source)
    async with _client(_app()) as c:
        r = await c.post(BASE.format(pid=pid))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "sites.upgrade_unsupported"
    assert (await _doc(pid)).engine == engine
    assert starter == []


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


async def test_revert_restores_engine_source_and_site_meta(beanie_test_db, starter, stores):
    pid = await _pocket()
    doc = await _doc(pid)
    await doc.set({"site_meta": {"note": "kept"}})
    async with _client(_app()) as c:
        up = await c.post(BASE.format(pid=pid))
        assert up.status_code == 200, up.text
        r = await c.post(f"{BASE.format(pid=pid)}/revert")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "pocket_id": pid,
        "engine": "html",
        "version_no": up.json()["upgraded_from"]["version_no"],
    }
    doc = await _doc(pid)
    assert doc.engine == "html"
    assert doc.source == HTML
    assert doc.site_meta == {"note": "kept"}


async def test_revert_after_an_edit_is_409(beanie_test_db, starter, stores) -> None:
    pid = await _pocket()
    async with _client(_app()) as c:
        assert (await c.post(BASE.format(pid=pid))).status_code == 200
        await project_tools.write_files(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pid,
            files={"index.html": "<h1>Edited</h1>"},
        )
        r = await c.post(f"{BASE.format(pid=pid)}/revert")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "sites.upgrade_revert_stale"
    doc = await _doc(pid)
    assert doc.engine == "project" and doc.source["index.html"] == "<h1>Edited</h1>"


async def test_revert_of_a_site_never_upgraded_is_422(beanie_test_db, starter, stores) -> None:
    pid = await _pocket()
    async with _client(_app()) as c:
        r = await c.post(f"{BASE.format(pid=pid)}/revert")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "sites.upgrade_not_upgraded"


# ---------------------------------------------------------------------------
# The site's CSS pointed at the tokens, the round trip, and the opt-in re-run
# ---------------------------------------------------------------------------

THEMED = {
    "index.html": "<html><head><style>h1 { font-family: Georgia, serif }</style></head>"
    "<body><h1>Bakery</h1></body></html>",
    "site.css": "body { background: #FDF6EC; color: #3b2f2f }\n.btn { background: #c2410c }\n"
    "@media (max-width: 600px) { body { background: #fdf6ec } }\n",
    "vendor/lib.css": ".btn { background: #c2410c }\n",
}


async def test_upgrade_points_the_site_css_at_the_tokens_and_undo_restores_it(
    beanie_test_db, starter, stores
):
    pid = await _pocket(source=THEMED)
    async with _client(_app()) as c:
        up = await c.post(BASE.format(pid=pid))
        assert up.status_code == 200, up.text
        upgraded = (await _doc(pid)).source
        assert upgraded["site.css"] == (
            "body { background: var(--background); color: var(--foreground) }\n"
            ".btn { background: var(--primary) }\n"
            "@media (max-width: 600px) { body { background: #fdf6ec } }\n"
        )
        assert "h1 { font-family: var(--font-heading) }" in upgraded["index.html"]
        assert upgraded["vendor/lib.css"] == THEMED["vendor/lib.css"]
        r = await c.post(f"{BASE.format(pid=pid)}/revert")
    assert r.status_code == 200, r.text
    doc = await _doc(pid)
    assert doc.engine == "html"
    assert doc.source == THEMED  # the original CSS, byte for byte


async def _upgraded_without_rewrite(monkeypatch, source: dict) -> str:
    """An html site upgraded the way it was before the CSS rewrite existed."""
    from pocketpaw_ee.sites import site_token_rewrite

    pid = await _pocket(source=source)
    with monkeypatch.context() as m:
        m.setattr(site_token_rewrite, "rewrite_source", lambda src, theme, **kw: dict(src))
        async with _client(_app()) as c:
            assert (await c.post(BASE.format(pid=pid))).status_code == 200
    assert (await _doc(pid)).source["site.css"] == source["site.css"]
    return pid


async def test_rerun_rewrites_an_already_upgraded_site_and_undo_still_works(
    beanie_test_db, starter, stores, monkeypatch
):
    pid = await _upgraded_without_rewrite(monkeypatch, THEMED)
    async with _client(_app()) as c:
        r = await c.post(f"{BASE.format(pid=pid)}/tokens")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pocket_id"] == pid
        assert body["changed"] is True
        assert body["rewritten"] == ["index.html", "site.css"]
        assert body["verification"]["status"] == "passed"
        doc = await _doc(pid)
        assert "background: var(--background)" in doc.source["site.css"]
        # Running it again finds nothing left to do and saves nothing.
        again = await c.post(f"{BASE.format(pid=pid)}/tokens")
        assert again.status_code == 200, again.text
        assert again.json()["changed"] is False
        assert again.json()["rewritten"] == []
        # Undo still restores the source from before the upgrade, exactly.
        back = await c.post(f"{BASE.format(pid=pid)}/revert")
    assert back.status_code == 200, back.text
    doc = await _doc(pid)
    assert doc.engine == "html"
    assert doc.source == THEMED


async def test_rerun_after_an_edit_keeps_undo_stale(beanie_test_db, starter, stores, monkeypatch):
    pid = await _upgraded_without_rewrite(monkeypatch, THEMED)
    await project_tools.write_files(
        workspace_id="ws1", user_id="u1", pocket_id=pid, files={"about.html": "<p>new</p>"}
    )
    async with _client(_app()) as c:
        assert (await c.post(f"{BASE.format(pid=pid)}/tokens")).json()["changed"] is True
        r = await c.post(f"{BASE.format(pid=pid)}/revert")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "sites.upgrade_revert_stale"


async def test_rerun_refuses_a_site_not_on_the_static_starter(beanie_test_db, starter, stores):
    pid = await _pocket()
    async with _client(_app()) as c:
        r = await c.post(f"{BASE.format(pid=pid)}/tokens")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "sites.tokens_unsupported"
    assert (await _doc(pid)).source == HTML


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_a_non_member_is_403(beanie_test_db, starter, stores) -> None:
    pid = await _pocket()
    async with _client(_app(user_id="stranger", member_of=[])) as c:
        for url in (
            BASE.format(pid=pid),
            f"{BASE.format(pid=pid)}/revert",
            f"{BASE.format(pid=pid)}/tokens",
        ):
            assert (await c.post(url)).status_code == 403
    assert (await _doc(pid)).engine == "html"


async def test_another_workspace_is_404(beanie_test_db, starter, stores) -> None:
    pid = await _pocket()
    async with _client(_app(user_id="u2", workspace_id="ws2")) as c:
        r = await c.post(BASE.format(pid=pid))
    assert r.status_code == 404, r.text
    assert (await _doc(pid)).engine == "html"


async def test_a_private_pocket_refuses_another_member(beanie_test_db, starter, stores) -> None:
    pid = await _pocket()
    await (await _doc(pid)).set({"visibility": "private"})
    async with _client(_app(user_id="intruder")) as c:
        r = await c.post(BASE.format(pid=pid))
    assert r.status_code in (403, 404), r.text
    assert (await _doc(pid)).engine == "html"


def test_both_routes_carry_the_guards() -> None:
    from pocketpaw_ee.sites.router import router

    paths = {
        "/sites/by-pocket/{pocket_id}/upgrade",
        "/sites/by-pocket/{pocket_id}/upgrade/revert",
        "/sites/by-pocket/{pocket_id}/upgrade/tokens",
    }
    routes = [r for r in router.routes if getattr(r, "path", "") in paths]
    assert len(routes) == 3
    for route in routes:
        deps = [repr(d.call) for d in route.dependant.dependencies]
        assert any("request_context" in d for d in deps), (route.path, deps)
        assert any("require_action" in d for d in deps), (route.path, deps)
        assert any("require_plan_feature" in d for d in deps), (route.path, deps)
