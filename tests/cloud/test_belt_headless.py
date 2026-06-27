# tests/cloud/test_belt_headless.py — the HEADLESS develop runner that closes
# the mandate→belt autonomy gap (feat/belt-headless-exec).
#
# Created: 2026-06-13.
#
# THE GAP UNDER TEST — before this, an approved mandate plan task became a
# QUEUED ``code_change`` Instinct Action (``station_pending=True``, NO diff) and
# a HUMAN had to open the ``/belt`` chat surface to produce the diff. The
# headless runner removes the human from PRODUCING the diff (not from approving
# it): given a queued ``code_change`` action and an injectable ``DevelopFn`` that
# returns a unified diff, it back-writes the diff onto the action's blob, clears
# ``station_pending``, and leaves the action PENDING — a real diff awaiting the
# per-diff Instinct gate, exactly as a human-driven ``belt_propose_change`` would.
#
# What is asserted:
#   * SUCCESS — a canned-diff ``DevelopFn`` turns a queued run into a real,
#     pending ``code_change`` carrying the diff + base_branch, station_pending
#     cleared, NOT approved / executed.
#   * GATE PRESERVED — the produced action is PENDING; the belt executor will
#     ONLY apply it after a human approves it. We prove the per-diff gate still
#     stands (the runner never approves or executes).
#   * FAILURE — a ``DevelopFn`` that raises leaves the action SAFE (still queued,
#     no diff, station_pending intact) and never crashes.
#   * The ``DevelopFn`` is injectable: the test passes a deterministic fake — the
#     runner NEVER calls a real LLM or spawns a real agent.

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt.headless import (  # noqa: E402
    DevelopRequest,
    DevelopResult,
    HeadlessDevelopRunner,
    HeadlessTaskDispatcher,
)
from pocketpaw_ee.cloud.mandates.executor import StationTaskDispatcher  # noqa: E402

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "w1"

