# tests/cloud/test_belt_verify.py — the develop station's MECHANICAL gate.
# Created: 2026-09-12 (feat/belt-gate).
# Updated: 2026-09-12 (headless gate) — a final section pins that BOTH develop
#   paths reach the gate. The gate shipped with one call site and the headless
#   runner walked around it; one recorder patched once, with the interactive MCP
#   handler and the headless runner both driven for real, is the only shape of
#   test that notices a path going missing. The per-verdict headless behaviour
#   (fail closed, disabled, no_checks, an exploding verifier) is in
#   ``test_belt_headless.py``.
# Updated: 2026-09-12 — per-repo verify commands. See the second section below.
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
#   per-repo commands (belt_verify_commands + the built-in pocketpaw default)
#     * an operator's argv REPLACES discovery, and is matched on a resolved path
#     * it is held to the same evidence rule (proves nothing → no_checks) and
#       FAILS rather than skipping when it cannot launch — a typo in settings
#       must not switch the gate off
#     * pocketpaw is recognised by [project].name and gets a real, targeted run
#     * derivation trusts `test_<parent>_<stem>.py`, and a bare `test_<stem>.py`
#       only in the module's mirrored directory — a same-named test in an
#       unrelated tree is proof of nothing wearing a passing count
#     * a repo with no configured command keeps discovery byte-for-byte
#
# Fixture style follows tests/cloud/test_belt_gate.py (local git repos, a tmp
# InstinctStore, identity via the agent ContextVars). NOTE: pytest addopts hides
# tests/cloud — run this file by explicit path.

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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


async def test_all_skipped_suite_is_not_a_pass(tmp_path):
    """A suite that exits 0 having PASSED nothing proved nothing → skipped.

    This is the pocketpaw shape, not a hypothetical: `uv run pytest` in a fresh
    worktree syncs default groups only, so pocketpaw_ee is missing, every
    tests/ee module importorskips, and the run exits 0 having exercised nothing.
    On the exit code alone the gate would green the very change it is least able
    to verify."""
    repo = _seed(
        tmp_path / "all-skipped",
        {
            "pyproject.toml": _PYPROJECT,
            "app.py": "VALUE = 1\n",
            # Collected and then skipped at RUNTIME — pytest exits 0, which the
            # exit-5 rule does not catch. (A module-level importorskip exits 5
            # instead and is already covered by the no-tests-collected case.)
            "test_app.py": (
                "import pytest\n\n\n"
                '@pytest.mark.skip(reason="stands in for an ee importorskip")\n'
                "def test_never_runs():\n    assert False\n"
            ),
        },
    )
    diff = _diff("app.py", "VALUE = 1\n", "VALUE = 2\n")

    result = await belt_verify.verify_diff(
        repo=str(repo), base_branch="main", diff=diff, timeout_s=120
    )

    check = result.checks[0]
    assert (check.ok, check.skipped) == (True, True), check.output
    assert "No test PASSED" in check.output
    assert result.status == "no_checks"


async def test_uv_lock_without_pytest_is_skipped_not_failed(tmp_path):
    """A uv-locked repo whose lock has no pytest → skipped. Running `uv run
    pytest` there exits non-zero on a missing command, which would refuse a
    perfectly good diff."""
    if not shutil.which("uv"):
        pytest.skip("uv is not on PATH, so the uv branch cannot be reached")
    repo = _seed(
        tmp_path / "uv-no-pytest",
        {
            "pyproject.toml": _PYPROJECT,
            # A lock that resolves something, but not pytest.
            "uv.lock": 'version = 1\n\n[[package]]\nname = "idna"\nversion = "3.7"\n',
            "app.py": "VALUE = 1\n",
        },
    )
    diff = _diff("app.py", "VALUE = 1\n", "VALUE = 2\n")

    result = await belt_verify.verify_diff(
        repo=str(repo), base_branch="main", diff=diff, timeout_s=120
    )

    assert [(c.name, c.ok, c.skipped) for c in result.checks] == [("pytest(uv)", True, True)]
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


# ---------------------------------------------------------------------------
# Per-repo verify commands — the gap generic discovery left on our OWN repo
# ---------------------------------------------------------------------------
#
# Discovery guesses the runner from the tree's shape, and on pocketpaw the guess
# is wrong in a way that reads as honest: `uv run pytest` in a throwaway worktree
# syncs the default groups only, pocketpaw_ee is absent, every tests/ee module
# importorskips, addopts hides tests/cloud, and _require_evidence demotes the
# whole thing to no_checks. Not a false pass — but no verification either, on the
# repo the gate was built for. These pin the two ways out: an operator's per-repo
# argv, and a built-in default for pocketpaw itself.

