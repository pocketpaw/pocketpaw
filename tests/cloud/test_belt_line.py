# tests/cloud/test_belt_line.py — one line branch per mandate, on real git.
#
# Every mandate run lands on ``belt/line/<mandate id>``: the develop station
# starts from the line tip and syncs it with the base first (a merged line moves
# to the base; a base ahead is merged in; a conflict stands the run down with a
# sighting and leaves the line alone), and the executor applies the approved
# diff on the line tip and moves the ref by compare-and-swap, pushing it and
# reusing one PR when the repo has an origin. A run without a mandate keeps its
# own ``feat/belt-<id>`` branch.
#
# Real git in tmp repos (a bare repo stands in for origin; no network). The
# develop work is a charter recipe (a deterministic command), so no LLM seat
# runs; the PR opener is a fake.

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.cloud.belt.executor as belt_executor  # noqa: E402
import pocketpaw_ee.cloud.mandates.executor as mandates_executor  # noqa: E402
from pocketpaw_ee.cloud.belt import develop_station as ds  # noqa: E402
from pocketpaw_ee.cloud.belt.headless import HeadlessDevelopRunner  # noqa: E402
from pocketpaw_ee.cloud.mandates.executor import StationTaskDispatcher  # noqa: E402

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "w1"
MID = "65f0c0ffee00000000000abc"  # a Mongo ObjectId, as real mandate ids are
LINE = f"belt/line/{MID}"
PY = sys.executable


def _write(name: str, text: str, mode: str = "w") -> str:
    return f"{PY} -c \"open('{name}', '{mode}').write('{text}\\n')\""


