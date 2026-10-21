# tests/cloud/test_belt_architecture_code.py — ``GET /belt/architecture/code``.
#
# Real git through ``test_belt_architecture_read``'s ``Origin`` fixture: files are
# listed at the read's sha by the component's own ``paths`` (a parent's ``src/``
# also lists a child's ``src/ui/``), with lang, loc (0 empty, ``None`` binary) and
# a GitHub url pinned to the sha. Also: ``expired``, ``no_paths``, the 404, the
# open-PR join through a fake gh (a failed gh keeps the files, uncached) and the
# route's RBAC.

from __future__ import annotations

import json

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_code as ac  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_read as ar  # noqa: E402
from pocketpaw_ee.cloud.belt.service import BeltConsoleError  # noqa: E402

from tests.cloud.test_belt_architecture_read import _no_run_store, origin  # noqa: E402, F401
from tests.cloud.test_belt_console import _build_app  # noqa: E402

COMPS = [
    {"id": "core", "paths": ["src/"]},
    {"id": "ui", "paths": ["src/ui/"]},
    {"id": "gone", "paths": ["old/**"]},
    {"id": "bare"},
]
MODEL = {
    "scope": "app",
    "model": {
        "systems": [{"id": "app", "containers": [{"id": "web", "components": COMPS}]}],
        "relationships": [],
    },
}
SLUG = "acme/app"


@pytest.fixture
def repo(origin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ac, "_CACHE", {})
    files = {
        "src/ui/a.py": "x = 1\ny = 2\nz = 3\n",
        "src/ui/empty.ts": "",
        "src/ui/my file.md": "# hi\n",
        "src/api/b.go": "package b\n",
        "README.md": "top\n",
    }
    for name, text in files.items():
        (origin.work / name).parent.mkdir(parents=True, exist_ok=True)
        (origin.work / name).write_text(text)
    (origin.work / "src/ui/logo.png").write_bytes(b"\x89PNG\x00\x01\x02")
    sha = origin.push(MODEL)
    origin.cloned()
    return sha


def with_read(monkeypatch, slug=SLUG, prs=()):
    """Give the real read a GitHub slug and open PRs on its live block."""
    real = ar.read_architecture

    async def read(workspace_id, repo):
        body = dict(await real(workspace_id, repo))
        rows = [{"number": n} for n in prs]
        live = {"components": {}, "repo": {"prs": rows}}
        return {**body, "slug": slug, "live": live}

    monkeypatch.setattr(ar, "read_architecture", read)


async def test_files_are_listed_at_the_reads_sha_with_lang_loc_and_url(repo, monkeypatch):
    with_read(monkeypatch)
    body = await ac.read_code("ws1", "repo", "ui")
    assert (body["sha"], body["expired"], body["no_paths"]) == (repo, False, False)
    got = {f["path"]: (f["lang"], f["loc"], f["prs"]) for f in body["files"]}
    assert got == {
        "src/ui/a.py": ("python", 3, []),
        "src/ui/empty.ts": ("typescript", 0, []),
        "src/ui/my file.md": ("markdown", 1, []),
        "src/ui/logo.png": ("png", None, []),
    }
    urls = {f["path"]: f["url"] for f in body["files"]}
    assert urls["src/ui/my file.md"] == f"https://github.com/{SLUG}/blob/{repo}/src/ui/my%20file.md"


async def test_a_parent_glob_also_lists_a_childs_files(repo, monkeypatch):
    with_read(monkeypatch)
    paths = {f["path"] for f in (await ac.read_code("ws1", "repo", "core"))["files"]}
    assert paths == {"src/ui/a.py", "src/ui/empty.ts", "src/ui/my file.md", "src/ui/logo.png",
                     "src/api/b.go"}  # fmt: skip


async def test_no_slug_means_no_url(repo):
    body = await ac.read_code("ws1", "repo", "ui")
    assert body["files"] and all(f["url"] is None for f in body["files"])


async def test_paths_matching_nothing_are_expired(repo):
    body = await ac.read_code("ws1", "repo", "gone")
    assert body == {"sha": repo, "files": [], "expired": True, "no_paths": False}


async def test_a_component_without_paths_says_so(repo):
    body = await ac.read_code("ws1", "repo", "bare")
    assert body == {"sha": repo, "files": [], "expired": False, "no_paths": True}


async def test_an_unknown_component_is_404(repo):
    with pytest.raises(BeltConsoleError) as err:
        await ac.read_code("ws1", "repo", "web")  # a container, not a component
    assert (err.value.status_code, err.value.code) == (404, "belt.component_not_found")


async def test_open_prs_join_by_file_and_the_body_is_cached(repo, monkeypatch):
    with_read(monkeypatch, prs=(7, 9))
    calls = []

    async def gh(argv):
        calls.append(argv)
        a, readme = {"path": "src/ui/a.py"}, {"path": "README.md"}
        rows = [{"number": 9, "files": [a]}, {"number": 7, "files": [a, readme]}]
        return 0, json.dumps(rows), ""

    first = await ac.read_code("ws1", "repo", "ui", gh=gh)
    prs = {f["path"]: f["prs"] for f in first["files"]}
    assert prs["src/ui/a.py"] == [7, 9] and prs["src/ui/empty.ts"] == []
    assert calls[0][:5] == ["gh", "pr", "list", "-R", SLUG]
    assert await ac.read_code("ws1", "repo", "ui", gh=gh) is first and len(calls) == 1


async def test_a_failed_gh_read_keeps_the_files_and_is_not_cached(repo, monkeypatch):
    with_read(monkeypatch, prs=(7,))
    calls = []

    async def gh(argv):
        calls.append(argv)
        return 1, "", "rate limited"

    body = await ac.read_code("ws1", "repo", "ui", gh=gh)
    assert len(body["files"]) == 4 and all(f["prs"] == [] for f in body["files"])
    await ac.read_code("ws1", "repo", "ui", gh=gh)
    assert len(calls) == 2


def test_route_serves_files_and_404s_an_unknown_component(repo):
    with TestClient(_build_app(role="member")) as client:
        ok = client.get("/api/v1/belt/architecture/code", params={"repo": "r", "component": "ui"})
        missing = client.get(
            "/api/v1/belt/architecture/code", params={"repo": "r", "component": "nope"}
        )
    assert ok.status_code == 200 and ok.json()["sha"] == repo and len(ok.json()["files"]) == 4
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "belt.component_not_found"
