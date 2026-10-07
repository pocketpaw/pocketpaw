# tests/ee/sites/test_project_files_routes.py — the HTTP files routes of a project site
# (``sites/files_router.py``), end to end against a mongomock pocket: the auth matrix
# (no session, no membership, another workspace, a private pocket), path safety, the
# size caps, the non-project refusal, exact-once patching, and that a write changes the
# content hash and queues that hash's draft build. Only the arq enqueue is stubbed.
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.pockets import service as pockets_service  # noqa: E402
from pocketpaw_ee.sites import build_job, project_build, project_tools  # noqa: E402

SOURCE = {
    "package.json": json.dumps({"name": "app", "dependencies": {"astro": "7.3.0"}}),
    "bun.lock": "{}",
    "src/pages/index.astro": "<h1>Hello</h1>\n",
    "AGENTS.md": "# map\n",
}
BASE = "/api/v1/sites/by-pocket/{pid}/files"


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
    from pocketpaw_ee.sites.files_router import router

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


@pytest.fixture()
def enqueued(monkeypatch) -> list[str]:
    """Record the content hash of every queued draft build (no Redis)."""
    hashes: list[str] = []

    async def _enqueue(*, pocket_id, content_hash, engine, generator_input, **_kw):
        assert engine == "project"
        hashes.append(content_hash)
        return SimpleNamespace(
            status="queued", job_id=f"site-preview-{pocket_id}-{content_hash}", reason=None
        )

    monkeypatch.setattr(build_job, "enqueue_preview_build", _enqueue)
    return hashes


async def _pocket(engine: str = "project", source: dict | None = None, **kw) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=kw.get("workspace_id", "ws1"),
        owner_id=kw.get("owner_id", "u1"),
        name="Proj",
        type_="site",
        pattern="landing",
        engine=engine,
        source=dict(source or SOURCE),
        trusted=True,
    )
    assert err is None
    return pocket_id


async def _source(pocket_id: str) -> dict:
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.pocket import Pocket

    return (await Pocket.get(ObjectId(pocket_id))).source


# ---------------------------------------------------------------------------
# Happy path + the shapes the Code view reads
# ---------------------------------------------------------------------------


