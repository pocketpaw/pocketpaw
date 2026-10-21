# tests/cloud/test_belt_architecture_read.py — ``GET /belt/architecture?repo=``.
#
# Real git, no runner fakes: a bare ``origin`` with the C4 model committed on
# ``dev`` (or only ``main``, ripple's trunk), and the clone the reader is bound to.
# A push after the clone proves the read fetches and serves ``origin/dev``, not the
# checkout. Pins the response contract later slices extend (``flows: []``,
# ``loom: None``, ``live: None``), the 404s (no model, an oversized one), the TTL
# cache, the stale fallback when the fetch breaks after one success (503 with
# none), and that ``belt.read`` admits a member and refuses an outsider.

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_read as ar  # noqa: E402
from pocketpaw_ee.cloud.belt import service as belt_service  # noqa: E402
from pocketpaw_ee.cloud.belt.service import BeltConsoleError  # noqa: E402

from tests.cloud.test_belt_console import _build_app  # noqa: E402

MODEL = {
    "version": 1,
    "scope": "app",
    "generated_at": "2026-10-21T00:00:00Z",
    "model": {
        "people": [],
        "systems": [{"id": "app", "containers": [{"id": "web", "components": [{"id": "ui"}]}]}],
        "relationships": [{"from": "web", "to": "api"}],
    },
}


def git(cwd: Path, *args: str) -> str:
    argv = ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args]
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout


class Origin:
    """A bare origin, a work tree that pushes to it, and the bound clone."""

    def __init__(self, tmp: Path, branch: str = "dev"):
        self.bare, self.work, self.clone, self.branch = tmp / "o.git", tmp / "w", tmp / "c", branch
        git(tmp, "init", "-q", "--bare", "-b", branch, str(self.bare))
        git(tmp, "init", "-q", "-b", branch, str(self.work))
        git(self.work, "remote", "add", "origin", str(self.bare))

    def push(self, model: dict | None = MODEL, filler: str = "") -> str:
        c4 = self.work / "docs" / "c4"
        c4.mkdir(parents=True, exist_ok=True)
        if model is not None:
            (c4 / "model.json").write_text(json.dumps(model) + filler)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "--allow-empty", "-m", "c")
        git(self.work, "push", "-q", "origin", self.branch)
        return git(self.work, "rev-parse", "HEAD").strip()

    def cloned(self) -> Path:
        if not self.clone.exists():
            git(self.clone.parent, "clone", "-q", str(self.bare), str(self.clone))
        return self.clone


@pytest.fixture
def origin(monkeypatch, tmp_path):
    for name in ("_CACHE", "_LOCKS"):
        monkeypatch.setattr(ar, name, {})
    o = Origin(tmp_path)
    monkeypatch.setattr(ar, "_re_resolve_repo", lambda r: (o.clone.resolve(), None))
    return o


async def read(t: float = 0.0) -> dict:
    return await ar.read_architecture("ws1", "repo", now=lambda: t)


async def test_a_repo_with_a_model_returns_it_at_origin_dev(origin):
    origin.push()
    origin.cloned()
    sha = origin.push({**MODEL, "scope": "moved"})  # lands after the clone: only a fetch sees it
    body = await read()
    assert set(body) == {
        "repo",
        "slug",
        "sha",
        "stale",
        "read_at",
        "model",
        "flows",
        "loom",
        "live",
    }
    assert (body["sha"], body["stale"], body["slug"]) == (sha, False, "")
    assert body["model"] == {"scope": "moved", "model": MODEL["model"]}
    assert (body["flows"], body["loom"], body["live"]) == ([], None, None)
    assert body["repo"] == str(origin.clone.resolve())


async def test_main_is_read_when_origin_has_no_dev(monkeypatch, tmp_path):
    for name in ("_CACHE", "_LOCKS"):
        monkeypatch.setattr(ar, name, {})
    o = Origin(tmp_path, branch="main")
    monkeypatch.setattr(ar, "_re_resolve_repo", lambda r: (o.clone.resolve(), None))
    sha = o.push()
    o.cloned()
    assert (await read())["sha"] == sha


async def test_no_model_is_404(origin):
    origin.push(model=None)
    origin.cloned()
    with pytest.raises(BeltConsoleError) as err:
        await read()
    assert (err.value.status_code, err.value.code) == (404, "belt.architecture_not_found")


async def test_an_oversized_model_is_unreadable_never_partial(origin, monkeypatch):
    origin.push(filler=" " * 500)
    origin.cloned()
    monkeypatch.setattr(belt_service, "MAX_MODEL_BYTES", 400)
    with pytest.raises(BeltConsoleError) as err:
        await read()
    assert err.value.status_code == 404


async def test_a_read_within_the_ttl_is_the_cached_body(origin):
    origin.push()
    origin.cloned()
    first = await read()
    sha = origin.push({**MODEL, "scope": "next"})
    assert await read(t=59.0) is first
    assert (await read(t=61.0))["sha"] == sha


async def test_a_failed_fetch_after_one_success_serves_stale(origin):
    origin.push()
    clone = origin.cloned()
    git(clone, "remote", "set-url", "origin", str(origin.bare.parent / "gone.git"))
    with pytest.raises(BeltConsoleError) as err:  # nothing read yet
        await read()
    assert err.value.status_code == 503
    git(clone, "remote", "set-url", "origin", str(origin.bare))
    first = await read(t=100.0)
    git(clone, "remote", "set-url", "origin", str(origin.bare.parent / "gone.git"))
    body = await read(t=161.0)
    assert body["stale"] is True and body["model"] == first["model"]
    assert body["sha"] == first["sha"]


def test_route_404_carries_detail_and_its_code(origin):
    origin.push(model=None)
    origin.cloned()
    with TestClient(_build_app(role="member")) as client:
        res = client.get("/api/v1/belt/architecture", params={"repo": "repo"})
    assert res.status_code == 404
    assert res.json()["detail"] and res.json()["error"]["code"] == "belt.architecture_not_found"


def test_route_serves_a_member_and_refuses_an_outsider(origin):
    from pocketpaw_ee.cloud.auth import current_active_user

    origin.push()
    origin.cloned()
    app = _build_app(role="member")
    with TestClient(app) as client:
        ok = client.get("/api/v1/belt/architecture", params={"repo": "repo"})
        outsider = SimpleNamespace(id="u9", active_workspace="w1", workspaces=[])

        async def fake_outsider():
            return outsider

        app.dependency_overrides[current_active_user] = fake_outsider
        denied = client.get("/api/v1/belt/architecture", params={"repo": "repo"})
    assert ok.status_code == 200 and ok.json()["model"]["scope"] == "app"
    assert denied.status_code == 403
