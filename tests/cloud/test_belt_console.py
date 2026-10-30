# tests/cloud/test_belt_console.py — Belt & Pulley console (SC-1 + SC-2).
#
# Created: 2026-06-10 (feat/belt-console-backend) — pins the /belt console the
# frontend builds against, end-to-end where cheap:
#   * GET /belt/repos — discovers git repos under the allowlist roots one level
#     deep (a root that IS a repo counts; child repos count); wire shape
#     {path, name, current_branch, branches}.
#   * POST /belt/repos — validates the path is an existing git repo, realpaths
#     it, persists it to the per-workspace allowlist extension. 4xx on
#     non-existent / non-git path; 403 for a non-admin caller.
#
# Updated: 2026-06-11 (feat/belt-repo-init) — added POST /belt/repos/init tests:
#   creates the dir + git repo + initial commit + registers it + returns the
#   standard repo shape; name validation rejects unsafe names; a location_root
#   outside the allowlist rejects; an existing target rejects; 403 for a
#   non-admin. create_remote: the fake gh-runner is called with the right args;
#   a remote failure → 200 + remote_error with the local repo intact.
#   * GET /belt/runs — runs read model over the belt code_change Instinct
#     Actions, newest-first, status/stage derived from the Action lifecycle.
#   * GET /belt/runs/{id} — run + diff, diff capped at MAX_DIFF_BYTES, 404 for a
#     cross-workspace / non-belt action.
#   * emit_belt_run_updated fires at each lifecycle point (propose / approve /
#     reject / executed / failed) on the WORKSPACE REALTIME BUS (captured via the
#     conftest recording_bus) with an additional per-stream SSE push; the
#     audience resolver fans belt_run_updated out to workspace members.
#   * GET /belt/blueprints, /belt/blueprints/c4 and /belt/blueprints/suite-map — a
#     member reads the app blueprint graph, one app's C4 view and the suite
#     feature map over B3's fake git and gh; 404
#     outside the allowlist, 422 on a bad app id, a status per component and
#     per component path, stale passed through, 503 with no graph yet.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install. The Mongo-backed
# add-repo persistence rides the conftest ``mongo_db`` fixture (mongomock-motor
# + Beanie over ALL_DOCUMENTS, which now includes BeltWorkspaceConfig).

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pocketpaw.instinct.models import ActionTrigger  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

_PATH = os.environ.get("PATH", "/usr/bin:/bin")


# ---------------------------------------------------------------------------
# git fixtures — a root holding two work repos + a child that is NOT a repo
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "Belt Test",
            "GIT_AUTHOR_EMAIL": "belt@test.local",
            "GIT_COMMITTER_NAME": "Belt Test",
            "GIT_COMMITTER_EMAIL": "belt@test.local",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": _PATH,
            "HOME": str(cwd),
        },
    )
    return res.stdout


def _make_repo(
    path: Path, *, default_branch: str = "main", extra_branch: str | None = None
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Belt Test")
    _git(path, "config", "user.email", "belt@test.local")
    (path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(path, "add", "app.py")
    _git(path, "commit", "-m", "init")
    _git(path, "branch", "-M", default_branch)
    if extra_branch:
        _git(path, "branch", extra_branch)


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    """An allowlist root holding two git work repos + a non-repo dir."""
    root = tmp_path / "checkouts"
    root.mkdir()
    _make_repo(root / "acme-api", default_branch="main", extra_branch="develop")
    _make_repo(root / "acme-web", default_branch="trunk")
    (root / "not-a-repo").mkdir()
    (root / "not-a-repo" / "readme.txt").write_text("plain dir\n", encoding="utf-8")
    return root


@pytest.fixture
def settings_allowlist(roots: Path, monkeypatch) -> Path:
    """Point belt_repo_allowlist at the root holding the two work repos."""
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(roots)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())
    return roots


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_console.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


# ---------------------------------------------------------------------------
# router app — real RBAC guard, user role is configurable per test
# ---------------------------------------------------------------------------


def _build_app(
    *,
    role: str = "admin",
    workspace_id: str = "w1",
    user_id: str = "u1",
    repo_creator=None,
) -> FastAPI:
    """A TestClient app over the belt console router with the REAL RBAC guard.

    The user's workspace role drives ``require_action_any_workspace`` — a
    ``member`` is rejected on the ADMIN-gated add-repo / init routes (403) but
    passes the MEMBER-gated reads. License is bypassed. ``repo_creator`` (when
    given) overrides the init route's ``RepoCreator`` so the ``gh repo create``
    shell-out is faked.
    """
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.belt.router import repo_creator_dep, router
    from pocketpaw_ee.cloud.license import require_license

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None

    user = SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )

    async def _fake_user_dep():
        return user

    app.dependency_overrides[current_active_user] = _fake_user_dep
    if repo_creator is not None:
        app.dependency_overrides[repo_creator_dep] = lambda: repo_creator
    return app