async def test_list_read_write_patch_delete(beanie_test_db, enqueued) -> None:
    pid = await _pocket()
    url = BASE.format(pid=pid)
    async with _client(_app()) as c:
        r = await c.get(url, params={"prefix": "src/"})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "pocket_id": pid,
            "files": [{"path": "src/pages/index.astro", "size": 15}],
            "file_count": 1,
        }

        r = await c.get(f"{url}/content", params={"path": "AGENTS.md"})
        assert r.status_code == 200, r.text
        assert r.json() == {"path": "AGENTS.md", "size": 6, "content": "# map\n"}

        r = await c.put(url, json={"files": {"src/pages/about.astro": "<h1>About</h1>\n"}})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["written"] == ["src/pages/about.astro"]
        assert body["created"] == ["src/pages/about.astro"]
        assert body["verification"]["status"] == "pending"
        assert body["verification"]["job_id"].startswith(f"site-preview-{pid}-")

        r = await c.post(
            f"{url}/patch",
            json={"path": "src/pages/about.astro", "edits": [{"old": "About", "new": "Team"}]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["path"] == "src/pages/about.astro"

        r = await c.request("DELETE", url, json={"paths": ["src/pages/index.astro"]})
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == ["src/pages/index.astro"]

    source = await _source(pid)
    assert source["src/pages/about.astro"] == "<h1>Team</h1>\n"
    assert "src/pages/index.astro" not in source


async def test_a_write_bumps_the_hash_and_queues_that_build(beanie_test_db, enqueued) -> None:
    pid = await _pocket()
    before = project_build.project_content_hash(SOURCE)
    async with _client(_app()) as c:
        r = await c.put(BASE.format(pid=pid), json={"files": {"src/x.ts": "export {}\n"}})
    assert r.status_code == 200, r.text
    after = project_build.project_content_hash(await _source(pid))
    assert after != before
    assert enqueued == [after]
    assert r.json()["verification"]["job_id"] == f"site-preview-{pid}-{after}"


async def test_a_dependency_change_drops_the_lockfile(beanie_test_db, enqueued) -> None:
    pid = await _pocket()
    pkg = json.dumps({"name": "app", "dependencies": {"astro": "7.3.0", "zod": "4.1.0"}})
    async with _client(_app()) as c:
        r = await c.put(BASE.format(pid=pid), json={"files": {"package.json": pkg}})
    assert r.json()["lockfile_removed"] == ["bun.lock"]
    assert "bun.lock" not in await _source(pid)


async def test_patch_needs_exactly_one_match(beanie_test_db, enqueued) -> None:
    pid = await _pocket(source={**SOURCE, "a.ts": "x\nx\n"})
    async with _client(_app()) as c:
        r = await c.post(
            f"{BASE.format(pid=pid)}/patch",
            json={"path": "a.ts", "edits": [{"old": "x", "new": "y"}]},
        )
        assert r.status_code == 422, r.text
        r = await c.post(
            f"{BASE.format(pid=pid)}/patch",
            json={"path": "a.ts", "edits": [{"old": "zzz", "new": "y"}]},
        )
        assert r.status_code == 422, r.text
    assert (await _source(pid))["a.ts"] == "x\nx\n"
    assert enqueued == []


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_a_non_project_pocket_is_422_on_every_route(beanie_test_db, enqueued) -> None:
    pid = await _pocket(engine="html", source={"index.html": "<p>a</p>"})
    url = BASE.format(pid=pid)
    async with _client(_app()) as c:
        responses = [
            await c.get(url),
            await c.get(f"{url}/content", params={"path": "index.html"}),
            await c.put(url, json={"files": {"index.html": "<p>b</p>"}}),
            await c.post(
                f"{url}/patch", json={"path": "index.html", "edits": [{"old": "a", "new": "b"}]}
            ),
            await c.request("DELETE", url, json={"paths": ["index.html"]}),
        ]
    for r in responses:
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "sites.not_a_project"
    assert await _source(pid) == {"index.html": "<p>a</p>"}


@pytest.mark.parametrize(
    "path",
    [
        "../escape.ts",
        "/abs.ts",
        "C:/win.ts",
        "src\\a.ts",
        ".env",
        ".dev.vars",
        "node_modules/x.js",
        ".git/config",
        ".paw/out/a.js",
        "paw-build.json",
        "a\x00b",
    ],
)
async def test_path_safety(beanie_test_db, enqueued, path) -> None:
    pid = await _pocket()
    async with _client(_app()) as c:
        r = await c.put(BASE.format(pid=pid), json={"files": {"ok.ts": "1", path: "x"}})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "sites.project_bad_path"
        r = await c.post(
            f"{BASE.format(pid=pid)}/patch",
            json={"path": path, "edits": [{"old": "a", "new": "b"}]},
        )
        assert r.status_code == 422, r.text
        r = await c.request("DELETE", BASE.format(pid=pid), json={"paths": [path]})
        assert r.status_code == 422, r.text
    assert await _source(pid) == SOURCE
    assert enqueued == []


async def test_caps(beanie_test_db, enqueued, monkeypatch) -> None:
    from pocketpaw_ee.sites import project_build as pb

    pid = await _pocket()
    url = BASE.format(pid=pid)
    monkeypatch.setattr(project_tools, "MAX_WRITE_FILE_BYTES", 8)
    async with _client(_app()) as c:
        r = await c.put(url, json={"files": {"big.ts": "123456789"}})
        assert r.status_code == 422 and r.json()["error"]["code"] == "sites.project_file_too_large"
        monkeypatch.setattr(project_tools, "MAX_WRITE_FILE_BYTES", 100)
        monkeypatch.setattr(project_tools, "MAX_WRITE_CALL_BYTES", 10)
        r = await c.put(url, json={"files": {"a.ts": "123456", "b.ts": "123456"}})
        assert r.status_code == 422 and r.json()["error"]["code"] == "sites.project_call_too_large"
        monkeypatch.setattr(project_tools, "MAX_WRITE_CALL_BYTES", 1000)
        monkeypatch.setattr(pb, "MAX_SOURCE_FILES", len(SOURCE))
        r = await c.put(url, json={"files": {"new.ts": "1"}})
        assert r.status_code == 422 and r.json()["error"]["code"] == "sites.project_too_large"
    assert await _source(pid) == SOURCE


async def test_package_json_and_missing_files(beanie_test_db, enqueued) -> None:
    pid = await _pocket()
    url = BASE.format(pid=pid)
    async with _client(_app()) as c:
        r = await c.request("DELETE", url, json={"paths": ["package.json"]})
        assert r.status_code == 422
        r = await c.request("DELETE", url, json={"paths": ["AGENTS.md", "gone.ts"]})
        assert r.status_code == 404
        r = await c.get(f"{url}/content", params={"path": "gone.ts"})
        assert r.status_code == 404
    assert await _source(pid) == SOURCE


# ---------------------------------------------------------------------------
# Auth matrix
# ---------------------------------------------------------------------------


async def _every_route(c: AsyncClient, pid: str) -> list:
    url = BASE.format(pid=pid)
    return [
        await c.get(url),
        await c.get(f"{url}/content", params={"path": "AGENTS.md"}),
        await c.put(url, json={"files": {"x.ts": "1"}}),
        await c.post(
            f"{url}/patch", json={"path": "AGENTS.md", "edits": [{"old": "map", "new": "m"}]}
        ),
        await c.request("DELETE", url, json={"paths": ["AGENTS.md"]}),
    ]


async def test_no_session_is_401(beanie_test_db, enqueued) -> None:
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.files_router import router

    pid = await _pocket()
    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    async with _client(app) as c:
        for r in await _every_route(c, pid):
            assert r.status_code == 401, r.text
    assert await _source(pid) == SOURCE


async def test_a_non_member_is_403(beanie_test_db, enqueued) -> None:
    pid = await _pocket()
    async with _client(_app(user_id="stranger", member_of=[])) as c:
        for r in await _every_route(c, pid):
            assert r.status_code == 403, r.text
    assert await _source(pid) == SOURCE


async def test_another_workspace_is_404(beanie_test_db, enqueued) -> None:
    pid = await _pocket()
    async with _client(_app(user_id="u2", workspace_id="ws2")) as c:
        for r in await _every_route(c, pid):
            assert r.status_code == 404, r.text
    assert await _source(pid) == SOURCE


async def test_a_private_pocket_refuses_another_member(beanie_test_db, enqueued) -> None:
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.pocket import Pocket

    pid = await _pocket()
    doc = await Pocket.get(ObjectId(pid))
    await doc.set({"visibility": "private"})
    async with _client(_app(user_id="intruder")) as c:
        for r in await _every_route(c, pid):
            assert r.status_code in (403, 404), r.text
    assert await _source(pid) == SOURCE


async def test_every_route_carries_the_guards() -> None:
    from pocketpaw_ee.sites.files_router import router

    routes = [r for r in router.routes if getattr(r, "path", "").startswith("/sites/")]
    assert len(routes) == 5
    for route in routes:
        deps = [repr(d.call) for d in route.dependant.dependencies]
        assert any("request_context" in d for d in deps), (route.path, deps)
        assert any("require_action" in d for d in deps), (route.path, deps)
        assert any("require_plan_feature" in d for d in deps), (route.path, deps)