# Both feature recipes append to one shared file (the way ``belt add`` edits
# package.json): a run developed from the base instead of the line would
# re-add the first feature's line and no longer apply on the line.
RECIPES = {
    "add-auth": _write("features.txt", "auth", "a"),
    "add-audit": _write("features.txt", "audit", "a"),
    "readme-line": _write("README.md", "line version"),
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _sha(cwd: Path, ref: str) -> str:
    return _git(cwd, "rev-parse", ref).strip()


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_line.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Belt Test")
    _git(path, "config", "user.email", "belt@test.local")
    (path / "README.md").write_text("toy\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A local-only toy repo on ``main`` inside the allowlist."""
    if shutil.which("git") is None:  # pragma: no cover - CI always has git
        pytest.skip("git not available")
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(tmp_path)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())
    return _init_repo(tmp_path / "toy")


@pytest.fixture
def origin(repo: Path) -> Path:
    """Give ``repo`` a bare ``origin`` with ``main`` pushed."""
    bare = repo.parent / "origin.git"
    _git(repo.parent, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main")
    return bare


class Sightings:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, workspace_id: str, mandate_id: str, draft: dict) -> None:
        self.calls.append((workspace_id, mandate_id, draft))


@pytest.fixture
def sightings() -> Sightings:
    return Sightings()


@pytest.fixture
def station(repo: Path, sightings: Sightings) -> ds.ClaudeCodeDevelop:
    async def charter_for(workspace_id: str, mandate_id: str):
        return {"repo": str(repo), "charter": {"checks": [], "recipes": RECIPES}}

    async def save_feed(*_a) -> None:
        return None

    return ds.ClaudeCodeDevelop(
        charter_for=charter_for, save_feed=save_feed, file_sighting=sightings
    )


async def _queue(store, repo: Path, recipe: str, monkeypatch, mandate_id: str = MID) -> str:
    async def _repo(workspace_id: str, mandate_id: str) -> str:
        return str(repo)

    monkeypatch.setattr(mandates_executor, "_repo_for_mandate", _repo)
    return await StationTaskDispatcher().dispatch(
        workspace_id=WS,
        mandate_id=mandate_id,
        shift_no=1,
        plan_action_id="plan-1",
        index=1,
        task={"title": recipe, "why": "", "expected_outcome": f"{recipe} done", "recipe": recipe},
    )


def _blob(action) -> dict:
    return action.parameters["_code_change"]


async def _develop(store, station, repo, recipe, monkeypatch, mandate_id: str = MID) -> str:
    run_id = await _queue(store, repo, recipe, monkeypatch, mandate_id)
    await HeadlessDevelopRunner(develop_fn=station).run(run_id, workspace_id=WS)
    return run_id


async def _land(store, run_id: str, opener=None) -> dict:
    blob = _blob(await store.get_action(run_id))
    assert not blob.get("headless_error"), blob.get("headless_error")
    await belt_executor.execute_approved_change(await store.approve(run_id), pr_opener=opener)
    action = await store.get_action(run_id)
    assert action.status == ActionStatus.EXECUTED, action.error
    return _blob(action)


class FakeOpener:
    def __init__(self, url: str = "https://github.com/acme/toy/pull/7", fail: bool = False):
        self.url, self.fail = url, fail
        self.calls: list[dict] = []

    async def open_pr(self, *, repo_path, branch, base_branch, title, body) -> str:
        self.calls.append({"branch": branch, "base_branch": base_branch})
        if self.fail:
            raise RuntimeError("gh pr create failed (exit 1): not authenticated")
        return self.url


# ---------------------------------------------------------------------------
# the line name comes from the mandate id only
# ---------------------------------------------------------------------------


def test_line_name_is_derived_from_a_real_mandate_id_only():
    assert belt_executor.line_branch(MID) == LINE
    for bad in ("", "m1", "../../etc", MID.upper(), MID + "0", MID[:-1], f"{MID[:-1]}/", "a b"):
        assert belt_executor.line_branch(bad) is None, bad


# ---------------------------------------------------------------------------
# two shifts in a row: the second builds on the first
# ---------------------------------------------------------------------------


async def test_second_run_starts_from_the_line_and_stacks_on_it(store, repo, station, monkeypatch):
    main = _sha(repo, "main")
    first = await _land(store, await _develop(store, station, repo, "add-auth", monkeypatch))
    assert first["branch"] == LINE
    assert first["commit_sha"] == _sha(repo, LINE)

    second_id = await _develop(store, station, repo, "add-audit", monkeypatch)
    # The second develop started from the line: it adds its own line only.
    diff = _blob(await store.get_action(second_id))["diff"]
    assert "+audit" in diff and "+auth" not in diff
    second = await _land(store, second_id)

    tip = _sha(repo, LINE)
    assert second["branch"] == LINE and second["commit_sha"] == tip
    assert _sha(repo, f"{LINE}^") == first["commit_sha"]
    assert _git(repo, "show", f"{LINE}:features.txt") == "auth\naudit\n"
    assert _git(repo, "diff", "--name-only", "main", LINE).split() == ["features.txt"]
    assert _sha(repo, "main") == main, "the base never moves on its own"
    assert _git(repo, "worktree", "list").count("\n") == 1


# ---------------------------------------------------------------------------
# base sync at the start of each run
# ---------------------------------------------------------------------------


async def test_a_merged_line_moves_to_the_base(store, repo, station, monkeypatch):
    await _land(store, await _develop(store, station, repo, "add-auth", monkeypatch))
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge the line", LINE)
    merged = _sha(repo, "main")

    run_id = await _develop(store, station, repo, "add-audit", monkeypatch)
    assert _sha(repo, LINE) == merged, "the line fast-forwards to the base"
    assert "line: belt/line/" in _blob(await store.get_action(run_id))["summary"]
    landed = await _land(store, run_id)
    assert _sha(repo, f"{landed['commit_sha']}^") == merged
    assert _git(repo, "show", f"{LINE}:features.txt") == "auth\naudit\n"


async def test_base_commits_the_line_lacks_are_merged_into_it(store, repo, station, monkeypatch):
    first = await _land(store, await _develop(store, station, repo, "add-auth", monkeypatch))
    (repo / "notes.txt").write_text("from main\n")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "a commit on main")
    main = _sha(repo, "main")

    run_id = await _develop(store, station, repo, "add-audit", monkeypatch)
    sync = _sha(repo, LINE)
    parents = _git(repo, "rev-list", "--parents", "-n", "1", sync).split()[1:]
    assert parents == [first["commit_sha"], main], "the base was merged into the line"
    diff = _blob(await store.get_action(run_id))["diff"]
    assert "+audit" in diff and "notes.txt" not in diff and "+auth" not in diff

    landed = await _land(store, run_id)
    assert _sha(repo, f"{landed['commit_sha']}^") == sync
    assert _git(repo, "show", f"{LINE}:features.txt") == "auth\naudit\n"
    assert _git(repo, "show", f"{LINE}:notes.txt") == "from main\n"