# ---------------------------------------------------------------------------
# GET /belt/repos — discovery
# ---------------------------------------------------------------------------


def test_list_repos_discovers_git_repos_one_level_deep(settings_allowlist, store):
    with TestClient(_build_app(role="member")) as client:
        res = client.get("/api/v1/belt/repos")
    assert res.status_code == 200, res.text
    repos = res.json()["repos"]
    names = sorted(r["name"] for r in repos)
    assert names == ["acme-api", "acme-web"]  # not-a-repo is excluded
    by_name = {r["name"]: r for r in repos}
    assert by_name["acme-api"]["current_branch"] == "main"
    assert "develop" in by_name["acme-api"]["branches"]
    assert by_name["acme-web"]["current_branch"] == "trunk"
    # Wire shape.
    for r in repos:
        assert set(r.keys()) == {"path", "name", "current_branch", "branches"}


def test_list_repos_counts_a_root_that_is_itself_a_repo(tmp_path, monkeypatch, store):
    """A root that IS a git repo counts directly (not just its children)."""
    repo = tmp_path / "single"
    _make_repo(repo, default_branch="main")

    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(repo)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())

    with TestClient(_build_app(role="member")) as client:
        res = client.get("/api/v1/belt/repos")
    assert res.status_code == 200, res.text
    names = [r["name"] for r in res.json()["repos"]]
    assert "single" in names


# ---------------------------------------------------------------------------
# POST /belt/repos — add + validation + RBAC
# ---------------------------------------------------------------------------


async def test_add_repo_persists_new_root(settings_allowlist, store, mongo_db, tmp_path):
    """A valid git repo is realpath-resolved, persisted, and returned. After the
    add, discovery surfaces a repo under the newly authorized root."""
    new_root = tmp_path / "extra"
    _make_repo(new_root / "beta", default_branch="main")

    with TestClient(_build_app(role="admin")) as client:
        res = client.post("/api/v1/belt/repos", json={"path": str(new_root / "beta")})
        assert res.status_code == 200, res.text
        repo = res.json()["repo"]
        assert repo["name"] == "beta"
        assert repo["current_branch"] == "main"

        # Persisted: the workspace's extension now carries the resolved root.
        from pocketpaw_ee.cloud.models.belt_workspace_config import BeltWorkspaceConfig

        doc = await BeltWorkspaceConfig.find_one(BeltWorkspaceConfig.workspace == "w1")
        assert doc is not None
        assert str((new_root / "beta").resolve()) in doc.allowlist_roots

        # Discovery now includes the added repo (settings root UNION extension).
        listed = client.get("/api/v1/belt/repos").json()["repos"]
        assert "beta" in [r["name"] for r in listed]


async def test_add_repo_rejects_nonexistent_path(settings_allowlist, store, mongo_db, tmp_path):
    with TestClient(_build_app(role="admin")) as client:
        res = client.post("/api/v1/belt/repos", json={"path": str(tmp_path / "ghost")})
    assert res.status_code == 400, res.text
    assert "error" in res.json()


async def test_add_repo_rejects_non_git_dir(settings_allowlist, store, mongo_db, roots):
    with TestClient(_build_app(role="admin")) as client:
        res = client.post("/api/v1/belt/repos", json={"path": str(roots / "not-a-repo")})
    assert res.status_code == 400, res.text
    assert "git repository" in res.json()["error"]["message"]


async def test_add_repo_forbidden_for_non_admin(settings_allowlist, store, mongo_db, roots):
    """A workspace MEMBER cannot add a repo — belt.manage is ADMIN-gated."""
    with TestClient(_build_app(role="member")) as client:
        res = client.post("/api/v1/belt/repos", json={"path": str(roots / "acme-api")})
    assert res.status_code == 403, res.text


# ---------------------------------------------------------------------------
# POST /belt/repos/init — create a brand-new repo + validation + RBAC + remote
# ---------------------------------------------------------------------------


