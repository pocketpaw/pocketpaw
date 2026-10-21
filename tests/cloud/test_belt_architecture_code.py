# tests/cloud/test_belt_architecture_code.py — ``GET /belt/architecture/code``.
#
# Real git through ``test_belt_architecture_read``'s ``Origin`` fixture: files are
# listed at the read's sha by the component's own ``paths`` (a parent's ``src/``
# also lists a child's ``src/ui/``), with lang, loc (0 empty, ``None`` binary) and
# a GitHub url pinned to the sha. Also: ``expired``, ``no_paths``, the 404, the
# open-PR join through a fake gh (a failed gh keeps the files, uncached) and the
# route's RBAC. File mode (``&file=``): symbols from a fixture world-model in a
# tmp loom dir (order, callers, url), the excerpt window, 404 off the listing,
# and empty symbols for a missing or oversized model. Without loom symbols, a
# Swift or TS file's declarations are read from the file itself (comments and
# strings skipped, capped).

from __future__ import annotations

import json
import os
import re

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_code as ac  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_read as ar  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_symbols as asy  # noqa: E402
from pocketpaw_ee.cloud.belt import file_symbols as fs  # noqa: E402
from pocketpaw_ee.cloud.belt.service import BeltConsoleError  # noqa: E402

from tests.cloud.test_belt_architecture_read import _no_run_store, origin  # noqa: E402, F401
from tests.cloud.test_belt_console import _build_app  # noqa: E402

COMPS = [
    {"id": "core", "paths": ["src/"]},
    {"id": "ui", "paths": ["src/ui/"]},
    {"id": "gone", "paths": ["old/**"]},
    {"id": "bare"},
    {"id": "mac", "paths": ["mac/"]},
]
MODEL = {
    "scope": "app",
    "model": {
        "systems": [{"id": "app", "containers": [{"id": "web", "components": COMPS}]}],
        "relationships": [],
    },
}
SLUG = "acme/app"
SWIFT = """import SwiftUI
/*
struct Hidden {}
*/
// func commented() {}
@MainActor public final class Store {
    let label = "{ func notAFunc()"
    @Published private(set) var items: [String] = []
    init(items: [String]) { self.items = items }
    class func make() -> Store { Store(items: []) }
    func add(_ item: String) {
        let local = 1
    }
}
enum Mode { case a }
extension Store.Mode {}
"""
TS = """/*
class Doc {}
*/
// export function gone() {}
export function build(a: number) { return a }
export default class Panel {}
export interface Props { a: string }
type Id = string;
export const LIMIT = 3;
const hidden = "function fake() {}";
"""


@pytest.fixture
def repo(origin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ac, "_CACHE", {})
    files = {
        "src/ui/a.py": "x = 1\ny = 2\nz = 3\n",
        "src/ui/long.py": "".join(f"line {n}\n" for n in range(1, 101)),
        "src/ui/empty.ts": "",
        "src/ui/my file.md": "# hi\n",
        "src/api/b.go": "package b\n",
        "README.md": "top\n",
        "mac/Store.swift": SWIFT,
        "mac/panel.ts": TS,
        "mac/feed.txt": "a\x0cb\nc\n",
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
        "src/ui/long.py": ("python", 100, []),
    }
    urls = {f["path"]: f["url"] for f in body["files"]}
    assert urls["src/ui/my file.md"] == f"https://github.com/{SLUG}/blob/{repo}/src/ui/my%20file.md"


async def test_a_parent_glob_also_lists_a_childs_files(repo, monkeypatch):
    with_read(monkeypatch)
    paths = {f["path"] for f in (await ac.read_code("ws1", "repo", "core"))["files"]}
    assert paths == {"src/ui/a.py", "src/ui/empty.ts", "src/ui/my file.md", "src/ui/logo.png",
                     "src/ui/long.py", "src/api/b.go"}  # fmt: skip


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
    assert len(body["files"]) == 5 and all(f["prs"] == [] for f in body["files"])
    await ac.read_code("ws1", "repo", "ui", gh=gh)
    assert len(calls) == 2


def test_route_serves_files_and_404s_an_unknown_component(repo):
    with TestClient(_build_app(role="member")) as client:
        ok = client.get("/api/v1/belt/architecture/code", params={"repo": "r", "component": "ui"})
        missing = client.get(
            "/api/v1/belt/architecture/code", params={"repo": "r", "component": "nope"}
        )
    assert ok.status_code == 200 and ok.json()["sha"] == repo and len(ok.json()["files"]) == 5
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "belt.component_not_found"


