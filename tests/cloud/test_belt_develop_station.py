# tests/cloud/test_belt_develop_station.py — the craft factory's T1 slice.
#
# Covers the headless develop station (``belt/develop_station.ClaudeCodeDevelop``)
# end to end against a REAL tmp git repo with REAL tiny check commands; only the
# ``claude`` binary is faked, by intercepting its argv in the injected runner.
# Also covers the plumbing it rides on: ``daily`` cadence due-ness, charter
# checks/recipes round-tripping through the mandates service, recipe validation
# in the foreman, the recipe surviving dispatch into ``DevelopRequest``, the
# production dispatcher developing in the background, and the env-gated wiring.

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import develop_station as ds  # noqa: E402
from pocketpaw_ee.cloud.belt.headless import (  # noqa: E402
    DevelopRequest,
    DevelopResult,
    HeadlessDevelopRunner,
    resolve_headless_dispatcher,
    set_production_develop_fn,
)
from pocketpaw_ee.cloud.mandates import foreman  # noqa: E402

FAKE_CLAUDE = "/fake/bin/claude"
PY = sys.executable
# A real check: passes only when feature.txt says "ok".
CHECK = (
    f"{PY} -c \"import pathlib,sys; t=pathlib.Path('feature.txt'); "
    "ok=t.exists() and t.read_text().strip()=='ok'; "
    "print('feature ok' if ok else 'NEED OK IN feature.txt'); sys.exit(0 if ok else 1)\""
)