class _FakeRepoCreator:
    """A fake RepoCreator that records its call args and never touches GitHub."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    async def create_remote(self, *, repo_path, name) -> None:
        self.calls.append({"repo_path": Path(repo_path), "name": name})
        if self.fail:
            raise RuntimeError("gh repo create failed (exit 1): not authenticated")


async def test_init_repo_creates_dir_git_commit_and_registers(
    settings_allowlist, store, mongo_db, tmp_path
):
    """A valid init creates the dir + a git repo with an initial commit, registers
    the repo under the workspace allowlist, and returns the standard repo shape."""
    location = tmp_path / "checkouts"  # the settings_allowlist root
    with TestClient(_build_app(role="admin")) as client:
        res = client.post(
            "/api/v1/belt/repos/init",
            json={"name": "fresh-svc", "location_root": str(location), "create_remote": False},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert "remote_error" not in body
        repo = body["repo"]
        assert repo["name"] == "fresh-svc"
        assert set(repo.keys()) == {"path", "name", "current_branch", "branches"}
        # The repo has a HEAD + default branch (seed commit landed).
        assert repo["current_branch"] != ""

        target = location / "fresh-svc"
        assert (target / ".git").is_dir()
        assert (target / "README.md").read_text(encoding="utf-8") == "# fresh-svc\n"
        # One commit on the default branch.
        log = _git(target, "log", "--oneline")
        assert log.strip()

        # Registered: the workspace extension now carries the resolved repo path.
        from pocketpaw_ee.cloud.models.belt_workspace_config import BeltWorkspaceConfig

        doc = await BeltWorkspaceConfig.find_one(BeltWorkspaceConfig.workspace == "w1")
        assert doc is not None
        assert str(target.resolve()) in doc.allowlist_roots


async def test_init_repo_rejects_unsafe_name(settings_allowlist, store, mongo_db, tmp_path):
    """A name with a path separator (or traversal) is refused before any FS write."""
    location = tmp_path / "checkouts"
    with TestClient(_build_app(role="admin")) as client:
        for bad in ("../escape", "a/b", "Has Space", "UPPER", ".hidden"):
            res = client.post(
                "/api/v1/belt/repos/init",
                json={"name": bad, "location_root": str(location)},
            )
            assert res.status_code == 400, f"{bad!r}: {res.text}"
            assert "error" in res.json()
    # Nothing was created.
    assert not (location / "escape").exists()


async def test_init_repo_rejects_root_outside_allowlist(
    settings_allowlist, store, mongo_db, tmp_path
):
    """A location_root outside every allowlist root is refused (path-free 400)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    with TestClient(_build_app(role="admin")) as client:
        res = client.post(
            "/api/v1/belt/repos/init",
            json={"name": "x", "location_root": str(outside)},
        )
    assert res.status_code == 400, res.text
    assert "authorized" in res.json()["error"]["message"].lower()
    assert not (outside / "x").exists()


async def test_init_repo_rejects_existing_target(settings_allowlist, store, mongo_db, tmp_path):
    """An init at a path that already exists is refused — no clobber."""
    location = tmp_path / "checkouts"
    (location / "taken").mkdir()
    with TestClient(_build_app(role="admin")) as client:
        res = client.post(
            "/api/v1/belt/repos/init",
            json={"name": "taken", "location_root": str(location)},
        )
    assert res.status_code == 400, res.text
    assert "already exists" in res.json()["error"]["message"].lower()


async def test_init_repo_forbidden_for_non_admin(settings_allowlist, store, mongo_db, tmp_path):
    """A workspace MEMBER cannot init a repo — belt.manage is ADMIN-gated."""
    location = tmp_path / "checkouts"
    with TestClient(_build_app(role="member")) as client:
        res = client.post(
            "/api/v1/belt/repos/init",
            json={"name": "nope", "location_root": str(location)},
        )
    assert res.status_code == 403, res.text
    assert not (location / "nope").exists()


async def test_init_repo_with_remote_calls_creator_with_right_args(
    settings_allowlist, store, mongo_db, tmp_path
):
    """create_remote=true → the fake gh-runner is called with the new repo path
    and name; the response has no remote_error."""
    location = tmp_path / "checkouts"
    creator = _FakeRepoCreator()
    with TestClient(_build_app(role="admin", repo_creator=creator)) as client:
        res = client.post(
            "/api/v1/belt/repos/init",
            json={"name": "remote-svc", "location_root": str(location), "create_remote": True},
        )
    assert res.status_code == 200, res.text
    assert "remote_error" not in res.json()
    assert len(creator.calls) == 1
    call = creator.calls[0]
    assert call["name"] == "remote-svc"
    assert call["repo_path"] == (location / "remote-svc").resolve()