def sym(path, name, line, kind="function"):
    attrs = {"kind": kind, "line": str(line)}
    return {"id": f"c:symbol:{path}#{name}", "kind": "symbol", "path": path, "symbol": name,
            "attrs": attrs}  # fmt: skip


def calls(src, dst, path="src/ui/long.py"):
    return {"from": f"c:symbol:{path}#{src}", "to": f"c:symbol:{path}#{dst}", "type": "calls"}


@pytest.fixture
def loom(repo, tmp_path, monkeypatch):
    """A world-model for the clone (dir ``c``): ``hot`` has two callers (one
    edge duplicated), ``late`` and ``warm`` tie on one (line breaks it), and
    ``cold``'s only caller is a component, which does not count."""
    monkeypatch.setattr(asy, "_CACHE", {})
    monkeypatch.setenv("POCKETPAW_FACTORY_LOOM_DIR", str(tmp_path / "loom"))
    (tmp_path / "loom").mkdir()
    long = "src/ui/long.py"
    ents = [sym(long, "cold", 5), sym(long, "hot", 60), sym(long, "warm", 30, "class"),
            sym(long, "late", 10), sym("src/ui/a.py", "x", 1),
            {"id": "c:file:src/ui/long.py", "kind": "file", "path": long}]  # fmt: skip
    edges = [calls("cold", "hot"), calls("warm", "hot"), calls("warm", "hot"),
             calls("hot", "warm"), calls("hot", "late"),
             {"from": "c:component:x", "to": f"c:symbol:{long}#cold", "type": "calls"},
             {"from": f"c:symbol:{long}#hot", "to": "c:file:x", "type": "composes"}]  # fmt: skip
    model = tmp_path / "loom" / "worldmodel-c.json"
    model.write_text(json.dumps({"schema": "loom/worldmodel/v1", "entities": ents,
                                 "edges": edges}))  # fmt: skip
    return model


async def test_a_files_symbols_sort_by_callers_with_a_line_url(loom, repo, monkeypatch):
    with_read(monkeypatch)
    body = await ac.read_file("ws1", "repo", "ui", "src/ui/long.py")
    assert (body["sha"], body["path"], body["lang"]) == (repo, "src/ui/long.py", "python")
    got = [(s["name"], s["kind"], s["line"], s["callers"]) for s in body["symbols"]]
    assert got == [("hot", "function", 60, 2), ("late", "function", 10, 1),
                   ("warm", "class", 30, 1), ("cold", "function", 5, 0)]  # fmt: skip
    assert body["symbols_skipped"] is None
    url = f"https://github.com/{SLUG}/blob/{repo}/src/ui/long.py#L60"
    assert body["symbols"][0]["url"] == url


async def test_the_excerpt_is_forty_lines_from_the_top_or_from_line(loom, repo):
    top = (await ac.read_file("ws1", "repo", "ui", "src/ui/long.py"))["excerpt"]
    assert (top["startLine"], top["language"], len(top["before"])) == (1, "python", 40)
    assert top["before"][0] == "line 1" and top["before"][-1] == "line 40"
    at = (await ac.read_file("ws1", "repo", "ui", "src/ui/long.py", line=70))["excerpt"]
    assert at["startLine"] == 70 and at["before"] == [f"line {n}" for n in range(70, 101)]


async def test_a_binary_or_empty_file_has_no_excerpt(loom, repo):
    for path in ("src/ui/logo.png", "src/ui/empty.ts"):
        assert (await ac.read_file("ws1", "repo", "ui", path))["excerpt"] is None


async def test_a_file_outside_the_component_is_404(loom, repo):
    for path in ("src/api/b.go", "README.md", "../c/src/ui/a.py", "/etc/passwd"):
        with pytest.raises(BeltConsoleError) as err:
            await ac.read_file("ws1", "repo", "ui", path)
        assert (err.value.status_code, err.value.code) == (404, "belt.file_not_found")