CANNED_DIFF = """\
diff --git a/hello.txt b/hello.txt
index e69de29..3b18e51 100644
--- a/hello.txt
+++ b/hello.txt
@@ -0,0 +1 @@
+hello
"""


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore wired into the global resolver the runner reads."""
    st = InstinctStore(tmp_path / "instinct_headless.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


async def _queue_station_run(store: InstinctStore, *, repo: str = "demo-repo") -> str:
    """File a QUEUED code_change run the way the StationTaskDispatcher does, then
    return its action id. We patch the repo lookup so the dispatcher doesn't need
    a live Mongo-backed mandate."""
    import pocketpaw_ee.cloud.mandates.executor as ex

    async def _fake_repo(workspace_id: str, mandate_id: str) -> str | None:
        return repo

    orig = ex._repo_for_mandate
    ex._repo_for_mandate = _fake_repo  # type: ignore[assignment]
    try:
        dispatcher = StationTaskDispatcher()
        run_ref = await dispatcher.dispatch(
            workspace_id=WS,
            mandate_id="m1",
            shift_no=1,
            plan_action_id="plan-act-1",
            index=1,
            task={
                "title": "Add a hello file",
                "why": "demonstrate the headless runner",
                "expected_outcome": "hello.txt exists",
                "requested_by": "u1",
            },
        )
    finally:
        ex._repo_for_mandate = orig  # type: ignore[assignment]
    return run_ref


# ---------------------------------------------------------------------------
# SUCCESS — fake DevelopFn → real pending diff, station_pending cleared.
# ---------------------------------------------------------------------------


async def test_headless_runner_produces_pending_diff(store: InstinctStore):
    action_id = await _queue_station_run(store)

    # Pre-condition: it is a QUEUED run (station_pending, no diff).
    queued = await store.get_action(action_id)
    assert queued is not None
    assert queued.status == ActionStatus.PENDING
    assert queued.parameters["_code_change"]["station_pending"] is True
    assert not queued.parameters["_code_change"]["diff"]

    calls: list[DevelopRequest] = []

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        calls.append(req)
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="adds hello.txt")

    runner = HeadlessDevelopRunner(develop_fn=fake_develop)
    result_ref = await runner.run(action_id)

    # The fake was invoked with the task text from the queued blob.
    assert len(calls) == 1
    assert "Add a hello file" in calls[0].task
    assert calls[0].repo == "demo-repo"
    assert result_ref == action_id

    after = await store.get_action(action_id)
    assert after is not None
    cc = after.parameters["_code_change"]
    # The diff is now real, base_branch populated, station_pending CLEARED.
    assert cc["diff"] == CANNED_DIFF
    assert cc["base_branch"] == "main"
    assert cc["station_pending"] is False
    # It is APPLYABLE-SHAPED: a chain correlation id was minted so the gate
    # closes the Decision-Graph chain on approve.
    assert cc.get("correlation_id")
    # CRITICAL — still PENDING. Not auto-approved, not executed.
    assert after.status == ActionStatus.PENDING

    # An operator trail entry was written — the first place LLM-produced content
    # enters the store without a human typing it.
    audit = await store.query_audit(event="headless_diff_attached")
    assert len(audit) == 1
    assert audit[0].action_id == action_id
    assert audit[0].context.get("base_branch") == "main"


# ---------------------------------------------------------------------------
# GATE PRESERVED — the produced diff still requires human approval; the belt
# executor only applies it AFTER approve. The runner never approves/executes.
# ---------------------------------------------------------------------------


async def test_headless_diff_still_requires_human_approval(store: InstinctStore):
    action_id = await _queue_station_run(store)

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="adds hello.txt")

    runner = HeadlessDevelopRunner(develop_fn=fake_develop)
    await runner.run(action_id)

    produced = await store.get_action(action_id)
    assert produced is not None
    # The runner left it PENDING — the per-diff Instinct gate is intact. The
    # belt executor refuses to apply anything that isn't an APPROVED action; the
    # only path that applies a diff is execute_approved_change, called by the
    # router AFTER store.approve(). The runner touches neither.
    assert produced.status == ActionStatus.PENDING
    assert produced.approved_by is None


# ---------------------------------------------------------------------------
# FAILURE — a DevelopFn that raises leaves the run SAFE, never crashes.
# ---------------------------------------------------------------------------


async def test_headless_runner_handles_develop_failure(store: InstinctStore):
    action_id = await _queue_station_run(store)

    async def boom(req: DevelopRequest) -> DevelopResult:
        raise RuntimeError("model unavailable")

    runner = HeadlessDevelopRunner(develop_fn=boom)
    # Must NOT raise.
    ref = await runner.run(action_id)
    assert ref == action_id

    after = await store.get_action(action_id)
    assert after is not None
    cc = after.parameters["_code_change"]
    # The run is left SAFE: still queued, no diff written, NOT applyable. A
    # human can still drive the station, or the dispatcher can retry.
    assert cc["station_pending"] is True
    assert not cc["diff"]
    # The action is NOT auto-approved / executed; it carries a failure note.
    assert after.status in (ActionStatus.PENDING, ActionStatus.FAILED)
    assert "headless" in (cc.get("headless_error") or "").lower() or after.error


async def test_headless_runner_rejects_empty_diff(store: InstinctStore):
    """A DevelopFn that returns an empty diff is a no-op failure, not an
    applyable run — leave the queued run untouched."""
    action_id = await _queue_station_run(store)

    async def empty(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff="   \n", base_branch="main", summary="nothing")

    runner = HeadlessDevelopRunner(develop_fn=empty)
    await runner.run(action_id)

    after = await store.get_action(action_id)
    assert after is not None
    cc = after.parameters["_code_change"]
    assert cc["station_pending"] is True
    assert not cc["diff"]


async def test_headless_runner_rejects_missing_base_branch(store: InstinctStore):
    """A DevelopFn that returns a real diff but NO base_branch is not applyable
    (the belt executor needs a base to worktree off) — leave the run queued via
    the no_base_branch safety path, not a half-attached applyable run."""
    action_id = await _queue_station_run(store)

    async def no_base(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="", summary="")

    runner = HeadlessDevelopRunner(develop_fn=no_base)
    ref = await runner.run(action_id)
    assert ref == action_id

    after = await store.get_action(action_id)
    assert after is not None
    cc = after.parameters["_code_change"]
    # The run is left SAFE: still queued, no diff written.
    assert cc["station_pending"] is True
    assert cc["diff"] == ""
    assert after.status == ActionStatus.PENDING
    assert "base_branch" in (cc.get("headless_error") or "")


# ---------------------------------------------------------------------------
# DISPATCHER WIRING — the HeadlessTaskDispatcher files the queued run AND runs
# the headless runner, so an approved plan task becomes a real pending diff in
# one dispatch (no human in the diff-producing loop).
# ---------------------------------------------------------------------------


async def test_headless_dispatcher_produces_diff_on_dispatch(store: InstinctStore, monkeypatch):
    import pocketpaw_ee.cloud.mandates.executor as ex

    async def _fake_repo(workspace_id: str, mandate_id: str) -> str | None:
        return "demo-repo"

    monkeypatch.setattr(ex, "_repo_for_mandate", _fake_repo)

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="dev", summary="adds hello.txt")

    dispatcher = HeadlessTaskDispatcher(runner=HeadlessDevelopRunner(develop_fn=fake_develop))
    run_ref = await dispatcher.dispatch(
        workspace_id=WS,
        mandate_id="m1",
        shift_no=1,
        plan_action_id="plan-act-1",
        index=1,
        task={"title": "Add hello", "why": "demo", "expected_outcome": "ok", "requested_by": "u1"},
    )

    action = await store.get_action(run_ref)
    assert action is not None
    cc = action.parameters["_code_change"]
    # The dispatch produced a real pending diff — NOT a queued placeholder.
    assert cc["diff"] == CANNED_DIFF
    assert cc["base_branch"] == "dev"
    assert cc["station_pending"] is False
    assert action.status == ActionStatus.PENDING


# ---------------------------------------------------------------------------
# SELECTION — POCKETPAW_MANDATE_DISPATCHER=headless selects the headless
# dispatcher when a production develop loop is wired, else degrades to the
# queued-run station dispatcher (the autonomous path is strictly opt-in).
# ---------------------------------------------------------------------------


def test_headless_selection_requires_wired_develop_loop(monkeypatch):
    import pocketpaw_ee.cloud.belt.headless as headless_mod
    import pocketpaw_ee.cloud.mandates.executor as ex

    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "headless")

    # No develop loop wired → falls back to the queued-run station dispatcher.
    # monkeypatch.setattr restores the global even if an assertion below raises,
    # so the wired/unwired state never leaks into another test under
    # asyncio_mode=auto.
    monkeypatch.setattr(headless_mod, "_PRODUCTION_DEVELOP_FN", None)
    assert isinstance(ex.resolve_dispatcher(), ex.StationTaskDispatcher)

    # Wire a (fake) develop loop → the headless dispatcher is selected.
    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="main")

    monkeypatch.setattr(headless_mod, "_PRODUCTION_DEVELOP_FN", fake_develop)
    assert isinstance(ex.resolve_dispatcher(), HeadlessTaskDispatcher)


def test_headless_resolve_returns_none_when_unwired(monkeypatch):
    import pocketpaw_ee.cloud.belt.headless as headless_mod

    monkeypatch.setattr(headless_mod, "_PRODUCTION_DEVELOP_FN", None)
    assert headless_mod.resolve_headless_dispatcher() is None


# ---------------------------------------------------------------------------
# END-TO-END — the headless-produced diff is a GENUINELY applyable run: after a
# human approves it, the REAL belt executor applies it to a real git repo. This
# proves the whole point — the human was removed from PRODUCING the diff, NOT
# from approving it, and the produced diff really lands through the existing gate.
# ---------------------------------------------------------------------------

_PATH = os.environ.get("PATH", "/usr/bin:/bin")


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


# A diff that applies cleanly to the seeded ``app.py`` (matches the gate test's
# local_repo fixture: ``def hello():\n    return 'hi'\n``).
_APP_DIFF = """\
diff --git a/app.py b/app.py
index 0000000..1111111 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def hello():
-    return 'hi'
+    return 'bye'
"""


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A git repo with NO origin — mirrors the gate test's local-only fixture so
    the executor lands the change locally (no push/PR) without network."""
    if shutil.which("git") is None:  # pragma: no cover - CI always has git
        pytest.skip("git not available")
    work = tmp_path / "local-work"
    work.mkdir()
    _git(work, "init")
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")
    (work / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    return work


@pytest.fixture
def allowlist(local_repo: Path, monkeypatch):
    """Point belt_repo_allowlist at the repo's parent so the executor's
    re-resolve passes."""
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(local_repo.parent)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())


async def test_headless_diff_applies_after_human_approval(
    store: InstinctStore, local_repo: Path, allowlist
):
    from pocketpaw_ee.cloud.belt.executor import execute_approved_change

    action_id = await _queue_station_run(store, repo=str(local_repo))

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=_APP_DIFF, base_branch="main", summary="flip the greeting")

    await HeadlessDevelopRunner(develop_fn=fake_develop).run(action_id)

    produced = await store.get_action(action_id)
    assert produced is not None
    assert produced.status == ActionStatus.PENDING  # still gated

    # The human approves (the per-diff gate). ONLY THEN does the executor apply.
    await store.approve(produced.id)
    approved = await store.get_action(produced.id)
    await execute_approved_change(approved)

    landed = await store.get_action(produced.id)
    assert landed is not None
    assert landed.status == ActionStatus.EXECUTED, landed.error
    # A real belt branch was created carrying the headless-produced change.
    branches = _git(local_repo, "branch", "--list", "feat/belt-*")
    assert branches.strip(), "expected a feat/belt-* branch from the applied diff"