async def test_init_repo_remote_failure_keeps_local_repo(
    settings_allowlist, store, mongo_db, tmp_path
):
    """A remote-creation failure → 200 with remote_error; the local repo is intact
    and registered (never rolled back)."""
    location = tmp_path / "checkouts"
    creator = _FakeRepoCreator(fail=True)
    with TestClient(_build_app(role="admin", repo_creator=creator)) as client:
        res = client.post(
            "/api/v1/belt/repos/init",
            json={"name": "half-svc", "location_root": str(location), "create_remote": True},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert "remote_error" in body
        assert "local repository was created" in body["remote_error"].lower()
        # Local repo is intact.
        target = location / "half-svc"
        assert (target / ".git").is_dir()
        assert (target / "README.md").exists()
        # And registered despite the remote failure.
        from pocketpaw_ee.cloud.models.belt_workspace_config import BeltWorkspaceConfig

        doc = await BeltWorkspaceConfig.find_one(BeltWorkspaceConfig.workspace == "w1")
        assert str(target.resolve()) in doc.allowlist_roots


# ---------------------------------------------------------------------------
# GET /belt/runs — runs read model + status derivation
# ---------------------------------------------------------------------------


def _trigger() -> ActionTrigger:
    return ActionTrigger(type="agent", source="belt:develop", reason="console test")


async def _propose_run(
    store: InstinctStore,
    *,
    workspace_id: str = "w1",
    task: str = "make hello friendlier",
    summary: str = "friendlier greeting",
    repo: str = "/srv/acme-api",
    base_branch: str = "main",
    diff: str = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n",
):
    """File a belt code_change Action directly via the store (the blob shape the
    MCP server writes)."""
    blob = {
        "kind": "code_change",
        "schema": 2,
        "repo": repo,
        "base_branch": base_branch,
        "diff": diff,
        "summary": summary,
        "task": task,
        "workspace_id": workspace_id,
        "requested_by": "u1",
        "correlation_id": "corr-123",
    }
    return await store.propose(
        workspace_id,
        f"Code change — {Path(repo).name} ({base_branch})",
        "desc",
        "rec",
        _trigger(),
        parameters={"_code_change": blob},
    )


async def test_list_runs_shape_and_status_derivation(store):
    await _propose_run(store, task="t-proposed")
    rejected = await _propose_run(store, task="t-rejected")
    await store.reject(rejected.id, reason="no thanks")
    landed = await _propose_run(store, task="t-landed")
    await store.approve(landed.id)
    await store.mark_executed(landed.id, "PR opened: https://x/pull/1")
    failed = await _propose_run(store, task="t-failed")
    await store.approve(failed.id)
    await store.mark_failed(failed.id, "apply conflict")
    # A non-belt action in the same workspace must be filtered out.
    await store.propose("w1", "not a belt run", "", "", _trigger())

    with TestClient(_build_app(role="member")) as client:
        res = client.get("/api/v1/belt/runs")
    assert res.status_code == 200, res.text
    runs = res.json()["runs"]
    by_task = {r["task"]: r for r in runs}
    # Only belt runs (4), not the bare action.
    assert set(by_task) == {"t-proposed", "t-rejected", "t-landed", "t-failed"}
    assert (by_task["t-proposed"]["status"], by_task["t-proposed"]["stage"]) == ("proposed", "gate")
    assert (by_task["t-rejected"]["status"], by_task["t-rejected"]["stage"]) == ("rejected", "done")
    assert (by_task["t-landed"]["status"], by_task["t-landed"]["stage"]) == ("landed", "done")
    assert (by_task["t-failed"]["status"], by_task["t-failed"]["stage"]) == ("failed", "done")
    # Wire shape on a row.
    row = by_task["t-proposed"]
    for key in (
        "action_id",
        "task",
        "summary",
        "status",
        "stage",
        "repo",
        "base_branch",
        "created_at",
        "correlation_id",
        "files_changed",
        "error",
    ):
        assert key in row
    assert row["correlation_id"] == "corr-123"
    # A failed run says why; a run that hasn't failed carries no error.
    assert by_task["t-failed"]["error"] == "apply conflict"
    assert row["error"] is None


async def test_a_headless_run_reads_running_at_its_live_stage(store):
    """A queued station run reads ``queued`` at ``station`` until a develop
    starts it (``headless_state`` "queued" = handed over, not started), then
    ``running`` at the stage its ``headless_state`` names."""
    from pocketpaw_ee.cloud.belt.service import list_runs

    run = await _propose_run(store, task="t-live", base_branch="", diff="")
    params = dict(run.parameters)
    for state, want in (
        (None, ("queued", "station")),
        ("queued", ("queued", "station")),
        ("develop", ("running", "develop")),
        ("review", ("running", "review")),
    ):
        blob = {**params["_code_change"], "station_pending": True, "headless_state": state}
        await store.update_parameters(run.id, {**params, "_code_change": blob})
        row = (await list_runs("w1"))["runs"][0]
        assert (row["status"], row["stage"]) == want, state
        assert row["headless_state"] == state


async def test_list_runs_newest_first(store):
    """Runs come back newest-first. The store orders by ``created_at DESC``;
    two proposes in the SAME wall-clock second tie (SQLite second resolution),
    so we space them by a second to assert the ordering deterministically."""
    import datetime as _dt

    first = await _propose_run(store, task="first")
    second = await _propose_run(store, task="second")
    # Force a strictly-newer created_at on `second` so the ordering is
    # unambiguous regardless of how fast the two proposes ran.
    import aiosqlite

    newer = (_dt.datetime.now() + _dt.timedelta(seconds=5)).isoformat()
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE instinct_actions SET created_at = ? WHERE id = ?", (newer, second.id)
        )
        await db.commit()

    with TestClient(_build_app(role="member")) as client:
        runs = client.get("/api/v1/belt/runs").json()["runs"]
    tasks = [r["task"] for r in runs]
    assert tasks.index("second") < tasks.index("first")
    assert {first.id, second.id} == {r["action_id"] for r in runs}


# ---------------------------------------------------------------------------
# GET /belt/runs/{id} — detail + diff cap + tenancy
# ---------------------------------------------------------------------------


