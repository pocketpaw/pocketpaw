# tests/cloud/test_belt_verify.py — the develop station's MECHANICAL gate.
# Created: 2026-09-12 (feat/belt-gate).
#
# What this pins — verification runs BEFORE the human, on REAL git repos and
# REAL pytest subprocesses (no mocking of git, no mocking of the runner; the
# memory here is that over-mocking hides live bugs):
#
#   verify_diff (ee/pocketpaw_ee/cloud/belt/verify.py)
#     * a diff whose tests pass          → status "passed"
#     * a diff whose tests fail          → status "failed", the check is named
#     * a repo with no test command      → status "no_checks" (NOT a pass)
#     * an internal explosion            → fails CLOSED (verify_internal, ok=False)
#     * a check that exceeds the timeout → ok=False
#     * a diff that doesn't apply        → a NAMED git_apply check, not an
#                                          opaque internal error
#     * the throwaway worktree is removed AND its registration pruned on BOTH
#       the happy and the exception path (a dir-gone assertion alone passes with
#       a stale `git worktree list` entry).
#     * the check imports from the APPLIED tree: a src-layout repo verifies green
#       only because the ambient-pytest path puts the tree's `src` on PYTHONPATH
#       ahead of any editable install. Drop that root and a good diff goes red on
#       an ImportError — or, worse, resolves to the ORIGINAL checkout, where the
#       diff's source change does not exist.
#
#   belt_propose_change (the real MCP handler)
#     * verification passed   → the Action IS filed, the blob carries
#                               `verification` (status + per-check metadata)
#     * verification failed   → an is_error response naming the failing check,
#                               and NO Action in the store
#     * no_checks             → the Action IS filed
#     * belt_verify_enabled=False → verify_diff is never called, the Action is
#                               filed, `verification` records "disabled"
#
# Fixture style follows tests/cloud/test_belt_gate.py (local git repos, a tmp
# InstinctStore, identity via the agent ContextVars). NOTE: pytest addopts hides
# tests/cloud — run this file by explicit path.

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_PATH = os.environ.get("PATH", "/usr/bin:/bin")

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.mcp_servers.belt as belt  # noqa: E402
from pocketpaw_ee.cloud.belt import verify as belt_verify  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """Run git from an arg list (no shell), assert success, return stdout."""
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


def _seed(work: Path, files: dict[str, str]) -> Path:
    """Init a local git repo (no origin) at ``work`` with ``files`` committed on
    ``main``. Local-only keeps the fixture fast — verify's base-ref resolution
    handles both, and the with-origin half is already pinned by test_belt_gate."""
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init")
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")
    for rel, body in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    return work


# A minimal pyproject is all the discovery needs to call this a Python repo.
_PYPROJECT = '[project]\nname = "widget"\nversion = "0.1.0"\n'

_APP = "def greet():\n    return 'hi'\n"
_APP_NEW = "def greet():\n    return 'hi there'\n"
_APP_BROKEN = "def greet():\n    return 'BROKEN'\n"
_TEST = "from app import greet\n\n\ndef test_greet():\n    assert greet() == 'hi'\n"
_TEST_NEW = "from app import greet\n\n\ndef test_greet():\n    assert greet() == 'hi there'\n"


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    """A Python repo: pyproject + a module + a test that passes on the CURRENT
    source. A diff that keeps the test true verifies green; one that breaks the
    module verifies red."""
    return _seed(
        tmp_path / "py-repo",
        {"pyproject.toml": _PYPROJECT, "app.py": _APP, "test_app.py": _TEST},
    )


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    """A repo with NO pyproject and NO package.json — nothing to run."""
    return _seed(tmp_path / "plain-repo", {"README.md": "# docs\n"})


def _diff(path: str, old: str, new: str) -> str:
    """A one-hunk unified diff replacing whole-file content."""
    body = "".join(f"-{line}\n" for line in old.splitlines())
    body += "".join(f"+{line}\n" for line in new.splitlines())
    head = f"@@ -1,{len(old.splitlines())} +1,{len(new.splitlines())} @@"
    return f"--- a/{path}\n+++ b/{path}\n{head}\n{body}"


def _passing_diff() -> str:
    """Changes app.py AND its test together — green."""
    return _diff("app.py", _APP, _APP_NEW) + _diff("test_app.py", _TEST, _TEST_NEW)


def _failing_diff() -> str:
    """Changes app.py and leaves the test asserting the old value — red."""
    return _diff("app.py", _APP, _APP_BROKEN)