_PP_PYPROJECT = (
    '[project]\nname = "pocketpaw"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n\n'
    '[dependency-groups]\ndev = ["pytest"]\nee = []\n'
)
_PP_MODULE = "def greet():\n    return 'hi'\n"
_PP_MODULE_NEW = "def greet():\n    return 'hi there'\n"
_PP_TEST = (
    "from widgets.thing import greet\n\n\n"
    "def test_greet():\n    assert greet() in ('hi', 'hi there')\n"
)

_needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="the built-in default shells uv")


@pytest.fixture
def pocketpaw_repo(tmp_path: Path) -> Path:
    """A repo shaped like pocketpaw — identified by [project].name, which is how
    the built-in default recognises it (no operator would key a worktree path,
    and every checkout deserves the same treatment).

    Deliberately tiny: it declares the same `ee` and `dev` groups the real
    default syncs, so the REAL argv runs here in ~2s instead of the real suite's
    forever. `widgets/thing.py` mirrors into `tests/widgets/test_thing.py` — the
    layout the derivation trusts — and that test is NOT in the diff below, so
    the built-in has to find it from the touched module."""
    return _seed(
        tmp_path / "pocketpaw",
        {
            "pyproject.toml": _PP_PYPROJECT,
            # A root conftest puts the repo root on sys.path, so the test below
            # imports the APPLIED tree's module rather than failing to find it.
            "conftest.py": "",
            "widgets/__init__.py": "",
            "widgets/thing.py": _PP_MODULE,
            "tests/widgets/test_thing.py": _PP_TEST,
            # A module nothing covers — the skip case.
            "orphan.py": "X = 1\n",
        },
    )


async def test_configured_command_wins_over_discovery(py_repo):
    """An explicit per-repo command REPLACES discovery — it does not run beside
    it.

    py_repo's discovery would find pytest and pass; the configured command
    fails. Red proves the configured one ran; exactly one check proves discovery
    did not also run (two checks would mean the operator's answer was merely
    added to a guess)."""
    result = await belt_verify.verify_diff(
        repo=str(py_repo),
        base_branch="main",
        diff=_passing_diff(),
        timeout_s=120,
        commands={str(py_repo): [sys.executable, "-c", "import sys; sys.exit(3)"]},
    )

    assert result.status == "failed", result.summary
    assert [c.name for c in result.checks] == ["configured"]


async def test_configured_command_matches_an_unresolved_key(py_repo, tmp_path):
    """The key is a repo PATH in the allowlist's form, so it is resolved on BOTH
    sides — a `..` hop or a trailing slash in config still names the same repo.
    A literal string compare would miss this and silently fall back to
    discovery."""
    detour = f"{py_repo.parent}/./{py_repo.name}/../{py_repo.name}/"

    result = await belt_verify.verify_diff(
        repo=str(py_repo),
        base_branch="main",
        diff=_passing_diff(),
        timeout_s=120,
        commands={detour: [sys.executable, "-c", "import sys; sys.exit(3)"]},
    )

    assert [c.name for c in result.checks] == ["configured"]
    assert result.status == "failed"


async def test_configured_command_that_proves_nothing_is_no_checks(py_repo):
    """A configured command that RUNS, exits 0 and shows no passing count is
    held to the same evidence rule as a discovered one: skipped, so the run
    lands on 'no_checks'.

    Never 'passed'. Configuring a command is not the same as proving something
    with it, and the gate must not accept the former as the latter."""
    result = await belt_verify.verify_diff(
        repo=str(py_repo),
        base_branch="main",
        diff=_passing_diff(),
        timeout_s=120,
        commands={str(py_repo): [sys.executable, "-c", "print('nothing to do here')"]},
    )

    assert result.status == "no_checks"
    check = result.checks[0]
    assert (check.ok, check.skipped) == (True, True)
    assert "No test PASSED" in check.output


async def test_configured_command_that_cannot_launch_is_a_failure(py_repo):
    """A command that does not exist FAILS — it does not skip.

    This is the whole reason the property is pinned: a skip here would mean one
    typo in settings silently switches the gate off for that repo, and every
    proposal after it sails through unverified looking fine."""
    result = await belt_verify.verify_diff(
        repo=str(py_repo),
        base_branch="main",
        diff=_passing_diff(),
        timeout_s=120,
        commands={str(py_repo): ["pocketpaw-no-such-binary-xyz", "--run"]},
    )

    assert result.status == "failed"
    check = result.checks[0]
    assert (check.name, check.ok, check.skipped) == ("configured", False, False)
    assert "FileNotFoundError" in check.output