async def test_get_run_returns_diff(store):
    action = await _propose_run(store, task="detail", diff="--- a\n+++ b\n@@ -1 +1 @@\n-a\n+b\n")
    with TestClient(_build_app(role="member")) as client:
        res = client.get(f"/api/v1/belt/runs/{action.id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["task"] == "detail"
    assert "+b" in body["diff"]
    assert body["diff_truncated"] is False


async def test_get_run_caps_diff(store):
    from pocketpaw_ee.cloud.belt.service import MAX_DIFF_BYTES

    big = "+" + ("a" * (MAX_DIFF_BYTES + 5000)) + "\n"
    action = await _propose_run(store, task="big", diff=big)
    with TestClient(_build_app(role="member")) as client:
        body = client.get(f"/api/v1/belt/runs/{action.id}").json()
    assert body["diff_truncated"] is True
    assert len(body["diff"].encode("utf-8")) <= MAX_DIFF_BYTES


async def test_get_run_cross_workspace_is_404(store):
    """A run belonging to another workspace is a 404 (never confirm existence)."""
    action = await _propose_run(store, workspace_id="w-other", task="foreign")
    with TestClient(_build_app(role="member", workspace_id="w1")) as client:
        res = client.get(f"/api/v1/belt/runs/{action.id}")
    assert res.status_code == 404, res.text


async def test_get_run_missing_is_404(store):
    with TestClient(_build_app(role="member")) as client:
        res = client.get("/api/v1/belt/runs/act-does-not-exist")
    assert res.status_code == 404, res.text


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


async def _run_with_screenshots(store, root: Path, workspace_id: str = "w1"):
    action = await _propose_run(store, workspace_id=workspace_id, task="shots")
    params = dict(action.parameters)
    params["_code_change"] = {
        **params["_code_change"],
        "screenshots": [
            {"name": "home-dark.png", "bytes": len(PNG), "sha": "f" * 64},
            {"name": "link.png", "bytes": len(PNG), "sha": "f" * 64},
        ],
        "tester": {"session_url": "https://claude.ai/code/session_T", "at": 1.0},
    }
    await store.update_parameters(action.id, params)
    run_dir = root / action.id
    run_dir.mkdir(parents=True)
    (run_dir / "home-dark.png").write_bytes(PNG)
    (run_dir / "unlisted.png").write_bytes(PNG)  # on disk, but not on the blob
    (root / "secret.png").write_bytes(PNG)
    (run_dir / "link.png").symlink_to(root / "secret.png")  # listed, but leads out
    return action


async def test_run_screenshots_are_listed_and_served(store, tmp_path, monkeypatch):
    root = tmp_path / "shots"
    monkeypatch.setenv("POCKETPAW_FACTORY_SCREENSHOT_DIR", str(root))
    action = await _run_with_screenshots(store, root)
    base = f"/api/v1/belt/runs/{action.id}"
    with TestClient(_build_app(role="member")) as client:
        detail = client.get(base).json()
        png = client.get(f"{base}/screenshots/home-dark.png")
        unlisted = client.get(f"{base}/screenshots/unlisted.png")
        escape = client.get(f"{base}/screenshots/..%2Fsecret.png")
        link = client.get(f"{base}/screenshots/link.png")
        missing = client.get("/api/v1/belt/runs/act-nope/screenshots/home-dark.png")
        rows = client.get("/api/v1/belt/runs").json()["runs"]

    assert detail["screenshots"] == [
        {"name": "home-dark.png", "bytes": len(PNG)},
        {"name": "link.png", "bytes": len(PNG)},
    ]
    assert detail["tester"] == {"session_url": "https://claude.ai/code/session_T", "at": 1.0}
    (row,) = [r for r in rows if r["action_id"] == action.id]
    assert row["screenshots"] == detail["screenshots"]  # the list's thumbnails
    assert png.status_code == 200, png.text
    assert png.headers["content-type"] == "image/png"
    assert png.content == PNG
    assert unlisted.status_code == 404
    assert escape.status_code == 404
    assert link.status_code == 404
    assert missing.status_code == 404


async def test_run_screenshots_are_workspace_scoped(store, tmp_path, monkeypatch):
    root = tmp_path / "shots"
    monkeypatch.setenv("POCKETPAW_FACTORY_SCREENSHOT_DIR", str(root))
    action = await _run_with_screenshots(store, root, workspace_id="w-other")
    with TestClient(_build_app(role="member", workspace_id="w1")) as client:
        res = client.get(f"/api/v1/belt/runs/{action.id}/screenshots/home-dark.png")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Realtime — emit_belt_run_updated rides the WORKSPACE BUS (primary) + SSE
# ---------------------------------------------------------------------------
#
# The bus is the REQUIRED path: the frontend subscribes via the global workspace
# bus because run status changes fire ASYNCHRONOUSLY after the chat turn ends
# (approve in the Tray, PR landing later). The conftest ``recording_bus`` fixture
# installs a RecordingBus for every test, so a bus publish lands in
# ``bus.events``. The per-stream ``push_sse_event`` is the secondary in-turn path.


class _SSECapture:
    """Capture push_sse_event calls in place of the real per-stream sink."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, name: str, data: dict) -> None:
        self.events.append((name, data))

    def belt_events(self) -> list[dict]:
        return [data for name, data in self.events if name == "belt_run_updated"]


@pytest.fixture
def sse(monkeypatch) -> _SSECapture:
    """Patch the secondary per-stream push_sse_event path."""
    cap = _SSECapture()
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.agent_service.push_sse_event", cap, raising=True)
    return cap


def _bus_belt_events(recording_bus) -> list[dict]:
    """The belt_run_updated events that hit the workspace bus this test."""
    return [e.data for e in recording_bus.events if e.type == "belt_run_updated"]


async def test_bus_event_audience_is_workspace_scoped() -> None:
    """The audience resolver fans belt_run_updated out to workspace members,
    keyed on the event's workspace_id."""
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud._core.realtime.events import BeltRunUpdated

    seen: list[str] = []

    async def _members(wid: str) -> list[str]:
        seen.append(wid)
        return ["u1", "u2", "u3"]

    resolver = AudienceResolver(workspace_members=_members)
    event = BeltRunUpdated(
        data={"workspace_id": "w1", "action_id": "a1", "status": "landed", "stage": "done"}
    )
    audience = await resolver.audience(event)
    assert set(audience) == {"u1", "u2", "u3"}
    assert seen == ["w1"]
    # No workspace_id → no fan-out (defensive).
    empty = await resolver.audience(BeltRunUpdated(data={"action_id": "a1"}))
    assert empty == []


async def test_propose_publishes_on_bus_and_sse(
    store, settings_allowlist, sse, roots, recording_bus
):
    """The propose path publishes belt_run_updated(proposed, gate) on the
    workspace bus (primary) AND the per-stream SSE (secondary)."""
    import pocketpaw_ee.agent.mcp_servers.belt as belt_mcp
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1", session_mongo_id="s1")
    try:
        res = await belt_mcp._propose_change_handler(
            {
                "repo": str(roots / "acme-api"),
                "base_branch": "main",
                "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n",
                "summary": "tweak",
                "task": "tweak the file",
            }
        )
        assert res.get("is_error") is not True, res
    finally:
        detach_agent_identity(tokens)

    # Primary: workspace bus carries workspace_id + the contract fields.
    bus_events = _bus_belt_events(recording_bus)
    proposed = [e for e in bus_events if e["status"] == "proposed"]
    assert proposed, "propose must publish belt_run_updated on the workspace bus"
    assert proposed[0]["stage"] == "gate"
    assert proposed[0]["workspace_id"] == "w1"
    # Secondary: in-turn SSE also fired.
    assert any(e["status"] == "proposed" for e in sse.belt_events())


async def test_emit_helper_publishes_on_bus(recording_bus, sse):
    """The shared helper publishes on the workspace bus with the full payload."""
    from pocketpaw_ee.cloud.belt.service import emit_belt_run_updated

    await emit_belt_run_updated(
        workspace_id="w1", action_id="act-1", status="rejected", stage="done"
    )
    bus_events = _bus_belt_events(recording_bus)
    assert bus_events == [
        {"workspace_id": "w1", "action_id": "act-1", "status": "rejected", "stage": "done"}
    ]
    # Secondary SSE mirrors it.
    assert sse.belt_events() == [
        {"workspace_id": "w1", "action_id": "act-1", "status": "rejected", "stage": "done"}
    ]


async def test_executor_emits_landed_and_persists_pr_result(
    monkeypatch, recording_bus, sse, tmp_path
):
    """The executor publishes belt_run_updated(landed, done) on the bus AND
    back-writes pr_url/branch onto the blob so the runs read model reads them
    structurally."""
    # Build a real local repo (bare origin + work clone) so the executor's git
    # path runs end-to-end with an injected fake PR opener.
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")
    (work / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-u", "origin", "main")

    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(tmp_path)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())

    st = InstinctStore(tmp_path / "exec.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)

    action = await _propose_run(
        st,
        repo=str(work),
        base_branch="main",
        diff=(
            "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
            " def hello():\n-    return 'hi'\n+    return 'hello world'\n"
        ),
    )
    await st.approve(action.id)

    from pocketpaw_ee.cloud.belt import executor as belt_executor

    class _FakeOpener:
        async def open_pr(self, *, repo_path, branch, base_branch, title, body) -> str:
            return "https://github.com/acme/repo/pull/7"

    await belt_executor.execute_approved_change(action, pr_opener=_FakeOpener())

    # Primary: workspace bus carries the landed/done terminal + pr_url + tenancy.
    landed = [e for e in _bus_belt_events(recording_bus) if e["status"] == "landed"]
    assert landed and landed[0]["stage"] == "done"
    assert landed[0]["pr_url"] == "https://github.com/acme/repo/pull/7"
    assert landed[0]["workspace_id"] == "w1"

    # PR result back-written onto the blob → runs read model sees it structurally.
    refreshed = await st.get_action(action.id)
    blob = refreshed.parameters["_code_change"]
    assert blob["pr_url"] == "https://github.com/acme/repo/pull/7"
    assert blob["branch"].startswith("feat/belt-")
    assert blob["files_changed"] == 1
    # The event names the landing branch, so the run chip updates without a refetch.
    assert landed[0]["branch"] == blob["branch"]


async def test_executor_local_only_emits_landed_without_pr_url(
    monkeypatch, recording_bus, sse, tmp_path
):
    """A NO-ORIGIN repo: the executor publishes belt_run_updated(landed, done) on
    the bus WITHOUT a pr_url, and back-writes branch + commit_sha (not pr_url)."""
    # A plain repo with NO remote at all.
    work = tmp_path / "local"
    work.mkdir()
    _git(work, "init")
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")
    (work / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")

    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(tmp_path)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())

    st = InstinctStore(tmp_path / "exec_local.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)

    action = await _propose_run(
        st,
        repo=str(work),
        base_branch="main",
        diff=(
            "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
            " def hello():\n-    return 'hi'\n+    return 'hello world'\n"
        ),
    )
    await st.approve(action.id)

    from pocketpaw_ee.cloud.belt import executor as belt_executor

    class _FakeOpener:
        def __init__(self) -> None:
            self.called = False

        async def open_pr(self, *, repo_path, branch, base_branch, title, body) -> str:
            self.called = True
            return "https://should-not-be-used"

    opener = _FakeOpener()
    await belt_executor.execute_approved_change(action, pr_opener=opener)

    # No PR opener was invoked (no remote → no push, no PR).
    assert opener.called is False

    # Bus: a landed/done terminal with NO pr_url key.
    landed = [e for e in _bus_belt_events(recording_bus) if e["status"] == "landed"]
    assert landed and landed[0]["stage"] == "done"
    assert "pr_url" not in landed[0]
    assert landed[0]["workspace_id"] == "w1"

    # Blob: branch + commit_sha back-written, NO pr_url.
    refreshed = await st.get_action(action.id)
    blob = refreshed.parameters["_code_change"]
    assert blob["branch"].startswith("feat/belt-")
    assert blob["commit_sha"]
    assert "pr_url" not in blob or blob["pr_url"] is None
    assert blob["files_changed"] == 1
    assert landed[0]["branch"] == blob["branch"]


async def test_executor_emits_failed_on_apply_conflict(monkeypatch, recording_bus, sse, tmp_path):
    """A diff that doesn't apply → the executor publishes belt_run_updated(failed)
    on the workspace bus."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")
    (work / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-u", "origin", "main")

    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(tmp_path)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())

    st = InstinctStore(tmp_path / "exec_fail.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)

    action = await _propose_run(
        st,
        repo=str(work),
        base_branch="main",
        # A diff against content that doesn't exist on main → apply conflict.
        diff="--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n nonexistent\n-line\n+other\n",
    )
    await st.approve(action.id)

    from pocketpaw_ee.cloud.belt import executor as belt_executor

    await belt_executor.execute_approved_change(action)

    failed = [e for e in _bus_belt_events(recording_bus) if e["status"] == "failed"]
    assert failed and failed[0]["stage"] == "done"
    assert failed[0]["workspace_id"] == "w1"


# ---------------------------------------------------------------------------
# GET /belt/blueprints, /belt/blueprints/c4 — the app blueprint reads
# ---------------------------------------------------------------------------


@pytest.fixture
def bp(settings_allowlist, monkeypatch):
    """B3's fake git and gh behind the real reader, on an allowlisted repo."""
    from pocketpaw_ee.cloud.belt import app_blueprint_read as abr

    from tests.cloud.test_belt_app_blueprint_read import Fake

    for name in ("_CACHE", "_LOCKS", "_BLOBS"):
        monkeypatch.setattr(abr, name, {})
    fake = Fake((settings_allowlist / "acme-api").resolve())
    monkeypatch.setattr(abr, "_git", lambda path: fake.git)
    monkeypatch.setattr("pocketpaw_ee.cloud.mandates.patrols._default_gh", fake.gh)
    monkeypatch.setattr(abr, "_lead_runs", fake.runs)
    monkeypatch.setattr(abr, "_touched", fake.touched)
    return fake


def _bp_get(path: str, fake, **params):
    with TestClient(_build_app(role="member")) as client:
        return client.get(f"/api/v1/belt/{path}", params={"repo": str(fake.repo), **params})


def test_member_reads_the_blueprint_graph(bp):
    res = _bp_get("blueprints", bp)
    assert res.status_code == 200, res.text
    g = res.json()
    assert (g["slug"], g["sha"], g["stale"]) == ("qbtrix/creative-apps", "62a8577", False)
    assert {f["id"]: f["status"] for f in g["features"]}["halo/layer-keys"] == "building"


def test_blueprint_repo_outside_the_allowlist_is_404(bp, tmp_path):
    _make_repo(tmp_path / "elsewhere")
    for path, extra in (("blueprints", {}), ("blueprints/c4", {"app": "halo"})):
        with TestClient(_build_app(role="member")) as client:
            q = {"repo": str(tmp_path / "elsewhere"), **extra}
            res = client.get(f"/api/v1/belt/{path}", params=q)
        assert res.status_code == 404, res.text
        assert res.json()["error"]["code"] == "belt.repo_not_found"
    assert bp.calls == []


def test_blueprint_c4_rejects_a_bad_app_id(bp):
    for app in ("../x", "Halo", "-x", ""):
        assert _bp_get("blueprints/c4", bp, app=app).status_code == 422, app
    assert bp.calls == []


def test_blueprint_c4_returns_the_model_and_a_status_per_component(bp):
    res = _bp_get("blueprints/c4", bp, app="halo")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["app"] == "halo"
    assert body["model"]["scope"] == "creative-apps/halo (planned)"
    assert body["model"]["model"]["systems"]
    status = body["status"]
    assert status["halo-window"] == "changing"  # owned by building halo/layer-keys
    assert status["halo-project"] == "landed"  # every path on dev
    assert status["halo-frames"] == "planned"  # partial
    assert status["halo-text"] == "planned"
    assert set(status.values()) <= {"planned", "landed", "changing", "failed"}
    missing = _bp_get("blueprints/c4", bp, app="nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "belt.app_not_found"


def test_blueprint_c4_marks_each_path_of_a_partial_component(bp):
    status = _bp_get("blueprints/c4", bp, app="halo").json()["status"]
    assert status["halo-frames"] == "planned"  # partial: one of two files on dev
    assert status["Sources/HaloModel/FrameRenderer.swift"] == "landed"
    assert status["Sources/HaloModel/HaloRenderFrame.swift"] == "planned"


def test_blueprint_c4_paints_a_stuck_owner_failed(bp, monkeypatch):
    async def gh(argv):
        code, out, err = await bp.gh(argv)
        if argv[1:3] == ["issue", "list"] and "qbtrix/creative-apps" in argv:
            out = out.replace('"feature-lead"', '"needs-human"')
        return code, out, err

    monkeypatch.setattr("pocketpaw_ee.cloud.mandates.patrols._default_gh", gh)
    status = _bp_get("blueprints/c4", bp, app="halo").json()["status"]
    assert status["halo-window"] == "failed"
    assert status["halo-project"] == "landed"


def test_blueprint_failed_refresh_passes_stale_through(bp, monkeypatch):
    from pocketpaw_ee.cloud.belt import app_blueprint_read as abr

    monkeypatch.setattr(abr, "TTL", 0.0)
    with TestClient(_build_app(role="member")) as client:
        q = {"repo": str(bp.repo)}
        assert client.get("/api/v1/belt/blueprints", params=q).json()["stale"] is False
        bp.limited = True
        res = client.get("/api/v1/belt/blueprints", params=q)
        c4 = client.get("/api/v1/belt/blueprints/c4", params={**q, "app": "halo"})
    assert res.status_code == 200 and res.json()["stale"] is True
    assert c4.status_code == 200 and c4.json()["status"]["halo-window"] == "changing"


def test_member_reads_the_suite_map(bp):
    from tests.cloud.test_belt_app_blueprint_read import MAP, suite_map

    bp.files[MAP] = suite_map([("keys", "halo/layer-keys"), ("new", None)])
    bp.tree()
    res = _bp_get("blueprints/suite-map", bp)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [f["status"] for f in body["features"]] == ["building", "new"]
    assert body["layers"] == [{"id": "kernel", "name": "SuiteKernel"}]


def test_suite_map_without_a_map_file_is_404(bp):
    res = _bp_get("blueprints/suite-map", bp)
    assert res.status_code == 404, res.text
    assert res.json()["error"]["code"] == "belt.suite_map_not_found"


def test_suite_map_repo_outside_the_allowlist_is_404(bp, tmp_path):
    _make_repo(tmp_path / "elsewhere")
    with TestClient(_build_app(role="member")) as client:
        q = {"repo": str(tmp_path / "elsewhere")}
        res = client.get("/api/v1/belt/blueprints/suite-map", params=q)
    assert res.status_code == 404 and res.json()["error"]["code"] == "belt.repo_not_found"
    assert bp.calls == []


def test_blueprint_with_no_graph_yet_is_503(bp):
    bp.limited = True
    res = _bp_get("blueprints", bp)
    assert res.status_code == 503, res.text
    assert res.json()["error"]["code"] == "belt.blueprint_unavailable"
