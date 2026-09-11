# tests/cloud/test_belt_factory_e2e.py — the Belt factory chain, end to end.
#
# Created: 2026-09-12 (integration/belt-factory) — the integration pin for four
# slices that were built separately and had never been run against each other:
# the per-file ``belt_entity_changed`` feed, the stage vocabulary, the
# propose-time mechanical gate, and the ``current_stream_run_id`` join key.
# Each has its own unit suite; NONE of them proved the chain HOLDS.
#
# What this pins, in ORDER (occurrence alone would pass a chain that fires the
# right events at the wrong time):
#
#   HAPPY — a run writes two files, the gate's real checks pass, a human gets a
#   verified proposal:
#     Write app.py       -> belt_entity_changed(app.py, change=write)
#                        -> stage=develop
#     Write test_app.py  -> belt_entity_changed(test_app.py, change=write)
#                        -> (no second develop — the forward-only guard)
#     propose            -> stage=verify   (BEFORE the gate runs)
#                        -> real pytest in a real throwaway worktree: PASSES
#                        -> Instinct Action filed, blob carries BOTH
#                           verification.status == "passed" AND the run_id
#                        -> stage=gate (status=proposed)
#                        -> the runs read model returns the row with its run_id
#
#   REFUSAL — the same flow with a diff that breaks its own test:
#     ... -> stage=verify -> the gate's checks FAIL -> the propose returns an
#     error naming the failing check -> NO Action is filed -> NO gate event ->
#     the run does not appear in the runs list. This is the fail-closed
#     property the whole design rests on: a human is never shown work that
#     nothing ran. It is asserted from all four directions, not just the
#     return value.
#
#   THE JOIN KEY — every event that carries a run_id carries the SAME one, and
#   it is the one the Action blob and the read-model row report. This is the
#   assertion the sprint exists for: the early events have no ``action_id``
#   (the Action is minted after the last write) and the ``gate`` event has no
#   ``run_id``, so the two halves only meet on the row the read model returns.
#
# FAKED: nothing but the LLM. There is no LLM here at all — the tool stream a
# real run would produce is replayed through the same two bridges
# ``chat/runs/run_core.py`` calls, in the same order, with the run id
# ``bind_stream_run_id`` would have bound. Everything else is real: a real git
# repo, a real ``git worktree`` + ``git apply``, a real pytest subprocess for
# the gate, the real realtime bus (conftest's ``recording_bus``), the real
# ``InstinctStore`` on a tmp file, the real MCP handler, the real read model.
#
# NOT covered here (and deliberately): that ``run_core`` threads its
# ``stream_run_id`` into both bridges and into ``bind_stream_run_id``. That is
# source-inspected by ``test_belt_entity_events.py::test_run_core_calls_the_bridge``
# and ``test_belt_stage_events.py::test_run_core_calls_the_stage_bridge`` —
# driving the agent loop itself would need the LLM this file refuses to fake.

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_PATH = os.environ.get("PATH", "/usr/bin:/bin")

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.mcp_servers.belt as belt  # noqa: E402
from pocketpaw_ee.cloud.belt.service import (  # noqa: E402
    list_runs,
    maybe_emit_belt_entity_changed,
    maybe_emit_belt_stage,
)
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    bind_stream_run_id,
    detach_agent_identity,
    unbind_stream_run_id,
)

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# The surface string the bridges gate on — the value of ``SurfaceKind.BELT``.
_BELT = "belt"

# The run id a real chat stream would have minted and bound. One constant, used
# for the bind AND both bridges, exactly as run_core threads one ``stream_run_id``
# through all three.
_RUN = "run-factory-e2e"

_WS = "w1"


# ---------------------------------------------------------------------------
# A real git repo whose tests really run
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