async def test_configured_failure_reaches_the_caller(py_repo, store, settings_patch):
    """A red configured command refuses the propose, and the check name AND its
    output ride back to the agent — otherwise the feedback loop has nothing to
    act on and no Action for a human to see either."""
    settings_patch(
        belt_repo_allowlist=[str(py_repo.parent)],
        belt_verify_commands={
            str(py_repo): [sys.executable, "-c", "import sys; print('BOOM-42'); sys.exit(1)"]
        },
    )

    res = await _propose(py_repo, _passing_diff())

    assert res["is_error"] is True
    text = res["content"][0]["text"]
    assert "verification FAILED" in text
    assert "configured" in text
    assert "BOOM-42" in text
    assert await store.list_actions(workspace_id="w1") == []


async def test_commands_setting_is_threaded_through(py_repo, store, settings_patch, monkeypatch):
    """The map comes from settings via the handler, like the timeout — verify.py
    never reads settings itself, which is what keeps it testable without them."""
    settings_patch(
        belt_repo_allowlist=[str(py_repo.parent)],
        belt_verify_commands={"/srv/other": ["make", "check"]},
    )
    seen: dict = {}

    async def _capture(**kwargs):
        seen.update(kwargs)
        return belt_verify.VerifyResult(status="no_checks", checks=(), summary="none")

    monkeypatch.setattr(belt_verify, "verify_diff", _capture)

    res = await _propose(py_repo, _passing_diff())
    assert res.get("is_error") is not True, res
    assert seen["commands"] == {"/srv/other": ["make", "check"]}


async def test_discovery_is_unchanged_when_nothing_matches(py_repo):
    """REGRESSION PIN. A repo with no configured command keeps today's discovery
    path exactly — same check name, same green. A non-matching key must not
    shadow it, and neither must an empty map."""
    for commands in (None, {}, {"/srv/somewhere-else": ["make", "check"]}):
        result = await belt_verify.verify_diff(
            repo=str(py_repo),
            base_branch="main",
            diff=_passing_diff(),
            timeout_s=120,
            commands=commands,
        )
        assert result.status == "passed", (commands, result.summary)
        assert [c.name for c in result.checks] == ["pytest(ambient)"], commands


# --- the built-in pocketpaw default ---------------------------------------


def test_pocketpaw_is_identified_by_project_name(pocketpaw_repo, py_repo):
    """Shape detection, not a path: [project].name is what marks the repo, so
    every checkout and worktree of pocketpaw gets the default and 'widget' does
    not."""
    assert belt_verify._is_pocketpaw(pocketpaw_repo) is True
    assert belt_verify._is_pocketpaw(py_repo) is False
    assert belt_verify._is_pocketpaw(py_repo / "nope") is False


def test_pocketpaw_targets_follow_the_repo_naming_convention(pocketpaw_repo):
    """Targeting is derived from the diff two ways: test files it carries, and
    the test that COVERS each module it touches.

    `test_<parent>_<stem>` is in there because that IS this repo's convention —
    cloud/belt/verify.py is covered by tests/cloud/test_belt_verify.py."""
    (pocketpaw_repo / "ee/pocketpaw_ee/cloud/belt").mkdir(parents=True)
    (pocketpaw_repo / "ee/pocketpaw_ee/cloud/belt/verify.py").write_text("X = 1\n")
    (pocketpaw_repo / "tests/cloud").mkdir(parents=True)
    (pocketpaw_repo / "tests/cloud/test_belt_verify.py").write_text("def test_x():\n    pass\n")

    diff = _diff("ee/pocketpaw_ee/cloud/belt/verify.py", "X = 1\n", "X = 2\n")
    assert belt_verify._pocketpaw_targets(pocketpaw_repo, diff) == [
        "tests/cloud/test_belt_verify.py"
    ]

    # A module with no test covering it finds nothing — the honest floor, rather
    # than widening to a directory that would blow the budget.
    orphan = _diff("ee/pocketpaw_ee/cloud/belt/orphan.py", "X = 1\n", "X = 2\n")
    assert belt_verify._pocketpaw_targets(pocketpaw_repo, orphan) == []