class _identity:
    """Sets the workspace/user/session ContextVars the handler reads."""

    def __init__(self, *, workspace="w1", user="u1", session="sess-1"):
        self._ws, self._user, self._sess = workspace, user, session
        self._tokens = None

    def __enter__(self):
        self._tokens = attach_agent_identity(
            workspace_id=self._ws, user_id=self._user, session_mongo_id=self._sess
        )
        return self

    def __exit__(self, *exc):
        detach_agent_identity(self._tokens)
        return False


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore on a tmp file, workspace-faithful (mirrors
    test_belt_gate's fixture — the seeded store answers only for ``w1``)."""
    from pocketpaw.stores import current_workspace

    st = InstinctStore(tmp_path / "instinct_belt_verify.db")
    others: dict[str, InstinctStore] = {}

    def _factory(*_a, workspace_id: str | None = None, **_k) -> InstinctStore:
        ws = str((workspace_id if workspace_id is not None else current_workspace.get()) or "")
        if ws == "w1":
            return st
        return others.setdefault(ws, InstinctStore(tmp_path / f"instinct_other_{ws or 'none'}.db"))

    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", _factory)
    return st


@pytest.fixture
def settings_patch(monkeypatch):
    """Patch ``pocketpaw.config.get_settings`` with overrides layered on the real
    settings — the allowlist plus whatever the test needs."""

    def _apply(**overrides):
        from pocketpaw.config import get_settings

        real = get_settings()

        class _S:
            def __getattr__(self, name):
                if name in overrides:
                    return overrides[name]
                return getattr(real, name)

        monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())

    return _apply


async def _propose(repo: Path, diff: str, **extra) -> dict:
    """Call the REAL MCP handler under identity."""
    with _identity():
        return await belt._propose_change_handler(
            {
                "repo": str(repo),
                "base_branch": "main",
                "diff": diff,
                "summary": "Change the greeting.",
                "task": "Make greet() friendlier.",
                **extra,
            }
        )


def _worktrees(repo: Path) -> list[str]:
    """Registered worktree paths for ``repo``, excluding the repo itself."""
    out = _git(repo, "worktree", "list", "--porcelain")
    paths = [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]
    return [p for p in paths if Path(p).resolve() != repo.resolve()]


# ---------------------------------------------------------------------------
# verify_diff — the runner
# ---------------------------------------------------------------------------


async def test_passing_diff_verifies_green_and_cleans_up(py_repo):
    """A diff whose tests pass → status 'passed', the pytest check RAN (not
    skipped), and the throwaway worktree is gone AND unregistered.

    This also pins that the APPLIED tree is what gets tested: the diff changes
    app.py and its test together, so a verifier reading the original checkout
    would fail the new assertion."""
    result = await belt_verify.verify_diff(
        repo=str(py_repo), base_branch="main", diff=_passing_diff(), timeout_s=120
    )

    assert result.status == "passed", result.checks
    assert len(result.checks) == 1
    check = result.checks[0]
    assert check.name.startswith("pytest")
    assert check.ok is True
    assert check.skipped is False
    assert check.duration_s > 0
    assert "passed" in result.summary

    assert _worktrees(py_repo) == []


async def test_failing_diff_verifies_red_and_names_the_check(py_repo):
    """A diff that breaks the test → status 'failed', the failing check is named
    and carries the pytest output the agent needs to fix it."""
    result = await belt_verify.verify_diff(
        repo=str(py_repo), base_branch="main", diff=_failing_diff(), timeout_s=120
    )

    assert result.status == "failed"
    failed = [c for c in result.checks if not c.ok]
    assert [c.name for c in failed] == ["pytest(ambient)"]
    assert "test_greet" in failed[0].output
    assert _worktrees(py_repo) == []


async def test_repo_with_no_test_command_is_no_checks(plain_repo):
    """No pyproject, no package.json → 'no_checks'. NOT a pass: nothing ran."""
    diff = _diff("README.md", "# docs\n", "# docs\n\nmore\n")
    result = await belt_verify.verify_diff(
        repo=str(plain_repo), base_branch="main", diff=diff, timeout_s=120
    )

    assert result.status == "no_checks"
    assert result.checks == ()
    assert _worktrees(plain_repo) == []


async def test_pytest_collecting_nothing_is_skipped_not_passed(tmp_path):
    """A Python repo with no tests → the check is SKIPPED (pytest exit 5) and the
    overall status is 'no_checks'. Exit 5 must never read as proof."""
    repo = _seed(tmp_path / "empty-py", {"pyproject.toml": _PYPROJECT, "app.py": "VALUE = 1\n"})
    diff = _diff("app.py", "VALUE = 1\n", "VALUE = 2\n")

    result = await belt_verify.verify_diff(
        repo=str(repo), base_branch="main", diff=diff, timeout_s=120
    )

    assert [(c.name, c.ok, c.skipped) for c in result.checks] == [("pytest(ambient)", True, True)]
    assert result.status == "no_checks"