_PYPROJECT = '[project]\nname = "widget"\nversion = "0.1.0"\n'
_APP = "def greet():\n    return 'hi'\n"
_APP_NEW = "def greet():\n    return 'hi there'\n"
_APP_BROKEN = "def greet():\n    return 'BROKEN'\n"
_TEST = "from app import greet\n\n\ndef test_greet():\n    assert greet() == 'hi'\n"
_TEST_NEW = "from app import greet\n\n\ndef test_greet():\n    assert greet() == 'hi there'\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A Python repo with a module and a test that passes on the CURRENT source.

    No ``origin`` (verify resolves a local base ref) and no ``uv.lock``, so the
    gate discovers the AMBIENT pytest and runs it as a real subprocess in the
    throwaway worktree. A diff that keeps the test true verifies green; one that
    breaks the module verifies red — the two halves of this file.
    """
    work = tmp_path / "py-repo"
    work.mkdir(parents=True)
    _git(work, "init")
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")
    for rel, body in {
        "pyproject.toml": _PYPROJECT,
        "app.py": _APP,
        "test_app.py": _TEST,
    }.items():
        (work / rel).write_text(body, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    return work


def _diff(path: str, old: str, new: str) -> str:
    """A one-hunk unified diff replacing whole-file content."""
    body = "".join(f"-{line}\n" for line in old.splitlines())
    body += "".join(f"+{line}\n" for line in new.splitlines())
    head = f"@@ -1,{len(old.splitlines())} +1,{len(new.splitlines())} @@"
    return f"--- a/{path}\n+++ b/{path}\n{head}\n{body}"


def _passing_diff() -> str:
    """Both files, changed together — the gate's checks pass."""
    return _diff("app.py", _APP, _APP_NEW) + _diff("test_app.py", _TEST, _TEST_NEW)


def _failing_diff() -> str:
    """Only the module, leaving its test asserting the old value — the gate's
    checks FAIL. Still TWO files written by the run: the station wrote both, and
    the diff it proposed is what is wrong."""
    return _diff("app.py", _APP, _APP_BROKEN)


# ---------------------------------------------------------------------------
# The real stores + settings, isolated
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """A real ``InstinctStore`` on a tmp file, workspace-faithful (the seeded
    store answers only for ``w1``) — mirrors test_belt_gate / test_belt_verify
    so the propose path and the read model resolve the same file the way
    production resolves a tenant's."""
    from pocketpaw.stores import current_workspace

    st = InstinctStore(tmp_path / "instinct_belt_e2e.db")
    others: dict[str, InstinctStore] = {}

    def _factory(*_a, workspace_id: str | None = None, **_k) -> InstinctStore:
        ws = str((workspace_id if workspace_id is not None else current_workspace.get()) or "")
        if ws == _WS:
            return st
        return others.setdefault(ws, InstinctStore(tmp_path / f"instinct_other_{ws or 'none'}.db"))

    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", _factory)
    return st


@pytest.fixture
def allowlist(repo: Path, monkeypatch) -> None:
    """Put the repo inside the belt allowlist boundary."""
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(repo.parent)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())


class _SSECapture:
    """Stand in for the per-stream SSE sink so the secondary delivery path is
    exercised without a live stream. The BUS is the primary path and the one the
    assertions read."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, name: str, data: dict) -> None:
        self.events.append((name, data))


@pytest.fixture
def sse(monkeypatch) -> _SSECapture:
    cap = _SSECapture()
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.agent_service.push_sse_event", cap, raising=True)
    return cap


@pytest.fixture
def stream():
    """Bind the identity + stream run id a real chat turn would have bound.

    ``run_id`` is NOT passed to the propose handler by any test in this file —
    the handler reads it off this ContextVar, which is what makes
    ``blob["run_id"] == _RUN`` a real assertion rather than an echo of an
    argument.
    """
    tokens = attach_agent_identity(workspace_id=_WS, user_id="u1", session_mongo_id="sess-1")
    run_token = bind_stream_run_id(_RUN)
    try:
        yield _RUN
    finally:
        unbind_stream_run_id(run_token)
        detach_agent_identity(tokens)


# ---------------------------------------------------------------------------
# Replaying the tool stream through the bridges run_core calls
# ---------------------------------------------------------------------------

# The belt feed's two event types. Filtering to them keeps the ORDER assertions
# readable and immune to unrelated workspace traffic on the shared bus.
_BELT_EVENTS = ("belt_entity_changed", "belt_run_updated")


def _feed(recording_bus) -> list[tuple[str, dict]]:
    """The belt feed as (event type, payload), in emission order."""
    return [(e.type, e.data) for e in recording_bus.events if e.type in _BELT_EVENTS]


def _shape(recording_bus) -> list[tuple[str, str | None]]:
    """The feed reduced to what ORDER means here: a file change (with its path)
    or a stage transition (with its stage)."""
    out: list[tuple[str, str | None]] = []
    for kind, data in _feed(recording_bus):
        if kind == "belt_entity_changed":
            out.append(("file", data.get("file")))
        else:
            out.append(("stage", data.get("stage")))
    return out


async def _write(repo: Path, relpath: str, prev: str | None) -> str | None:
    """Replay ONE ``Write`` tool call through both bridges, in run_core's order:
    the per-file feed first, then the stage axis, threading ``prev`` back exactly
    as the agent loop's local does."""
    await maybe_emit_belt_entity_changed(
        surface=_BELT,
        tool_name="Write",
        tool_input={"file_path": str(repo / relpath)},
        workspace_id=_WS,
        run_id=_RUN,
        repo_root=str(repo),
    )
    return await maybe_emit_belt_stage(
        surface=_BELT,
        tool_name="Write",
        workspace_id=_WS,
        run_id=_RUN,
        prev=prev,
    )