async def test_a_base_conflict_stands_the_run_down_with_a_sighting(
    store, repo, station, sightings, monkeypatch
):
    await _land(store, await _develop(store, station, repo, "readme-line", monkeypatch))
    before = _sha(repo, LINE)
    (repo / "README.md").write_text("main version\n")
    _git(repo, "commit", "-q", "-am", "main edits the readme")

    run_id = await _develop(store, station, repo, "add-audit", monkeypatch)
    blob = _blob(await store.get_action(run_id))
    assert blob["station_pending"] is True and not blob["diff"]
    assert "conflict" in blob["headless_error"] and "README.md" in blob["headless_error"]
    assert _sha(repo, LINE) == before, "a conflict never moves the line"
    assert _git(repo, "worktree", "list").count("\n") == 1, "the merge worktree is gone"

    ((ws, mandate, draft),) = sightings.calls
    assert (ws, mandate, draft["patrol"]) == (WS, MID, "line")
    assert LINE in draft["summary"] and "README.md" in draft["summary"]
    assert draft["evidence"]["dedup_key"] == f"line-conflict:{LINE}:{before}"


# ---------------------------------------------------------------------------
# landing: compare-and-swap on the line ref
# ---------------------------------------------------------------------------


async def test_a_line_moved_mid_landing_redevelops_and_leaves_the_line_alone(
    store, repo, station, monkeypatch
):
    await _land(store, await _develop(store, station, repo, "add-auth", monkeypatch))
    run_id = await _develop(store, station, repo, "add-audit", monkeypatch)
    tip = _sha(repo, LINE)
    tree = _sha(repo, f"{LINE}^{{tree}}")
    racer = _git(repo, "commit-tree", tree, "-p", tip, "-m", "another landing").strip()

    real_run = belt_executor._run

    async def racing_run(argv, *, cwd=None, stdin=None):
        if argv[:2] == ["git", "update-ref"] and argv[2] == f"refs/heads/{LINE}":
            _git(repo, "update-ref", f"refs/heads/{LINE}", racer)
        return await real_run(argv, cwd=cwd, stdin=stdin)

    developed: list[str] = []

    class Redeveloper:
        async def develop(self, run_ref: str, *, workspace_id: str) -> None:
            developed.append(run_ref)

    monkeypatch.setattr(belt_executor, "_run", racing_run)
    monkeypatch.setattr(belt_executor, "_headless_redeveloper", lambda: Redeveloper())
    await belt_executor.execute_approved_change(await store.approve(run_id))

    action = await store.get_action(run_id)
    assert action.status == ActionStatus.PENDING, action.error
    assert _blob(action)["station_pending"] is True and _blob(action)["redevelop"] == 1
    assert developed == [run_id]
    assert _sha(repo, LINE) == racer, "the losing landing never moves the line"


# ---------------------------------------------------------------------------
# remote: push the line, one PR per line
# ---------------------------------------------------------------------------


async def test_line_is_pushed_and_one_pr_is_reused(store, repo, origin, station, monkeypatch):
    opener = FakeOpener()
    first = await _land(
        store, await _develop(store, station, repo, "add-auth", monkeypatch), opener
    )
    second = await _land(
        store, await _develop(store, station, repo, "add-audit", monkeypatch), opener
    )

    assert [c["branch"] for c in opener.calls] == [LINE, LINE]
    assert {c["base_branch"] for c in opener.calls} == {"main"}
    assert first["pr_url"] == second["pr_url"] == opener.url
    assert second["commit_sha"] == _sha(origin, LINE) == _sha(repo, LINE)
    assert _sha(origin, f"{LINE}^") == first["commit_sha"]