# ---------------------------------------------------------------------------
# fixtures + the fake claude
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A local-only (no origin) git repo on ``main`` with one commit, inside an
    allowlisted root."""
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    (root / "README.md").write_text("toy\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    monkeypatch.setattr(
        "pocketpaw_ee.agent.mcp_servers.belt._resolve_allowlist", lambda: [tmp_path.resolve()]
    )
    monkeypatch.setenv("POCKETPAW_FACTORY_CLAUDE_BIN", FAKE_CLAUDE)
    monkeypatch.delenv("POCKETPAW_FACTORY_CLAUDE_MODEL", raising=False)
    return root


class FakeClaude:
    """Records every runner call; answers ``claude`` argv from scripted seats and
    passes everything else (git, checks, recipes) to the real subprocess."""

    def __init__(self, develop=(), review=()):
        self.develop = list(develop)  # callables(cwd) run on each develop/fix seat
        self.review = list(review)  # dicts returned by each review seat
        self.argvs: list[list[str]] = []
        self.claude_calls: list[tuple[str, str]] = []  # (seat, prompt)

    async def __call__(self, argv, *, cwd, timeout, stdin=None):
        self.argvs.append(list(argv))
        if argv[0] != FAKE_CLAUDE:
            return await ds.run_subprocess(argv, cwd=cwd, timeout=timeout, stdin=stdin)
        tools = argv[argv.index("--tools") + 1].split(",")
        if "Edit" in tools:
            seat = "develop" if not self.claude_calls else "fix"
            self.claude_calls.append((seat, stdin or ""))
            if self.develop:
                self.develop.pop(0)(Path(cwd))
            return 0, json.dumps({"type": "result", "result": "done"}), ""
        self.claude_calls.append(("review", stdin or ""))
        verdict = self.review.pop(0) if self.review else {"verdict": "pass", "notes": []}
        return 0, json.dumps({"type": "result", "result": json.dumps(verdict)}), ""


def _write(text: str):
    return lambda cwd: (cwd / "feature.txt").write_text(text + "\n")


def _station(fake: FakeClaude, repo: Path, *, checks=(CHECK,), recipes=None):
    async def charter_for(workspace_id: str, mandate_id: str):
        return {
            "repo": str(repo),
            "charter": {
                "goal": "ship features",
                "boundaries": ["never touch auth"],
                "says_no": [],
                "checks": list(checks),
                "recipes": dict(recipes or {}),
            },
        }

    return ds.ClaudeCodeDevelop(run=fake, charter_for=charter_for)


def _request(repo: Path, recipe: str = "") -> DevelopRequest:
    return DevelopRequest(
        task="Add feature.txt\n\nusers asked for it",
        summary="feature.txt exists",
        repo=str(repo),
        base_branch="",
        workspace_id="w1",
        mandate_id="m1",
        recipe=recipe,
    )


def _git_args(argv: list[str]) -> list[str] | None:
    """A station git argv with its hardening prefix stripped (``None`` when the
    argv is not git). Asserts the prefix is there: no station git call may run
    with fsmonitor or hooks live."""
    if argv[0] != "git":
        return None
    assert tuple(argv[: len(ds._GIT)]) == ds._GIT, argv
    return argv[len(ds._GIT) :]


def _assert_clean(repo: Path, fake: FakeClaude) -> None:
    """CLEANUP ran: only the main worktree is registered and the temp dir is gone,
    and no subprocess ever went through a shell."""
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert out.count("worktree ") == 1, out
    gits = [g for g in map(_git_args, fake.argvs) if g is not None]
    adds = [g for g in gits if g[:2] == ["worktree", "add"]]
    assert adds, "PREPARE never added a worktree"
    for g in adds:
        assert not Path(g[3]).parent.exists(), "temp dir left behind"
    for a in fake.argvs:
        assert all(isinstance(x, str) for x in a)
        assert Path(a[0]).name not in {"sh", "bash", "zsh"}, a
        assert "-c" not in a[:2] or a[0] in (PY, "git"), a


# ---------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------


async def test_green_first_try(repo):
    fake = FakeClaude(develop=[_write("ok")])
    result = await _station(fake, repo)(_request(repo))

    assert [s for s, _ in fake.claude_calls] == ["develop", "review"]
    assert "feature.txt" in result.diff and "+ok" in result.diff
    assert result.base_branch == "main"
    assert result.files_changed == 1
    assert f"check `{CHECK}`: pass" in result.summary
    assert "review: pass" in result.summary
    # Checks run as split argv, never a shell string.
    assert shlex.split(CHECK) in fake.argvs
    # The develop prompt carries task + charter, and rides stdin, not argv.
    develop_prompt = fake.claude_calls[0][1]
    assert "Add feature.txt" in develop_prompt and "never touch auth" in develop_prompt
    claude_argvs = [a for a in fake.argvs if a[0] == FAKE_CLAUDE]
    assert not any("Add feature.txt" in x for a in claude_argvs for x in a)
    develop_argv, review_argv = claude_argvs
    # Every seat: no settings files, no MCP, no hooks, web + subagents denied.
    for argv in claude_argvs:
        assert argv[argv.index("--setting-sources") + 1] == ""
        assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
        assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
        denied = argv[argv.index("--disallowedTools") + 1 : argv.index("--setting-sources")]
        assert denied == ["WebFetch", "WebSearch", "Task"]
        assert "Read(./**)" in argv and "Read" not in argv[argv.index("--allowedTools") :]
        assert "--bare" not in argv
    # Edit seat: path-scoped edits; CHECK has parens, so no Bash rule and no Bash.
    assert develop_argv[develop_argv.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write"
    assert {"Edit(./**)", "Write(./**)"} <= set(develop_argv)
    assert develop_argv[develop_argv.index("--permission-mode") + 1] == "acceptEdits"
    assert not any(x.startswith("Bash(") for x in develop_argv)
    # Review seat: read-only.
    assert review_argv[review_argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert "--permission-mode" not in review_argv and "Edit(./**)" not in review_argv
    _assert_clean(repo, fake)


def test_tool_flags_give_bash_rules_only_to_parseable_checks():
    flags = ds._tool_flags(edits=True, checks=["uv run pytest -q", CHECK])
    assert flags[flags.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write,Bash"
    assert "Bash(uv run pytest -q:*)" in flags
    assert not any(CHECK in x for x in flags)
    assert ds._tool_flags(edits=False, checks=["uv run pytest -q"])[1] == "Read,Glob,Grep"


async def test_check_red_then_fix_then_green(repo):
    fake = FakeClaude(develop=[_write("broken"), _write("ok")])
    result = await _station(fake, repo)(_request(repo))

    assert [s for s, _ in fake.claude_calls] == ["develop", "fix", "review"]
    assert "NEED OK IN feature.txt" in fake.claude_calls[1][1], "fix sees the check output"
    assert "fix attempts: 1" in result.summary
    _assert_clean(repo, fake)


async def test_checks_still_red_after_two_fixes_names_the_check(repo):
    fake = FakeClaude(develop=[_write("broken"), _write("still"), _write("nope")])
    with pytest.raises(ds.DevelopStationError) as exc:
        await _station(fake, repo)(_request(repo))

    msg = str(exc.value)
    assert msg.startswith("CHECK:") and CHECK in msg and "2 fix attempt(s)" in msg
    assert "NEED OK IN feature.txt" in msg
    assert [s for s, _ in fake.claude_calls] == ["develop", "fix", "fix"]
    _assert_clean(repo, fake)


async def test_review_fail_then_fix_then_pass(repo):
    fake = FakeClaude(
        develop=[_write("ok"), _write("ok")],
        review=[{"verdict": "fail", "notes": ["no test for feature"]}, {"verdict": "pass"}],
    )
    result = await _station(fake, repo)(_request(repo))

    assert [s for s, _ in fake.claude_calls] == ["develop", "review", "fix", "review"]
    assert "no test for feature" in fake.claude_calls[2][1]
    assert "+ok" in fake.claude_calls[1][1], "review sees the staged diff (new files too)"
    assert "review: pass" in result.summary
    _assert_clean(repo, fake)


async def test_review_still_failing_raises_review(repo):
    bad = {"verdict": "fail", "notes": ["wrong file"]}
    fake = FakeClaude(develop=[_write("ok")] * 3, review=[bad, bad, bad])
    with pytest.raises(ds.DevelopStationError, match=r"^REVIEW: .*wrong file"):
        await _station(fake, repo)(_request(repo))
    _assert_clean(repo, fake)


async def test_recipe_runs_charter_command_without_claude(repo):
    recipe_cmd = f"{PY} -c \"open('feature.txt', 'w').write('ok')\""
    fake = FakeClaude()
    result = await _station(fake, repo, recipes={"make-feature": recipe_cmd})(
        _request(repo, recipe="make-feature")
    )

    assert fake.claude_calls == []
    assert shlex.split(recipe_cmd) in fake.argvs
    assert "+ok" in result.diff
    assert "recipe: make-feature" in result.summary and "review: skipped" in result.summary
    _assert_clean(repo, fake)


async def test_unknown_recipe_fails_in_work(repo):
    fake = FakeClaude()
    with pytest.raises(ds.DevelopStationError, match=r"^WORK: recipe 'nope'"):
        await _station(fake, repo)(_request(repo, recipe="nope"))
    _assert_clean(repo, fake)


async def test_empty_diff_is_an_error(repo):
    fake = FakeClaude(develop=[lambda cwd: None])
    with pytest.raises(ds.DevelopStationError, match=r"^DONE: .*empty diff"):
        await _station(fake, repo, checks=())(_request(repo))
    _assert_clean(repo, fake)


async def test_model_env_adds_model_flag(repo, monkeypatch):
    monkeypatch.setenv("POCKETPAW_FACTORY_CLAUDE_MODEL", "opus")
    fake = FakeClaude(develop=[_write("ok")])
    await _station(fake, repo)(_request(repo))
    claude_argv = next(a for a in fake.argvs if a[0] == FAKE_CLAUDE)
    assert claude_argv[-2:] == ["--model", "opus"]
    assert foreman.claude_cli_argv("p")[:3] == [FAKE_CLAUDE, "-p", "p"]


async def test_station_error_lands_on_the_blob(repo, tmp_path, monkeypatch):
    """A station failure is recorded as ``headless_error`` on the queued run, so
    the console/digest can show it."""
    from pocketpaw.instinct.store import InstinctStore

    store = InstinctStore(tmp_path / "instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    fake = FakeClaude(develop=[_write("broken")] * 3)
    await HeadlessDevelopRunner(develop_fn=_station(fake, repo)).run(action_id)

    blob = (await store.get_action(action_id)).parameters["_code_change"]
    assert blob["station_pending"] is True and not blob["diff"]
    assert blob["headless_error"].startswith("headless develop failed: CHECK:")


# ---------------------------------------------------------------------------
# plumbing — recipe dispatch, foreman validation, cadence, charter, wiring
# ---------------------------------------------------------------------------


async def _queue_run(monkeypatch, repo: Path, *, recipe: str) -> str:
    import pocketpaw_ee.cloud.mandates.executor as ex

    async def _fake_repo(workspace_id: str, mandate_id: str) -> str:
        return str(repo)

    monkeypatch.setattr(ex, "_repo_for_mandate", _fake_repo)
    return await ex.StationTaskDispatcher().dispatch(
        workspace_id="w1",
        mandate_id="m1",
        shift_no=1,
        plan_action_id="plan-1",
        index=1,
        task={"title": "Bump photo", "why": "upstream moved", "recipe": recipe},
    )


async def test_recipe_survives_dispatch_into_develop_request(repo, tmp_path, monkeypatch):
    from pocketpaw.instinct.store import InstinctStore

    store = InstinctStore(tmp_path / "instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    action_id = await _queue_run(monkeypatch, repo, recipe="bump-photo")
    seen: list[DevelopRequest] = []

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        seen.append(req)
        return DevelopResult(diff="", base_branch="main")

    await HeadlessDevelopRunner(develop_fn=fake_develop).run(action_id)
    assert seen and seen[0].recipe == "bump-photo"


def _plan(recipe: str | None) -> foreman.PlanProposal:
    return foreman.PlanProposal(
        shift_no=1,
        tasks=[
            foreman.PlannedTask(
                title="Bump photo engine",
                why="upstream fix",
                evidence_refs=["s1"],
                expected_outcome="engine_lag down",
                recipe=recipe,
            )
        ],
    )


def test_foreman_validates_recipe_names_and_lists_them():
    charter = {"goal": "g", "recipes": {"bump-photo": "node bump.mjs photo"}}
    assert foreman.validate_plan(_plan("bump-photo"), charter) == []
    assert foreman.validate_plan(_plan(None), charter) == []
    violations = foreman.validate_plan(_plan("rm-rf"), charter)
    assert len(violations) == 1 and "'rm-rf'" in violations[0]
    assert foreman.parse_plan(json.dumps(_plan("bump-photo").model_dump())).tasks[0].recipe == (
        "bump-photo"
    )
    prompt = foreman.build_prompt(foreman.ForemanContext(shift_no=1, charter=charter))
    assert "== RECIPES" in prompt and "- bump-photo" in prompt and '"recipe": null' in prompt


async def _make_mandate(cadence: str, **charter_extra) -> str:
    from pocketpaw_ee.cloud.mandates import service

    created = await service.create_mandate(
        "w1",
        "u1",
        {
            "name": f"m-{cadence}",
            "surface": {"repo_id": "/tmp/x"},
            "charter": {"goal": "g", "cadence": cadence, **charter_extra},
        },
    )
    return created["mandate"]["id"]


async def test_daily_cadence_due_after_one_day(mongo_db):
    from pocketpaw_ee.cloud.mandates import service
    from pocketpaw_ee.cloud.mandates.domain import ShiftDoc

    daily = await _make_mandate("daily")
    now = datetime(2026, 10, 6, tzinfo=UTC)
    assert daily in {r["mandate_id"] for r in await service.list_cadence_due(now)}

    shift = ShiftDoc(workspace="w1", mandate_id=daily, no=1, state="done")
    shift.createdAt = now - timedelta(hours=6)
    await shift.insert()
    assert daily not in {r["mandate_id"] for r in await service.list_cadence_due(now)}
    later = now + timedelta(days=1)
    assert daily in {r["mandate_id"] for r in await service.list_cadence_due(later)}


async def test_charter_checks_and_recipes_round_trip(mongo_db):
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.mandates import service

    mid = await _make_mandate(
        "manual", checks=["uv run pytest -q"], recipes={"bump-photo": "node b.mjs photo"}
    )
    found = await service.charter_for_mandate("w1", mid)
    assert found["repo"] == "/tmp/x"
    assert found["charter"]["checks"] == ["uv run pytest -q"]
    assert found["charter"]["recipes"] == {"bump-photo": "node b.mjs photo"}
    assert await service.charter_for_mandate("other-ws", mid) is None
    with pytest.raises((CloudError, ValueError)):
        await _make_mandate("manual", checks=["'unbalanced"])


async def test_production_dispatcher_develops_in_background(repo, tmp_path, monkeypatch):
    """The production headless dispatcher returns once the queued run is filed
    (approval must not block on a develop) and attaches the diff afterwards."""
    import asyncio

    import pocketpaw_ee.cloud.belt.headless as headless
    import pocketpaw_ee.cloud.mandates.executor as ex

    from pocketpaw.instinct.store import InstinctStore

    store = InstinctStore(tmp_path / "instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)

    async def _fake_repo(workspace_id: str, mandate_id: str) -> str:
        return str(repo)

    monkeypatch.setattr(ex, "_repo_for_mandate", _fake_repo)
    release = asyncio.Event()

    async def slow_develop(req: DevelopRequest) -> DevelopResult:
        await release.wait()
        return DevelopResult(diff="diff --git a/x b/x\n", base_branch="main")

    set_production_develop_fn(slow_develop)
    try:
        dispatcher = resolve_headless_dispatcher()
        assert dispatcher is not None and dispatcher.background is True
        run_ref = await dispatcher.dispatch(
            workspace_id="w1",
            mandate_id="m1",
            shift_no=1,
            plan_action_id="plan-1",
            index=1,
            task={"title": "t"},
        )
        blob = (await store.get_action(run_ref)).parameters["_code_change"]
        assert blob["station_pending"] is True, "dispatch returned before the develop ran"
        release.set()
        await asyncio.gather(*headless._BACKGROUND_DEVELOPS)
        blob = (await store.get_action(run_ref)).parameters["_code_change"]
        assert blob["station_pending"] is False and blob["diff"].startswith("diff --git")
    finally:
        set_production_develop_fn(None)


def test_wire_from_env(monkeypatch):
    from pocketpaw_ee.cloud.shared import db as cloud_db

    monkeypatch.setattr(cloud_db, "is_multi_tenant_cloud", lambda: False)
    monkeypatch.delenv("POCKETPAW_FACTORY_DEVELOP", raising=False)
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "headless")
    try:
        assert ds.wire_from_env() is False
        assert resolve_headless_dispatcher() is None
        monkeypatch.setenv("POCKETPAW_FACTORY_DEVELOP", "claude")
        assert ds.wire_from_env() is True
        assert resolve_headless_dispatcher() is not None
    finally:
        set_production_develop_fn(None)


# ---------------------------------------------------------------------------
# hardening — env scrub, process-group kill
# ---------------------------------------------------------------------------

NO_SECRET_CHECK = f"{PY} -c \"import os,sys; sys.exit(1 if 'SECRET_TOKEN' in os.environ else 0)\""


async def test_checks_run_without_secret_env(repo, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "planted-secret-value")
    fake = FakeClaude(develop=[_write("ok")])
    result = await _station(fake, repo, checks=(CHECK, NO_SECRET_CHECK))(_request(repo))
    assert f"check `{NO_SECRET_CHECK}`: pass" in result.summary


async def test_scrubbed_env_keeps_only_the_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "planted")
    monkeypatch.setenv("POCKETPAW_CLOUD_MONGODB_URI", "mongodb://u:p@h")
    code, out, _ = await ds.run_subprocess(
        [PY, "-c", "import os, json; print(json.dumps(sorted(os.environ)))"],
        cwd=tmp_path,
        timeout=30,
    )
    keys = set(json.loads(out))
    assert code == 0
    assert "SECRET_TOKEN" not in keys and "POCKETPAW_CLOUD_MONGODB_URI" not in keys
    assert {"PATH", "HOME"} <= keys
    # The child python / macOS add LC_CTYPE and __CF_USER_TEXT_ENCODING themselves.
    extra = {"PYTHONDONTWRITEBYTECODE", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert keys <= set(ds._ENV_KEYS) | extra


_SPAWN_SLEEPER = (
    "import subprocess, sys, time; "
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "open(sys.argv[1], 'w').write(str(p.pid)); time.sleep(60)"
)


async def _gone(pid: int) -> bool:
    import asyncio
    import os

    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_timeout_kills_the_process_group(tmp_path):
    pidfile = tmp_path / "pid"
    code, _, err = await ds.run_subprocess(
        [PY, "-c", _SPAWN_SLEEPER, str(pidfile)], cwd=tmp_path, timeout=2
    )
    assert code == -1 and "timed out" in err
    assert await _gone(int(pidfile.read_text())), "grandchild survived the timeout"


async def test_cancel_kills_the_process_group(tmp_path):
    import asyncio

    pidfile = tmp_path / "pid"
    task = asyncio.create_task(
        ds.run_subprocess([PY, "-c", _SPAWN_SLEEPER, str(pidfile)], cwd=tmp_path, timeout=60)
    )
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _gone(int(pidfile.read_text())), "grandchild survived the cancel"


# ---------------------------------------------------------------------------
# hardening — worktree integrity, protected paths
# ---------------------------------------------------------------------------


async def test_rewritten_worktree_git_file_fails_the_run(repo):
    def tamper(cwd: Path) -> None:
        (cwd / "feature.txt").write_text("ok\n")
        (cwd / ".git").write_text("gitdir: ./evil\n")

    fake = FakeClaude(develop=[tamper])
    with pytest.raises(ds.DevelopStationError, match=r"^INTEGRITY: worktree \.git changed"):
        await _station(fake, repo)(_request(repo))
    gits = [g for g in map(_git_args, fake.argvs) if g]
    assert ["add", "-A"] not in [g[:2] for g in gits], "git ran on the tampered worktree"
    _assert_clean(repo, fake)


async def test_git_file_swapped_for_a_directory_fails_the_run(repo):
    def tamper(cwd: Path) -> None:
        (cwd / ".git").unlink()
        (cwd / ".git").mkdir()

    fake = FakeClaude(develop=[tamper])
    with pytest.raises(ds.DevelopStationError, match=r"^INTEGRITY:"):
        await _station(fake, repo, checks=())(_request(repo))
    _assert_clean(repo, fake)


@pytest.mark.parametrize("planted", [".claude/settings.json", ".mcp.json", "sub/.gitmodules"])
async def test_diff_touching_agent_config_is_refused(repo, planted):
    def develop(cwd: Path) -> None:
        (cwd / "feature.txt").write_text("ok\n")
        target = cwd / planted
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"permissions": {"allow": ["Bash"]}}\n')

    fake = FakeClaude(develop=[develop])
    with pytest.raises(ds.DevelopStationError, match=r"^DONE: .*protected paths") as exc:
        await _station(fake, repo)(_request(repo))
    assert planted in str(exc.value)
    _assert_clean(repo, fake)


@pytest.mark.parametrize(
    ("checks", "recipes", "recipe", "match"),
    [
        (("bash -c 'echo pwned > /tmp/x'",), None, "", r"^CHECK: command 'bash' is not allowed"),
        (("./node_modules/.bin/x",), None, "", r"^CHECK: .*relative path"),
        ((), {"r": "git commit -am x"}, "r", r"^WORK: command 'git' is not allowed"),
    ],
)
async def test_station_refuses_disallowed_programs_before_exec(
    repo, monkeypatch, checks, recipes, recipe, match
):
    """A charter stored before the DTO rule (or an allowlist narrowed since) is
    still refused by the station, and the program never runs."""
    monkeypatch.delenv("POCKETPAW_FACTORY_ALLOWED_COMMANDS", raising=False)
    fake = FakeClaude(develop=[_write("ok")])
    with pytest.raises(ds.DevelopStationError, match=match):
        await _station(fake, repo, checks=checks, recipes=recipes)(_request(repo, recipe=recipe))
    ran = {Path(a[0]).name for a in fake.argvs}
    assert not ran & {"bash", "x"} and ["git", "commit"] not in [a[:2] for a in fake.argvs]
    if checks:  # refused up front: no worktree, no LLM spend
        assert fake.argvs == []
    else:
        _assert_clean(repo, fake)


def test_wire_from_env_refuses_a_multi_tenant_process(monkeypatch, caplog):
    import logging

    from pocketpaw_ee.cloud.shared import db as cloud_db

    monkeypatch.setattr(cloud_db, "is_multi_tenant_cloud", lambda: True)
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "headless")
    monkeypatch.setenv("POCKETPAW_FACTORY_DEVELOP", "claude")
    monkeypatch.delenv("POCKETPAW_FACTORY_DEDICATED_HOST", raising=False)
    try:
        with caplog.at_level(logging.ERROR, logger=ds.__name__):
            assert ds.wire_from_env() is False
        assert resolve_headless_dispatcher() is None
        assert any(
            r.levelno == logging.ERROR and "POCKETPAW_FACTORY_DEDICATED_HOST" in r.getMessage()
            for r in caplog.records
        )
        monkeypatch.setenv("POCKETPAW_FACTORY_DEDICATED_HOST", "1")
        assert ds.wire_from_env() is True
        assert resolve_headless_dispatcher() is not None
    finally:
        set_production_develop_fn(None)