async def _propose(repo: Path, diff: str) -> dict:
    """Call the REAL MCP handler. Note what is NOT passed: no run_id — the
    handler reads the bound stream's."""
    return await belt._propose_change_handler(
        {
            "repo": str(repo),
            "base_branch": "main",
            "diff": diff,
            "summary": "Change the greeting.",
            "task": "Make greet() friendlier.",
        }
    )


async def _run_the_station(repo: Path, diff: str) -> dict:
    """Two file writes then a propose — the whole station run."""
    stage = await _write(repo, "app.py", None)
    stage = await _write(repo, "test_app.py", stage)
    assert stage == "develop", "two writes must leave the run at develop"
    return await _propose(repo, diff)


# ---------------------------------------------------------------------------
# HAPPY PATH — the chain holds and a human is shown verified work
# ---------------------------------------------------------------------------


async def test_the_whole_chain_in_order(repo, store, allowlist, stream, recording_bus, sse):
    """Two writes, a passing gate, an Action, a row — in that order.

    The ORDER is the point. Every one of these events already fires somewhere in
    the unit suites; what had never been shown is that they fire in a sequence a
    console can actually render: work, then a check, then a gate — never a gate
    before the check that justifies it.
    """
    res = await _run_the_station(repo, _passing_diff())
    assert res.get("is_error") is not True, res

    # 1. THE SEQUENCE. Asserted whole, not by membership: a chain that emits
    #    ``gate`` before ``verify`` would pass every occurrence check and still
    #    be wrong. The second Write emits a file event but NOT a second
    #    ``develop`` — the forward-only guard, visible here as an absence.
    assert _shape(recording_bus) == [
        ("file", "app.py"),
        ("stage", "develop"),
        ("file", "test_app.py"),
        ("stage", "verify"),
        ("stage", "gate"),
    ]

    # 2. The file events say WHAT changed, in loom's entity-id form.
    files = [d for k, d in _feed(recording_bus) if k == "belt_entity_changed"]
    assert [(f["entity_id"], f["change"]) for f in files] == [
        ("py-repo:file:app.py", "write"),
        ("py-repo:file:test_app.py", "write"),
    ]

    # 3. THE JOIN KEY. Every event that carries one carries the SAME run id, and
    #    it is the one the stream bound — nothing in the chain minted its own.
    carried = {d["run_id"] for _k, d in _feed(recording_bus) if d.get("run_id")}
    assert carried == {_RUN}

    #    The early events have NO action_id: the Action does not exist while the
    #    station is writing. This is why the run_id has to exist at all.
    early = [d for _k, d in _feed(recording_bus) if d.get("stage") != "gate"]
    assert all(d.get("action_id") is None for d in early)


