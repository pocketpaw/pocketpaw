# tests/cloud/test_belt_architecture_history.py — the architecture viewer's
# history: ``GET /belt/architecture/history?repo=`` and ``?at=<sha>`` on the
# architecture read (and on the Code tab's read).
#
# Real git on ``test_belt_architecture_read``'s fixture: three model commits and
# one that leaves ``docs/c4/`` alone on ``origin/dev``. Pins the history's order,
# content and limit; that ``at`` (full or short) serves the old model with
# ``live: None``, no background task and a per-commit cache; that a malformed sha,
# an unknown one and one off the trunk are 404 ``belt.commit_not_found``; and the
# routes, including ``at=`` empty reading the live path. Each history row's
# ``+n -m ~k`` counts (against the parent, computed once per commit); ``diff=``
# returning the read plus ``diff`` with the removed elements' own data, behind
# the same sha guard; git missing or timing out as 503; and the drop-all at
# ``_CACHE_MAX``.

from __future__ import annotations

import copy

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_code as ac  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_history as ah  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_read as ar  # noqa: E402
from pocketpaw_ee.cloud.belt.service import BeltConsoleError  # noqa: E402

from tests.cloud.test_belt_architecture_read import MODEL, Origin, git  # noqa: E402
from tests.cloud.test_belt_console import _build_app  # noqa: E402


@pytest.fixture
def origin(monkeypatch, tmp_path):
    for name in ("_CACHE", "_LOCKS", "_TASKS"):
        monkeypatch.setattr(ar, name, {})
    monkeypatch.setattr(ah, "_CACHE", {})
    monkeypatch.setattr(ah, "_COUNTS", {})
    monkeypatch.setattr(ac, "_CACHE", {})
    o = Origin(tmp_path)
    resolve = lambda r: (o.clone.resolve(), None)  # noqa: E731
    monkeypatch.setattr(ar, "_re_resolve_repo", resolve)
    monkeypatch.setattr(ah, "_re_resolve_repo", resolve)
    o.shas = [o.push({**MODEL, "scope": f"v{i}"}, msg=f"model v{i}") for i in (1, 2, 3)]
    o.push(model=None, msg="no model change")  # an empty commit: nothing under docs/c4
    o.cloned()
    return o


async def test_history_lists_the_trunk_model_commits_newest_first(origin):
    rows = await ah.read_history("repo")
    assert [r["sha"] for r in rows] == origin.shas[::-1]
    assert [r["subject"] for r in rows] == ["model v3", "model v2", "model v1"]
    assert all(r["date"][:2] == "20" and "T" in r["date"] for r in rows)


async def test_history_stops_at_its_limit(origin, monkeypatch):
    monkeypatch.setattr(ah, "HISTORY_LIMIT", 2)
    assert [r["sha"] for r in await ah.read_history("repo")] == origin.shas[:0:-1]


async def test_at_serves_the_old_model_with_no_live_and_no_background_task(origin):
    body = await ah.read_at("repo", origin.shas[0][:7])
    assert body["sha"] == origin.shas[0]
    assert body["model"] == {"scope": "v1", "model": MODEL["model"]}
    assert (body["live"], body["stale"], body["flows"], body["loom"]) == (None, False, [], None)
    assert set(body) == {
        "repo", "slug", "sha", "stale", "read_at", "model", "flows", "warnings", "loom", "live",
    }  # fmt: skip
    assert not ar._TASKS and not ar._CACHE
    assert await ah.read_at("repo", origin.shas[0]) is body  # cached per commit


@pytest.mark.parametrize("at", ["zz12345", "--all", "abc", "deadbeefdeadbeef", "HEAD"])
async def test_a_malformed_or_unknown_sha_is_404(origin, at):
    with pytest.raises(BeltConsoleError) as err:
        await ah.read_at("repo", at)
    assert (err.value.status_code, err.value.code) == (404, "belt.commit_not_found")


async def test_a_commit_off_the_trunk_is_404(origin):
    git(origin.clone, "commit", "-q", "--allow-empty", "-m", "local only")
    off = git(origin.clone, "rev-parse", "HEAD").strip()
    with pytest.raises(BeltConsoleError) as err:
        await ah.read_at("repo", off)
    assert (err.value.status_code, err.value.code) == (404, "belt.commit_not_found")


async def test_the_code_read_at_a_commit_uses_that_sha(origin):
    out = await ac.read_code("ws1", "repo", "ui", at=origin.shas[1])
    assert (out["sha"], out["no_paths"]) == (origin.shas[1], True)
    assert not ar._TASKS


def test_routes_serve_history_and_at_and_404_a_bad_sha(origin, monkeypatch):
    monkeypatch.setattr(ar, "_behind", lambda key, now, read: read.close())
    with TestClient(_build_app(role="member")) as client:
        hist = client.get("/api/v1/belt/architecture/history", params={"repo": "repo"})
        old = client.get("/api/v1/belt/architecture", params={"repo": "repo", "at": origin.shas[0]})
        bad = client.get("/api/v1/belt/architecture", params={"repo": "repo", "at": "nope"})
        now = client.get("/api/v1/belt/architecture", params={"repo": "repo", "at": ""})
    assert hist.status_code == 200 and hist.json()[0]["sha"] == origin.shas[2]
    assert old.status_code == 200 and old.json()["model"]["scope"] == "v1"
    assert old.json()["live"] is None
    assert bad.status_code == 404 and bad.json()["error"]["code"] == "belt.commit_not_found"
    assert now.status_code == 200 and now.json()["model"]["scope"] == "v3"