async def test_a_line_pushed_ahead_on_origin_is_where_the_next_run_starts(
    store, repo, origin, station, monkeypatch
):
    await _land(store, await _develop(store, station, repo, "add-auth", monkeypatch), FakeOpener())
    clone = repo.parent / "captain"
    _git(repo.parent, "clone", "-q", "-b", LINE, str(origin), str(clone))
    (clone / "fixup.txt").write_text("by hand\n")
    _git(clone, "add", "fixup.txt")
    _git(clone, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "fixup")
    _git(clone, "push", "-q", "origin", LINE)
    pushed = _sha(clone, "HEAD")

    run_id = await _develop(store, station, repo, "add-audit", monkeypatch)
    assert "fixup.txt" not in _blob(await store.get_action(run_id))["diff"]
    landed = await _land(store, run_id, FakeOpener())
    assert _sha(origin, f"{landed['commit_sha']}^") == pushed


async def test_a_line_pr_failure_keeps_the_landing(store, repo, origin, station, monkeypatch):
    """Once the line ref moved, the commit is on the line: a push or PR failure
    is noted, never a failed run (the Foreman would re-plan built work)."""
    run_id = await _develop(store, station, repo, "add-auth", monkeypatch)
    blob = await _land(store, run_id, FakeOpener(fail=True))
    assert blob["branch"] == LINE and blob.get("pr_url") is None
    assert blob["commit_sha"] == _sha(origin, LINE)
    outcome = str((await store.get_action(run_id)).outcome)
    assert "PR" in outcome and "not authenticated" in outcome


async def test_a_run_without_a_mandate_line_keeps_its_own_branch(
    store, repo, origin, station, monkeypatch
):
    """A hand-proposed change (no mandate) lands on ``feat/belt-<id>``; a PR
    failure fails it and deletes the local branch so a retry can branch."""
    run_id = await _develop(store, station, repo, "add-auth", monkeypatch, mandate_id="m1")
    opener = FakeOpener(fail=True)
    await belt_executor.execute_approved_change(await store.approve(run_id), pr_opener=opener)
    action = await store.get_action(run_id)
    assert action.status == ActionStatus.FAILED
    assert opener.calls[0]["branch"].startswith("feat/belt-")
    assert _git(repo, "branch", "--list", "feat/belt-*").strip() == ""
    assert _git(repo, "branch", "--list", "belt/line/*").strip() == ""

    retry_id = await _develop(store, station, repo, "add-audit", monkeypatch, mandate_id="m1")
    landed = await _land(store, retry_id, FakeOpener())
    assert landed["branch"].startswith("feat/belt-")


async def test_the_ref_move_is_a_compare_and_swap(repo):
    """A create refuses an existing ref and a stale expected sha refuses a
    moved one; neither touches the ref."""
    git = belt_executor._git_in(repo)
    base = _sha(repo, "main")
    (repo / "x.txt").write_text("x\n")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-q", "-m", "x")
    second = _sha(repo, "main")

    assert await belt_executor.move_ref(git, "belt/line/x", base, "")
    assert not await belt_executor.move_ref(git, "belt/line/x", second, "")
    assert _sha(repo, "belt/line/x") == base
    assert await belt_executor.move_ref(git, "belt/line/x", second, base)
    assert not await belt_executor.move_ref(git, "belt/line/x", base, base)
    assert _sha(repo, "belt/line/x") == second


async def test_gh_opener_reuses_the_open_pr_for_a_branch(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    listed = {"out": "https://github.com/acme/toy/pull/9\n"}

    async def fake_run(argv, *, cwd=None, stdin=None):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, listed["out"], ""
        return 0, "https://github.com/acme/toy/pull/10\n", ""

    monkeypatch.setattr(belt_executor, "_run", fake_run)
    opener = belt_executor.GhCliPrOpener()
    kw = {"repo_path": tmp_path, "branch": LINE, "base_branch": "main", "title": "t", "body": "b"}
    assert await opener.open_pr(**kw) == "https://github.com/acme/toy/pull/9"
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)
    listed["out"] = ""
    assert await opener.open_pr(**kw) == "https://github.com/acme/toy/pull/10"
    assert calls[-1][:3] == ["gh", "pr", "create"]