def test_a_bare_test_name_only_counts_in_the_mirrored_directory(pocketpaw_repo):
    """The bare `test_<stem>.py` form needs the module's own parent directory —
    otherwise a same-named test somewhere unrelated becomes 'proof'.

    Measured on origin/dev, matching the bare name anywhere hits 758 of 1591
    modules, but 472 of those share no directory with the module: instinct/
    store.py would pull in tests/atlas/test_store.py. Running that and reporting
    its passing count is not a slow gate, it is a lying one — the same false
    green _require_evidence refuses. Directory affinity cuts it to 286, nearly
    all resolving to exactly one file."""
    (pocketpaw_repo / "src/pocketpaw/browser").mkdir(parents=True)
    (pocketpaw_repo / "src/pocketpaw/browser/driver.py").write_text("X = 1\n")
    (pocketpaw_repo / "src/pocketpaw/instinct").mkdir(parents=True)
    (pocketpaw_repo / "src/pocketpaw/instinct/store.py").write_text("X = 1\n")
    # Mirrors the module's layout — trustworthy.
    (pocketpaw_repo / "tests/browser").mkdir(parents=True)
    (pocketpaw_repo / "tests/browser/test_driver.py").write_text("def test_x():\n    pass\n")
    # Same file name, unrelated subsystem — must NOT be treated as coverage.
    (pocketpaw_repo / "tests/atlas").mkdir(parents=True)
    (pocketpaw_repo / "tests/atlas/test_store.py").write_text("def test_x():\n    pass\n")

    mirrored = _diff("src/pocketpaw/browser/driver.py", "X = 1\n", "X = 2\n")
    assert belt_verify._pocketpaw_targets(pocketpaw_repo, mirrored) == [
        "tests/browser/test_driver.py"
    ]

    cross_tree = _diff("src/pocketpaw/instinct/store.py", "X = 1\n", "X = 2\n")
    assert belt_verify._pocketpaw_targets(pocketpaw_repo, cross_tree) == []


@_needs_uv
async def test_pocketpaw_default_runs_the_real_argv_and_passes(pocketpaw_repo):
    """The built-in default is selected for a pocketpaw-shaped repo and produces
    a REAL pass on a good diff — through the real argv, a real uv subprocess and
    a real pytest, not a stand-in.

    The diff touches thing.py ONLY, so tests/test_thing.py is reached by
    derivation, not because the diff carried it. `--group ee --group dev` is the
    load-bearing part of the argv: without it uv syncs the default groups, and
    on the real repo that is exactly the all-skipped nothing this default
    exists to fix."""
    result = await belt_verify.verify_diff(
        repo=str(pocketpaw_repo),
        base_branch="main",
        diff=_diff("widgets/thing.py", _PP_MODULE, _PP_MODULE_NEW),
        timeout_s=300,
    )

    assert result.status == "passed", result.checks[0].output
    check = result.checks[0]
    assert (check.name, check.ok, check.skipped) == ("pytest(pocketpaw)", True, False)
    # The real command, and the derived target — not a discovered `uv run pytest`.
    assert "--group ee --group dev" in check.output
    assert "tests/widgets/test_thing.py" in check.output
    assert _worktrees(pocketpaw_repo) == []


async def test_pocketpaw_default_selects_the_documented_argv(pocketpaw_repo, monkeypatch):
    """Pins the argv itself, so the shelling test above cannot pass on a
    quietly-changed default (and so the uv-less skip still leaves the command
    covered)."""
    seen: list[list[str]] = []

    async def _capture(name, argv, **_k):
        seen.append(argv)
        return belt_verify.CheckResult(
            name=name, ok=True, skipped=False, output="1 passed", duration_s=0.1
        )

    monkeypatch.setattr(belt_verify, "_timed", _capture)

    await belt_verify.verify_diff(
        repo=str(pocketpaw_repo),
        base_branch="main",
        diff=_diff("widgets/thing.py", _PP_MODULE, _PP_MODULE_NEW),
        timeout_s=120,
    )

    assert seen == [
        [
            "uv",
            "run",
            "--group",
            "ee",
            "--group",
            "dev",
            "pytest",
            "-q",
            "tests/widgets/test_thing.py",
        ]
    ]