async def test_the_gate_proved_it_and_the_action_carries_both_halves(
    repo, store, allowlist, stream, recording_bus, sse
):
    """The filed Action carries the gate's verdict AND the run id.

    ``verification`` without ``run_id`` is a verdict nobody can trace back to
    the work; ``run_id`` without ``verification`` is a traceable rubber stamp.
    The row is only useful holding both.
    """
    res = await _run_the_station(repo, _passing_diff())
    assert res.get("is_error") is not True, res

    actions = await store.list_actions(pocket_id=_WS)
    assert len(actions) == 1
    blob = actions[0].parameters["_code_change"]

    # A REAL pytest ran in a REAL worktree and really passed — not a stub, not a
    # skip. ``no_checks`` would mean the gate found nothing to run and this test
    # would be proving nothing.
    assert blob["verification"]["status"] == "passed"
    assert [c["name"] for c in blob["verification"]["checks"]] == ["pytest(ambient)"]
    assert blob["verification"]["checks"][0]["ok"] is True
    assert blob["verification"]["checks"][0]["skipped"] is False

    # The join key, read off the ContextVar by the handler — never passed in.
    assert blob["run_id"] == _RUN

    # The gate event names the Action; it does NOT carry the run id. That is the
    # shape of the join, not a defect: the two halves meet on the read-model row.
    gate = [d for _k, d in _feed(recording_bus) if d.get("stage") == "gate"]
    assert len(gate) == 1
    assert gate[0]["action_id"] == actions[0].id
    assert gate[0]["status"] == "proposed"
    assert gate[0].get("run_id") is None


async def test_the_read_model_closes_the_join(repo, store, allowlist, stream, recording_bus, sse):
    """The runs list returns the run id, so the console can match a row to the
    live events that produced it. Without this the whole chain is two feeds that
    never meet."""
    res = await _run_the_station(repo, _passing_diff())
    assert res.get("is_error") is not True, res

    runs = (await list_runs(_WS))["runs"]
    assert len(runs) == 1
    row = runs[0]

    assert row["run_id"] == _RUN
    assert row["status"] == "proposed"
    assert row["stage"] == "gate"

    # The row holds BOTH halves — this is the join, in one object.
    ran = {d["run_id"] for _k, d in _feed(recording_bus) if d.get("run_id")}
    assert ran == {row["run_id"]}
    gate = next(d for _k, d in _feed(recording_bus) if d.get("stage") == "gate")
    assert gate["action_id"] == row["action_id"]


async def test_a_headless_run_reports_a_null_run_id(repo, store, allowlist, recording_bus, sse):
    """No chat stream, no run id — and the read model says so instead of
    inventing one. The key is optional on purpose: the headless runner has no
    stream to bind."""
    tokens = attach_agent_identity(workspace_id=_WS, user_id="u1", session_mongo_id="sess-1")
    try:
        res = await _propose(repo, _passing_diff())
    finally:
        detach_agent_identity(tokens)
    assert res.get("is_error") is not True, res

    actions = await store.list_actions(pocket_id=_WS)
    assert actions[0].parameters["_code_change"]["run_id"] is None
    assert (await list_runs(_WS))["runs"][0]["run_id"] is None


# ---------------------------------------------------------------------------
# REFUSAL PATH — fail-closed, asserted from every direction
# ---------------------------------------------------------------------------


async def test_a_failing_gate_refuses_and_files_nothing(
    repo, store, allowlist, stream, recording_bus, sse
):
    """The property the whole design rests on: when the mechanical checks fail,
    NO human is ever shown the change.

    Asserted four ways, because any one of them can be true while the design is
    broken — an error return with an Action filed anyway is the exact failure
    mode this gate exists to prevent.
    """
    res = await _run_the_station(repo, _failing_diff())

    # 1. The propose is REFUSED, and the refusal names the check that failed —
    #    without the name the agent has nothing to fix and the feedback loop is
    #    a dead end.
    assert res["is_error"] is True
    text = res["content"][0]["text"]
    # The check NAME itself carries parens (``pytest(ambient)``), so match up to
    # the em-dash that follows the list rather than to the first ``)``.
    named = re.search(r"verification FAILED \((.+?)\) —", text)
    assert named is not None, text
    assert named.group(1) == "pytest(ambient)"
    # The failing check's own output rides back too, not just its name.
    assert "test_greet" in text

    # 2. NO Action was filed. The store is the durable truth; an error response
    #    over a stored Action would still put unverified work in the Tray.
    assert await store.list_actions(pocket_id=_WS) == []

    # 3. The run does not appear in the runs list — nothing for a human to open,
    #    approve, or mistake for reviewed work.
    assert (await list_runs(_WS))["runs"] == []

    # 4. NO gate event. The console is never told the run reached the human
    #    gate, because it did not.
    assert _shape(recording_bus) == [
        ("file", "app.py"),
        ("stage", "develop"),
        ("file", "test_app.py"),
        ("stage", "verify"),
    ]