async def test_a_missing_model_gives_no_symbols(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("POCKETPAW_FACTORY_LOOM_DIR", str(tmp_path / "none"))
    body = await ac.read_file("ws1", "repo", "ui", "src/ui/long.py")
    assert (body["symbols"], body["symbols_skipped"]) == ([], "no model")
    assert body["excerpt"]["startLine"] == 1


async def test_an_oversized_model_is_skipped_unread(loom, repo, monkeypatch):
    monkeypatch.setattr(asy, "MAX_WORLD_MODEL_BYTES", 10)
    monkeypatch.setattr(asy, "_load", lambda p: pytest.fail("read an oversized model"))
    body = await ac.read_file("ws1", "repo", "ui", "src/ui/long.py")
    assert (body["symbols"], body["symbols_skipped"]) == ([], "too large")


async def test_the_index_is_cached_per_mtime(loom, repo, monkeypatch):
    loads = []
    real = asy._load
    monkeypatch.setattr(asy, "_load", lambda p: loads.append(p) or real(p))
    await ac.read_file("ws1", "repo", "ui", "src/ui/long.py")
    await ac.read_file("ws1", "repo", "ui", "src/ui/a.py")
    assert len(loads) == 1
    st = loom.stat()
    os.utime(loom, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # a regenerated model
    await ac.read_file("ws1", "repo", "ui", "src/ui/a.py")
    assert len(loads) == 2


async def test_the_model_named_by_the_c4_scope_wins(loom, repo):
    # MODEL's scope is "app", the clone's dir is "c": the scope's model is read first.
    (loom.parent / "worldmodel-app.json").write_text(
        json.dumps({"entities": [sym("src/ui/long.py", "scoped", 3)], "edges": []})
    )
    body = await ac.read_file("ws1", "repo", "ui", "src/ui/long.py")
    assert [s["name"] for s in body["symbols"]] == ["scoped"]


def test_route_serves_a_file_and_404s_one_outside_the_component(loom, repo):
    url, q = "/api/v1/belt/architecture/code", {"repo": "r", "component": "ui"}
    with TestClient(_build_app(role="member")) as client:
        ok = client.get(url, params={**q, "file": "src/ui/long.py", "line": 70})
        missing = client.get(url, params={**q, "file": "README.md"})
        bad = client.get(url, params={**q, "file": "src/ui/long.py", "line": -1})
    assert ok.status_code == 200 and ok.json()["excerpt"]["startLine"] == 70
    assert [s["name"] for s in ok.json()["symbols"]][0] == "hot"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "belt.file_not_found"
    assert bad.status_code == 422


async def test_without_loom_symbols_a_swift_file_is_read_directly(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("POCKETPAW_FACTORY_LOOM_DIR", str(tmp_path / "none"))
    with_read(monkeypatch)
    body = await ac.read_file("ws1", "repo", "mac", "mac/Store.swift")
    got = [(s["kind"], s["name"], s["line"]) for s in body["symbols"]]
    assert got == [("class", "Store", 6), ("property", "label", 7), ("property", "items", 8),
                   ("init", "init", 9), ("function", "make", 10), ("function", "add", 11),
                   ("enum", "Mode", 15), ("extension", "Store.Mode", 16)]  # fmt: skip
    assert (body["symbols_source"], body["symbols_skipped"]) == ("file", "no model")
    assert all(s["callers"] is None for s in body["symbols"])
    assert body["symbols"][0]["url"] == f"https://github.com/{SLUG}/blob/{repo}/mac/Store.swift#L6"


async def test_without_loom_symbols_a_ts_file_is_read_directly(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("POCKETPAW_FACTORY_LOOM_DIR", str(tmp_path / "none"))
    body = await ac.read_file("ws1", "repo", "mac", "mac/panel.ts")
    got = [(s["kind"], s["name"], s["line"]) for s in body["symbols"]]
    assert got == [("function", "build", 5), ("class", "Panel", 6), ("interface", "Props", 7),
                   ("type", "Id", 8), ("const", "LIMIT", 9)]  # fmt: skip
    assert body["symbols_source"] == "file"


async def test_loom_symbols_win_and_say_so(loom, repo):
    body = await ac.read_file("ws1", "repo", "ui", "src/ui/long.py")
    assert body["symbols_source"] == "loom"


async def test_the_fallback_is_capped(repo, monkeypatch):
    monkeypatch.setattr(fs, "MAX_SYMBOLS", 2)
    assert len(fs.file_symbols("def a(): pass\ndef b(): pass\ndef c(): pass\n", "python")) == 2


async def test_the_excerpt_splits_on_newlines_only(repo):
    excerpt = (await ac.read_file("ws1", "repo", "mac", "mac/feed.txt"))["excerpt"]
    assert excerpt["before"] == ["a\x0cb", "c"]


async def test_an_overlong_scope_is_unreadable_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("POCKETPAW_FACTORY_LOOM_DIR", str(tmp_path))
    assert await asy.symbols_for(tmp_path, "a.py", "a" * 300) == ([], "no model")
    monkeypatch.setattr(asy, "_SCOPE", re.compile(r"a+"))  # past the cap: the stat fails
    assert await asy.symbols_for(tmp_path, "a.py", "a" * 300) == ([], "unreadable")
