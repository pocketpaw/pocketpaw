# tests/cloud/test_belt_architecture_history.py — the architecture viewer's
# history: ``GET /belt/architecture/history?repo=`` and ``?at=<sha>`` on the
# architecture read (and on the Code tab's read).
#
# Real git on ``test_belt_architecture_read``'s fixture: three model commits and
# one that leaves ``docs/c4/`` alone on ``origin/dev``. Pins the history's order,
# content and limit; that ``at`` (full or short) serves the old model with
# ``live: None``, no background task and a per-commit cache; that a malformed sha,
# an unknown one and one off the trunk are 404 ``belt.commit_not_found``; and the
# routes, including ``at=`` empty reading the live path.

from __future__ import annotations

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
    rows = await ah.read_history("ws1", "repo")
    assert [r["sha"] for r in rows] == origin.shas[::-1]
    assert [r["subject"] for r in rows] == ["model v3", "model v2", "model v1"]
    assert all(r["date"][:2] == "20" and "T" in r["date"] for r in rows)


async def test_history_stops_at_its_limit(origin, monkeypatch):
    monkeypatch.setattr(ah, "HISTORY_LIMIT", 2)
    assert [r["sha"] for r in await ah.read_history("ws1", "repo")] == origin.shas[:0:-1]


async def test_at_serves_the_old_model_with_no_live_and_no_background_task(origin):
    body = await ah.read_at("ws1", "repo", origin.shas[0][:7])
    assert body["sha"] == origin.shas[0]
    assert body["model"] == {"scope": "v1", "model": MODEL["model"]}
    assert (body["live"], body["stale"], body["flows"], body["loom"]) == (None, False, [], None)
    assert set(body) == {
        "repo", "slug", "sha", "stale", "read_at", "model", "flows", "warnings", "loom", "live",
    }  # fmt: skip
    assert not ar._TASKS and not ar._CACHE
    assert await ah.read_at("ws1", "repo", origin.shas[0]) is body  # cached per commit


@pytest.mark.parametrize("at", ["zz12345", "--all", "abc", "deadbeefdeadbeef", "HEAD"])
async def test_a_malformed_or_unknown_sha_is_404(origin, at):
    with pytest.raises(BeltConsoleError) as err:
        await ah.read_at("ws1", "repo", at)
    assert (err.value.status_code, err.value.code) == (404, "belt.commit_not_found")


async def test_a_commit_off_the_trunk_is_404(origin):
    git(origin.clone, "commit", "-q", "--allow-empty", "-m", "local only")
    off = git(origin.clone, "rev-parse", "HEAD").strip()
    with pytest.raises(BeltConsoleError) as err:
        await ah.read_at("ws1", "repo", off)
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