async def test_the_refused_run_still_joins(repo, store, allowlist, stream, recording_bus, sse):
    """A refused run's events still carry the one run id.

    A refusal is not a run that never happened: the work was done and the check
    was run, and a console following the live feed has to be able to show that
    the run stopped at ``verify``. Losing the join here would leave orphan
    events with nothing to attach them to — the failure that is hardest to
    notice, because the happy path looks fine.
    """
    res = await _run_the_station(repo, _failing_diff())
    assert res["is_error"] is True

    events = _feed(recording_bus)
    assert events, "a refused run must still have reported its work"
    assert {d["run_id"] for _k, d in events if d.get("run_id")} == {_RUN}
    # Every one of them — the file feed and the stage feed alike.
    assert all(d.get("run_id") == _RUN for _k, d in events)


async def test_verify_is_emitted_before_the_gate_runs_not_after(
    repo, store, allowlist, stream, recording_bus, sse
):
    """``verify`` announces a check that is ABOUT to run, not one that finished.

    Proven by the refusal path: the run failed the gate, so if the stage were
    emitted after the checks it would never have been emitted at all. Its
    presence on a run that was refused is what dates it to before the gate.
    """
    res = await _run_the_station(repo, _failing_diff())
    assert res["is_error"] is True
    assert ("stage", "verify") in _shape(recording_bus)


async def test_a_disabled_gate_never_claims_the_verify_stage(
    repo, store, allowlist, stream, recording_bus, sse, monkeypatch
):
    """Gate off — no check runs, so no ``verify`` is claimed.

    The stage vocabulary's rule is that a stage we cannot prove is one we do not
    emit. An emit placed at the handler's call site instead of behind the
    enabled guard would light ``verify`` on a run nothing ever checked, which is
    exactly the lie the whole console strip is meant not to tell.
    """
    from pocketpaw.config import get_settings

    current = get_settings()

    class _S:
        belt_verify_enabled = False

        def __getattr__(self, name):
            return getattr(current, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())

    res = await _run_the_station(repo, _passing_diff())
    assert res.get("is_error") is not True, res

    assert ("stage", "verify") not in _shape(recording_bus)
    assert _shape(recording_bus)[-1] == ("stage", "gate")

    actions = await store.list_actions(pocket_id=_WS)
    blob = actions[0].parameters["_code_change"]
    assert blob["verification"] == {"status": "disabled"}
    # The join key is independent of the gate — it still lands.
    assert blob["run_id"] == _RUN
    assert (await list_runs(_WS))["runs"][0]["run_id"] == _RUN


# ---------------------------------------------------------------------------
# The secondary delivery path
# ---------------------------------------------------------------------------


async def test_the_in_turn_sse_mirrors_the_bus(repo, store, allowlist, stream, recording_bus, sse):
    """Both delivery paths carry the same feed. The bus is what a teammate with
    the console open receives; the SSE push is the in-turn nudge for the tab
    that is driving the run. A chain that only reached one of them would look
    fine to whoever tested it and be dead for everyone else."""
    res = await _run_the_station(repo, _passing_diff())
    assert res.get("is_error") is not True, res

    pushed = [(name, data) for name, data in sse.events if name in _BELT_EVENTS]
    assert [(n, d.get("file") or d.get("stage")) for n, d in pushed] == [
        (k, d.get("file") or d.get("stage")) for k, d in _feed(recording_bus)
    ]