async def test_pocketpaw_default_with_no_derivable_target_is_a_named_skip(pocketpaw_repo):
    """No test in the diff and no test matching the touched module → a named
    SKIP that says so, landing on 'no_checks'.

    Not a full-suite run (that outruns any propose-time budget) and not a pass.
    The message tells the agent the move that makes the gate bite: change the
    test alongside the code."""
    result = await belt_verify.verify_diff(
        repo=str(pocketpaw_repo),
        base_branch="main",
        diff=_diff("orphan.py", "X = 1\n", "X = 2\n"),
        timeout_s=120,
    )

    assert result.status == "no_checks"
    check = result.checks[0]
    assert (check.name, check.ok, check.skipped) == ("pytest(pocketpaw)", True, True)
    assert "nothing targeted to run" in check.output


async def test_configured_command_beats_the_pocketpaw_default(pocketpaw_repo):
    """Precedence runs one way: an operator who has configured this repo
    overrides the built-in, not the other way round."""
    result = await belt_verify.verify_diff(
        repo=str(pocketpaw_repo),
        base_branch="main",
        diff=_diff("widgets/thing.py", _PP_MODULE, _PP_MODULE_NEW),
        timeout_s=120,
        commands={str(pocketpaw_repo): [sys.executable, "-c", "import sys; sys.exit(7)"]},
    )

    assert [c.name for c in result.checks] == ["configured"]
    assert result.status == "failed"


# ---------------------------------------------------------------------------
# BOTH develop paths reach the gate — the hole this file's gate did not cover
# ---------------------------------------------------------------------------
#
# For its first day the gate had exactly ONE call site, ``belt_propose_change``.
# The headless develop runner — the mandate-driven autonomous path, where no
# human is driving and an unverified diff is most dangerous — wrote its produced
# diff straight onto the queued Action. Everything above proves the gate works;
# this proves nothing walks around it.
#
# One recorder, one patch, both paths driven for real. If either stops calling
# the gate, ``seen`` is short by one and this fails — which is the property that
# a per-path test, however thorough, cannot give you.


async def test_the_gate_is_reached_from_both_develop_paths(
    py_repo, store, settings_patch, monkeypatch
):
    from pocketpaw_ee.cloud.belt.headless import (
        DevelopRequest,
        DevelopResult,
        HeadlessDevelopRunner,
    )
    from pocketpaw_ee.cloud.mandates import executor as mandates_ex

    settings_patch(belt_repo_allowlist=[str(py_repo.parent)])

    seen: list[dict] = []

    async def _capture(**kwargs):
        seen.append(kwargs)
        return belt_verify.VerifyResult(
            status="passed",
            checks=(
                belt_verify.CheckResult(
                    name="pytest(fake)", ok=True, skipped=False, output="1 passed", duration_s=0.1
                ),
            ),
            summary="passed: pytest(fake) ok (0.1s)",
        )

    monkeypatch.setattr(belt_verify, "verify_diff", _capture)

    # 1. INTERACTIVE — a human is driving the station and calls the MCP tool.
    interactive_diff = _passing_diff()
    res = await _propose(py_repo, interactive_diff)
    assert res.get("is_error") is not True, res

    # 2. HEADLESS — a mandate filed a queued run and the runner develops it with
    #    nobody watching. Same store, same repo.
    async def _fake_repo(workspace_id: str, mandate_id: str) -> str | None:
        return str(py_repo)

    monkeypatch.setattr(mandates_ex, "_repo_for_mandate", _fake_repo)
    action_id = await mandates_ex.StationTaskDispatcher().dispatch(
        workspace_id="w1",
        mandate_id="m1",
        shift_no=1,
        plan_action_id="plan-act-1",
        index=1,
        task={"title": "Change the greeting", "why": "demo", "requested_by": "u1"},
    )

    headless_diff = _diff(
        "app.py", "def hello():\n    return 'hi'\n", "def hello():\n    return 'yo'\n"
    )

    async def _develop(request: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=headless_diff, base_branch="main", summary="say yo")

    await HeadlessDevelopRunner(develop_fn=_develop).run(action_id, workspace_id="w1")

    # BOTH reached the gate, each with its OWN diff — not one path called twice.
    assert len(seen) == 2, f"the gate was reached {len(seen)} time(s), expected both paths"
    assert [c["repo"] for c in seen] == [str(py_repo), str(py_repo)]
    assert seen[0]["diff"] == interactive_diff
    assert seen[1]["diff"] == headless_diff

    # And the headless run really is a verified, pending proposal now.
    produced = await store.get_action(action_id)
    assert produced is not None
    blob = produced.parameters["_code_change"]
    assert blob["diff"] == headless_diff
    assert blob["station_pending"] is False
    assert blob["verification"]["status"] == "passed"
