# tests/cloud/test_belt_headless.py — the HEADLESS develop runner that closes
# the mandate→belt autonomy gap (feat/belt-headless-exec).
#
# Created: 2026-06-13.
#
# Updated: 2026-09-12 (headless gate) — the runner now puts its produced diff
#   through the MECHANICAL gate before attaching it, so this file grew a "THE
#   MECHANICAL GATE" section at the bottom covering the four verdicts (passed /
#   failed / no_checks / disabled), a verifier that explodes, and a swallowed
#   write failure. Two things to know before adding a test here:
#     * the ``gate`` fixture is AUTOUSE and stubs the verifier GREEN. The gate
#       is on by default and these blobs name ``demo-repo``, which does not
#       exist — without the stub every attach assertion in the file would
#       silently be measuring the refusal path instead.
#     * the both-call-sites pin (that the interactive station AND this runner
#       reach the same ``verify_diff``) lives in ``test_belt_verify.py``, which
#       already has the harness to drive the MCP propose handler.
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

import pocketpaw_ee.cloud.belt.verify as belt_verify  # noqa: E402
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


class _FakeVerifier:
    """Stands in for ``verify_diff`` — the one genuinely external thing in the
    gate (it shells out to git and a test runner in a throwaway worktree).

    Records the kwargs it was handed, so a test can assert the REAL repo / base
    branch / diff reached the gate rather than trusting that something was
    called, and returns whatever verdict the test sets. ``raises`` makes it
    explode, which is the one thing the real one promises never to do — the
    runner must survive it anyway."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.raises: Exception | None = None
        self.result = _verdict("passed")

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result


def _verdict(status: str, *, name: str = "pytest(fake)") -> belt_verify.VerifyResult:
    """A VerifyResult of the given status, shaped the way the real one is."""
    if status == "no_checks":
        return belt_verify.VerifyResult(
            status="no_checks",
            checks=(),
            summary="no mechanical checks discovered for this repo",
        )
    ok = status == "passed"
    return belt_verify.VerifyResult(
        status=status,  # type: ignore[arg-type]
        checks=(
            belt_verify.CheckResult(
                name=name,
                ok=ok,
                skipped=False,
                output="3 passed" if ok else "1 failed: test_greet",
                duration_s=0.5,
            ),
        ),
        summary=f"{status}: {name} {'ok' if ok else 'FAILED'} (0.5s)",
    )


@pytest.fixture(autouse=True)
def gate(monkeypatch) -> _FakeVerifier:
    """The mechanical gate, stubbed GREEN for every test in this module.

    Autouse because the gate is ON by default (``belt_verify_enabled``) and the
    runner now goes through it on the way to attaching a diff: without this, the
    repo on these queued blobs (``demo-repo``, which does not exist) would fail
    verification and every attach assertion in the file would be measuring the
    refusal path by accident. Tests that care about the verdict set
    ``gate.result`` / ``gate.raises``."""
    fake = _FakeVerifier()
    monkeypatch.setattr(belt_verify, "verify_diff", fake)
    return fake


def _stages(recording_bus) -> list[str]:
    """Every stage value that hit the bus, in order."""
    return [e.data["stage"] for e in recording_bus.events if e.type == "belt_run_updated"]


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


# ---------------------------------------------------------------------------
# THE MECHANICAL GATE — the hole this closes
# ---------------------------------------------------------------------------
#
# ``verify_diff`` had exactly ONE call site: ``belt_propose_change``, the
# INTERACTIVE station. This runner — the mandate-driven autonomous path, the one
# with no human anywhere in it — wrote its produced diff straight onto the queued
# Action. "A human at the Instinct gate only ever approves VERIFIED work" was
# therefore true of the station and false here, on the path where an unverified
# diff is most dangerous.
#
# The runner's standing contract shapes what fail-closed can MEAN here. It never
# raises, and a failure leaves the run SAFE — still queued, no diff, a note on
# the blob. That is a different move from the station's: the station refuses the
# propose and hands the failure text back to an agent that can fix it, and files
# no Action at all. There is no agent here, and the Action already exists, so a
# red verdict lands the run back on the state it was already in.


async def test_gate_passes_attaches_a_verified_diff(store, gate, recording_bus):
    """GREEN — the diff attaches as before, carrying the SAME ``verification``
    blob key the interactive station writes, and the two stages fire in order.

    Mutation: drop ``blob["verification"] = verification`` in ``_attach_diff``
    and the human loses the evidence; drop the ``gate`` emit and the console
    never learns the run reached review."""
    action_id = await _queue_station_run(store)
    recording_bus.events.clear()  # ignore the dispatch's own station event

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="adds hello.txt")

    await HeadlessDevelopRunner(develop_fn=fake_develop).run(action_id, workspace_id=WS)

    # The REAL produced diff reached the gate — not a placeholder, and not a
    # different repo or base than the one about to be written onto the blob.
    assert len(gate.calls) == 1
    assert gate.calls[0]["repo"] == "demo-repo"
    assert gate.calls[0]["base_branch"] == "main"
    assert gate.calls[0]["diff"] == CANNED_DIFF

    after = await store.get_action(action_id)
    assert after is not None
    cc = after.parameters["_code_change"]
    assert cc["diff"] == CANNED_DIFF
    assert cc["station_pending"] is False
    assert cc["verification"]["status"] == "passed"
    assert cc["verification"]["checks"][0]["name"] == "pytest(fake)"
    assert after.status == ActionStatus.PENDING  # still the human's call

    # verify BEFORE the check, gate once the diff is really on the row.
    assert _stages(recording_bus) == ["orient", "develop", "verify", "gate"]
    gate_event = [e.data for e in recording_bus.events if e.data.get("stage") == "gate"]
    assert gate_event[0]["action_id"] == action_id
    assert gate_event[0]["status"] == "proposed"

    # The operator trail says what was proven, not just that something was.
    audit = await store.query_audit(event="headless_diff_attached")
    assert audit[0].context.get("verification") == "passed"


async def test_gate_failure_leaves_the_run_queued_and_unproposed(store, gate, recording_bus):
    """RED — fail closed. No diff is attached, the run stays exactly as queued
    as it was, the failing check name is on the blob, and NO ``gate`` stage is
    emitted: a human must see no proposal rather than an unverified one.

    Mutation: turn the ``if refusal is not None`` branch off and an unverified
    diff lands on the row."""
    gate.result = _verdict("failed", name="pytest(pocketpaw)")
    action_id = await _queue_station_run(store)
    recording_bus.events.clear()

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="adds hello.txt")

    ref = await HeadlessDevelopRunner(develop_fn=fake_develop).run(action_id, workspace_id=WS)
    assert ref == action_id  # handled, not raised

    after = await store.get_action(action_id)
    assert after is not None
    cc = after.parameters["_code_change"]
    assert not cc["diff"]
    assert cc["station_pending"] is True  # unchanged — the safe state it began in
    assert "verification" not in cc  # nothing was proven, so nothing is claimed
    assert "pytest(pocketpaw)" in cc["headless_error"]
    assert after.status == ActionStatus.PENDING

    # ``verify`` is honest — the check DID run. ``gate`` must not appear.
    assert _stages(recording_bus) == ["orient", "develop", "verify"]

    # Nothing was written to the operator trail either: no diff was attached.
    assert await store.query_audit(event="headless_diff_attached") == []


async def test_gate_disabled_behaves_exactly_as_before(store, gate, recording_bus, monkeypatch):
    """OFF — a workspace that turns the gate off gets the pre-gate behaviour
    byte for byte: the verifier is never called, no ``verify`` stage is emitted
    (a stage we cannot prove is one we do not emit), and the blob records
    ``disabled`` so nobody mistakes it for a pass."""
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_verify_enabled = False

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())

    gate.raises = AssertionError("verify_diff must not run when the gate is disabled")
    action_id = await _queue_station_run(store)
    recording_bus.events.clear()

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="adds hello.txt")

    await HeadlessDevelopRunner(develop_fn=fake_develop).run(action_id, workspace_id=WS)

    assert gate.calls == []
    after = await store.get_action(action_id)
    assert after is not None
    cc = after.parameters["_code_change"]
    assert cc["diff"] == CANNED_DIFF
    assert cc["station_pending"] is False
    assert cc["verification"] == {"status": "disabled"}
    assert _stages(recording_bus) == ["orient", "develop", "gate"]


async def test_no_checks_is_not_a_refusal(store, gate, recording_bus):
    """A repo with nothing runnable is not a failure. The diff attaches and the
    human sees that nothing was proven — the same three-state rule the station
    gets."""
    gate.result = _verdict("no_checks")
    action_id = await _queue_station_run(store)

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="adds hello.txt")

    await HeadlessDevelopRunner(develop_fn=fake_develop).run(action_id, workspace_id=WS)

    after = await store.get_action(action_id)
    assert after is not None
    cc = after.parameters["_code_change"]
    assert cc["diff"] == CANNED_DIFF
    assert cc["station_pending"] is False
    assert cc["verification"]["status"] == "no_checks"


async def test_a_verifier_that_explodes_does_not_escape_the_runner(store, gate, recording_bus):
    """``verify_diff`` promises never to raise. If it ever breaks that promise,
    the runner's own promise — NEVER raises, always lands safe — has to hold
    anyway, because it runs on a background dispatch path where an escaping
    exception takes the whole mandate shift with it."""
    gate.raises = RuntimeError("the verifier fell over")
    action_id = await _queue_station_run(store)
    recording_bus.events.clear()

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="adds hello.txt")

    ref = await HeadlessDevelopRunner(develop_fn=fake_develop).run(action_id, workspace_id=WS)
    assert ref == action_id

    after = await store.get_action(action_id)
    assert after is not None
    cc = after.parameters["_code_change"]
    assert not cc["diff"]
    assert cc["station_pending"] is True
    assert "errored" in cc["headless_error"]
    assert "gate" not in _stages(recording_bus)


async def test_a_write_failure_does_not_report_a_proposal(store, gate, recording_bus, monkeypatch):
    """``_attach_diff`` swallows a write failure (dispatch must not crash). The
    ``gate`` stage hangs off whether the row actually TOOK the diff, so a
    swallowed failure cannot light a review card for a proposal that is not
    there."""
    import aiosqlite

    action_id = await _queue_station_run(store)
    recording_bus.events.clear()

    # Every sqlite call from the develop loop onwards fails — the store is real
    # up to that point (so the run gets as far as attaching) and dead after it.
    real_connect = aiosqlite.connect
    boom = {"on": False}

    def _maybe_boom(*a, **k):
        if boom["on"]:
            raise RuntimeError("disk full")
        return real_connect(*a, **k)

    monkeypatch.setattr("aiosqlite.connect", _maybe_boom)

    async def fake_develop(req: DevelopRequest) -> DevelopResult:
        boom["on"] = True
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="adds hello.txt")

    await HeadlessDevelopRunner(develop_fn=fake_develop).run(action_id, workspace_id=WS)
    boom["on"] = False  # let the assertions read the row back

    assert "gate" not in _stages(recording_bus)
    after = await store.get_action(action_id)
    assert after is not None
    assert not after.parameters["_code_change"]["diff"]