async def test_history_rows_count_against_the_parent_once_per_commit(origin, monkeypatch):
    rows = await ah.read_history("repo")
    oldest = {"added": 4, "removed": 0, "changed": 0}  # a root commit: app, web, ui, web->api
    assert [{k: r[k] for k in oldest} for r in rows] == [dict.fromkeys(oldest, 0)] * 2 + [oldest]
    reads = []

    async def spy(path, rev):
        reads.append(rev)
        return None, None

    monkeypatch.setattr(ah, "read_repo_model", spy)
    assert await ah.read_history("repo") == rows and not reads  # cached per commit


def _next_model() -> dict:
    """MODEL with ``ui`` gone, ``web`` relabelled, ``api`` added and its relationship."""
    m = copy.deepcopy(MODEL)
    web = m["model"]["systems"][0]["containers"][0]
    web["components"], web["description"] = [], "the site"
    m["model"]["systems"][0]["containers"].append({"id": "api", "name": "API"})
    m["model"]["relationships"] = [{"source": "api", "target": "web"}]
    return m


async def test_diff_returns_the_read_plus_what_changed_since_the_commit(origin, monkeypatch):
    origin.push(_next_model(), msg="model v4")
    git(origin.clone, "fetch", "-q", "origin")
    body = await ah.read_at("repo", git(origin.clone, "rev-parse", "origin/dev").strip())
    out = await ah.read_diff("repo", origin.shas[0], body)
    assert {k: out[k] for k in body} == body and "diff" not in body
    d = out["diff"]
    assert (d["added"], d["removed"]) == (["api"], ["ui"])
    assert d["changed"] == [{"id": "web", "fields": ["description"]}]
    assert d["removed_elements"] == [{"parent": "web", "in": "components", "element": {"id": "ui"}}]
    assert d["relationships"] == {
        "added": [{"source": "api", "target": "web"}],
        "removed": [{"from": "web", "to": "api"}],
    }


@pytest.mark.parametrize("bad", ["--all", "zz12345", "deadbeefdeadbeef"])
async def test_diff_guards_its_sha_like_at_with_its_own_code(origin, bad):
    body = await ah.read_at("repo", origin.shas[2])
    with pytest.raises(BeltConsoleError) as err:
        await ah.read_diff("repo", bad, body)
    assert (err.value.status_code, err.value.code) == (404, "belt.diff_commit_not_found")


@pytest.mark.parametrize("boom", [OSError("no git"), RuntimeError("command timed out")])
async def test_git_missing_or_timing_out_is_503(origin, monkeypatch, boom):
    async def fail(*_a, **_k):
        raise boom

    monkeypatch.setattr(ah, "_git", fail)
    for call in (ah.read_history("repo"), ah.read_at("repo", origin.shas[0])):
        with pytest.raises(BeltConsoleError) as err:
            await call
        assert (err.value.status_code, err.value.code) == (503, "belt.architecture_unavailable")


async def test_the_at_cache_drops_whole_at_its_max(origin, monkeypatch):
    monkeypatch.setattr(ah, "_CACHE_MAX", 2)
    await ah.read_at("repo", origin.shas[0])
    await ah.read_at("repo", origin.shas[1])
    assert len(ah._CACHE) == 2
    await ah.read_at("repo", origin.shas[2])
    assert list(ah._CACHE) == [(str(origin.clone.resolve()), origin.shas[2])]


def test_route_diff_adds_the_diff_and_a_bad_one_is_404(origin, monkeypatch):
    monkeypatch.setattr(ar, "_behind", lambda key, now, read: read.close())
    params = {"repo": "repo", "at": origin.shas[2]}
    with TestClient(_build_app(role="member")) as client:
        ok = client.get("/api/v1/belt/architecture", params={**params, "diff": origin.shas[0]})
        bad = client.get("/api/v1/belt/architecture", params={**params, "diff": "nope"})
        plain = client.get("/api/v1/belt/architecture", params=params)
    assert ok.status_code == 200 and ok.json()["diff"]["added"] == []
    assert ok.json()["model"]["scope"] == "v3"
    assert bad.status_code == 404 and bad.json()["error"]["code"] == "belt.diff_commit_not_found"
    assert "diff" not in plain.json()


def test_route_diff_without_at_compares_the_current_read(origin, monkeypatch):
    monkeypatch.setattr(ar, "_behind", lambda key, now, read: read.close())
    params = {"repo": "repo", "diff": origin.shas[0]}
    with TestClient(_build_app(role="member")) as client:
        now = client.get("/api/v1/belt/architecture", params=params)
    assert now.status_code == 200 and now.json()["model"]["scope"] == "v3"
    assert now.json()["diff"]["added"] == [] and "live" in now.json()
    assert "diff" not in ar._CACHE[next(iter(ar._CACHE))]["body"]  # the cached read is untouched