async def test_node_repo_without_an_install_is_skipped_not_failed(tmp_path):
    """A JS repo's suite cannot run in a fresh worktree (no node_modules), so it
    is SKIPPED. Running it anyway would fail on missing modules and refuse a
    perfectly good diff — an install-state failure is not a code failure."""
    repo = _seed(
        tmp_path / "node-repo",
        {
            "package.json": '{"name":"w","scripts":{"test":"vitest run"}}\n',
            "bun.lock": "{}\n",
            "index.js": "export const V = 1;\n",
        },
    )
    diff = _diff("index.js", "export const V = 1;\n", "export const V = 2;\n")

    result = await belt_verify.verify_diff(
        repo=str(repo), base_branch="main", diff=diff, timeout_s=120
    )

    assert [(c.name, c.ok, c.skipped) for c in result.checks] == [("bun test", True, True)]
    assert result.status == "no_checks"


async def test_node_repo_without_a_test_script_discovers_nothing(tmp_path):
    """No ``test`` script → no check at all (distinct from a skipped one)."""
    repo = _seed(
        tmp_path / "node-notest",
        {
            "package.json": '{"name":"w","scripts":{"build":"tsc"}}\n',
            "index.js": "export const V = 1;\n",
        },
    )
    diff = _diff("index.js", "export const V = 1;\n", "export const V = 2;\n")

    result = await belt_verify.verify_diff(
        repo=str(repo), base_branch="main", diff=diff, timeout_s=120
    )

    assert result.checks == ()
    assert result.status == "no_checks"


async def test_src_layout_repo_imports_from_the_applied_tree(tmp_path):
    """A src-layout repo verifies green — which only happens if the APPLIED
    tree's ``src`` is on the check's import path.

    Mutation that breaks this: drop ``tree / "src"`` from the ambient check's
    PYTHONPATH roots. pytest's own rootdir insertion adds the rootdir, not
    ``rootdir/src``, so ``from widget import VALUE`` then fails to import and the
    check goes red on a perfectly good diff."""
    repo = _seed(
        tmp_path / "src-layout",
        {
            "pyproject.toml": _PYPROJECT,
            "src/widget/__init__.py": "VALUE = 1\n",
            "tests/test_widget.py": (
                "from widget import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n"
            ),
        },
    )
    diff = _diff("src/widget/__init__.py", "VALUE = 1\n", "VALUE = 2\n")

    result = await belt_verify.verify_diff(
        repo=str(repo), base_branch="main", diff=diff, timeout_s=120
    )

    assert result.status == "passed", result.checks[0].output
    assert result.checks[0].skipped is False


async def test_internal_error_fails_closed_and_still_cleans_up(py_repo, monkeypatch):
    """A verifier that explodes must FAIL, not wave the change through — and it
    must still tear the worktree down."""

    # Explode AFTER the worktree exists, so the teardown under test is the real
    # one (not a no-op before anything was created).
    async def _explode(*_a, **_k):
        raise RuntimeError("git went sideways")

    monkeypatch.setattr(belt_verify, "_pytest_check", _explode)

    result = await belt_verify.verify_diff(
        repo=str(py_repo), base_branch="main", diff=_passing_diff(), timeout_s=120
    )

    assert result.status == "failed"
    assert [(c.name, c.ok) for c in result.checks] == [("verify_internal", False)]
    assert "git went sideways" in result.checks[0].output
    # Dir gone AND registration pruned — a stale registration passes the first
    # assertion alone.
    assert _worktrees(py_repo) == []


async def test_timeout_is_a_failure(tmp_path):
    """A check that outruns the timeout is killed and counts as ok=False."""
    repo = _seed(
        tmp_path / "slow-py",
        {
            "pyproject.toml": _PYPROJECT,
            "test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(30)\n",
        },
    )
    diff = _diff(
        "test_slow.py",
        "import time\n\n\ndef test_slow():\n    time.sleep(30)\n",
        "import time\n\n\ndef test_slow():\n    time.sleep(45)\n",
    )

    result = await belt_verify.verify_diff(
        repo=str(repo), base_branch="main", diff=diff, timeout_s=1
    )

    assert result.status == "failed"
    assert result.checks[0].ok is False
    assert "timed out" in result.checks[0].output
    assert _worktrees(repo) == []


async def test_non_applying_diff_is_a_named_git_apply_check(py_repo):
    """A stale/conflicting diff is actionable feedback ('re-propose'), not an
    opaque verify_internal."""
    stale = _diff(
        "app.py",
        "def greet():\n    return 'NEVER WAS'\n",
        "def greet():\n    return 'x'\n",
    )

    result = await belt_verify.verify_diff(
        repo=str(py_repo), base_branch="main", diff=stale, timeout_s=120
    )

    assert result.status == "failed"
    assert [c.name for c in result.checks] == ["git_apply"]
    assert "re-propose" in result.checks[0].output
    assert _worktrees(py_repo) == []


# ---------------------------------------------------------------------------
# belt_propose_change — the gate wiring
# ---------------------------------------------------------------------------


async def test_propose_files_the_action_and_attaches_verification(py_repo, store, settings_patch):
    """Green verification → the Action IS filed and the blob carries the verdict
    (status + per-check metadata + summary, and NO full logs)."""
    settings_patch(belt_repo_allowlist=[str(py_repo.parent)])

    res = await _propose(py_repo, _passing_diff())
    assert res.get("is_error") is not True, res
    body = json.loads(res["content"][0]["text"])

    action = await store.get_action(body["action_id"])
    assert action is not None
    verification = action.parameters["_code_change"]["verification"]
    assert verification["status"] == "passed"
    assert verification["checks"][0]["ok"] is True
    assert verification["checks"][0]["skipped"] is False
    assert "summary" in verification
    # Metadata only — a whole test log must never ride in the Instinct blob.
    assert "output" not in verification["checks"][0]


async def test_propose_is_refused_when_verification_fails(py_repo, store, settings_patch):
    """Red verification → an error response naming the failing check, and NO
    Action filed. This is the gate: a human never sees unverified work."""
    settings_patch(belt_repo_allowlist=[str(py_repo.parent)])

    res = await _propose(py_repo, _failing_diff())

    assert res["is_error"] is True
    text = res["content"][0]["text"]
    assert "verification FAILED" in text
    assert "pytest(ambient)" in text  # the failing check name reaches the caller
    assert "test_greet" in text  # ...and so does the output that explains it

    assert await store.list_actions(workspace_id="w1") == []


async def test_propose_files_the_action_when_there_is_nothing_to_run(
    plain_repo, store, settings_patch
):
    """'no_checks' is not a failure — the proposal still reaches the human, who
    can see that nothing was proven."""
    settings_patch(belt_repo_allowlist=[str(plain_repo.parent)])

    res = await _propose(plain_repo, _diff("README.md", "# docs\n", "# docs\n\nmore\n"))
    assert res.get("is_error") is not True, res
    body = json.loads(res["content"][0]["text"])

    action = await store.get_action(body["action_id"])
    assert action is not None
    assert action.parameters["_code_change"]["verification"] == {
        "status": "no_checks",
        "checks": [],
        "summary": "no mechanical checks discovered for this repo",
    }


async def test_disabled_setting_skips_verification_entirely(
    py_repo, store, settings_patch, monkeypatch
):
    """belt_verify_enabled=False → verify_diff is NEVER called (a diff that would
    fail verification still files) and the blob records 'disabled'."""
    settings_patch(belt_repo_allowlist=[str(py_repo.parent)], belt_verify_enabled=False)

    async def _never(**_k):
        raise AssertionError("verify_diff must not run when the gate is disabled")

    monkeypatch.setattr(belt_verify, "verify_diff", _never)

    res = await _propose(py_repo, _failing_diff())
    assert res.get("is_error") is not True, res
    body = json.loads(res["content"][0]["text"])

    action = await store.get_action(body["action_id"])
    assert action is not None
    assert action.parameters["_code_change"]["verification"] == {"status": "disabled"}


async def test_timeout_setting_is_threaded_through(py_repo, store, settings_patch, monkeypatch):
    """The per-call timeout comes from settings, not a module constant."""
    settings_patch(belt_repo_allowlist=[str(py_repo.parent)], belt_verify_timeout_s=42)
    seen: dict = {}

    async def _capture(**kwargs):
        seen.update(kwargs)
        return belt_verify.VerifyResult(status="no_checks", checks=(), summary="none")

    monkeypatch.setattr(belt_verify, "verify_diff", _capture)

    res = await _propose(py_repo, _passing_diff())
    assert res.get("is_error") is not True, res
    assert seen["timeout_s"] == 42
    assert seen["base_branch"] == "main"
