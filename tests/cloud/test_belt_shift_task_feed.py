# tests/cloud/test_belt_shift_task_feed.py
# Created: 2026-08-08 (feat/coupling-t14-belt-to-tasks).
#
# T-14 — belt/mandate work lands in the WORKSPACE TASK FEED.
#
# Before this slice the Belt kept its dispatched work entirely to itself: a
# shift's tasks existed only as ``code_change`` Instinct blobs and queued
# station runs, so /deep-work — the surface a human actually watches — showed
# nothing while an approved mandate was doing real work. The Belt had grown a
# private task list rather than relating to the Task primitive that already
# owns "a unit of work someone is doing".
#
# What is pinned here:
#   1. approving a shift files ONE workspace Task per dispatched task, carrying
#      ``source.type="belt_shift"`` and ``ref_id=<shift id>`` (the SHIFT, so a
#      shift's rows group; the run_ref rides in metadata);
#   2. the shift RECORDS the ids it filed on ``ShiftDoc.task_ids``, so the
#      console joins shift→feed without re-deriving from the blobs;
#   3. a tasks-service failure NEVER fails the shift — the mirror is a
#      courtesy, the dispatch is the job;
#   4. the mirrored rows are tenant-scoped to the mandate's workspace.
#
# Harness mirrors tests/cloud/test_belt_foreman_agent.py — mock LLM transport,
# real Instinct store, real instinct-router approve, real plan executor, with
# the dispatcher swapped for a recorder at the develop-station boundary.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.agents import service as agents_service  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.decisions.service import (  # noqa: E402
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.cloud.mandates import executor as mandate_executor  # noqa: E402
from pocketpaw_ee.cloud.mandates import foreman  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import ShiftDoc  # noqa: E402
from pocketpaw_ee.cloud.mandates.router import router as mandates_router  # noqa: E402
from pocketpaw_ee.cloud.models.task import Task as _TaskDoc  # noqa: E402
from pocketpaw_ee.cloud.tasks import service as tasks_service  # noqa: E402
from pocketpaw_ee.instinct.router import router as instinct_router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "w1"
OTHER_WS = "w2"
USER = "u1"


# ---------------------------------------------------------------------------
# fixtures (same shape as test_belt_foreman_agent.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def soul_home(tmp_path, monkeypatch) -> Path:
    import pocketpaw.config as pp_config

    home = tmp_path / "pocketpaw-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pp_config, "get_config_dir", lambda: home)
    return home


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    journal_dep.reset_journal_cache()
    original = journal_dep._cached_journal
    journal_dep._cached_journal = lambda: j  # type: ignore[assignment]
    yield j
    journal_dep._cached_journal = original  # type: ignore[assignment]
    journal_dep.reset_journal_cache()
    j.close()


@pytest.fixture
def graph(tmp_path: Path) -> DecisionGraph:
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_shift_feed.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: st)
    return st


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setenv("POCKETPAW_MANDATE_LLM", "mock")
    foreman.set_mock_plan(None)
    yield
    foreman.set_mock_plan(None)


class RecorderDispatcher:
    """Records dispatch calls — the develop-station boundary."""

    instances: list[RecorderDispatcher] = []

    def __init__(self) -> None:
        self.calls: list[dict] = []
        RecorderDispatcher.instances.append(self)

    async def dispatch(
        self, *, workspace_id, mandate_id, shift_no, plan_action_id, index, task
    ) -> str:
        self.calls.append({"index": index, "task": task})
        return f"{plan_action_id}:t{index}"


@pytest.fixture
def dispatcher(monkeypatch) -> type[RecorderDispatcher]:
    RecorderDispatcher.instances = []
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "bus")
    monkeypatch.setattr(mandate_executor, "BusTaskDispatcher", RecorderDispatcher)
    return RecorderDispatcher


def _make_client(monkeypatch, *, workspace_id: str = WS, user_id: str = USER):
    import pocketpaw_ee.cloud.workspace.service as ws_svc
    from fastapi.testclient import TestClient

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(mandates_router)
    app.include_router(instinct_router)
    app.dependency_overrides[require_license] = lambda: None

    user = SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role="admin")],
    )

    async def _fake_user_dep():
        return user

    app.dependency_overrides[current_active_user] = _fake_user_dep
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    return TestClient(app)


def _charter(budget: int = 3) -> dict:
    return {
        "goal": "keep dependencies fresh and CVE-free",
        "kpis": [{"name": "open_cves", "target": 0, "direction": "down"}],
        "says_no": ["major version bumps"],
        "boundaries": ["never touch auth code"],
        "budget": {"max_tasks_per_shift": budget, "gate_minutes_per_week": 15},
        "cadence": "manual",
    }


async def _seed_default_agent(workspace: str = WS) -> str:
    doc, _created = await agents_service.seed_default_agent(workspace, USER)
    assert doc is not None
    return str(doc.id)


async def _run_shift_to_dispatch(client, tmp_path: Path) -> tuple[str, str]:
    """Create a mandate, seed a sighting, run a shift, approve the plan.

    Returns ``(mandate_id, shift_id)``."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    res = client.post(
        "/belt/mandates",
        json={"name": "deps freshness", "surface": {"repo_id": str(repo)}, "charter": _charter()},
    )
    assert res.status_code == 200, res.text
    mandate_id = res.json()["mandate"]["id"]

    res = client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "lodash CVE flagged by a customer", "severity": 4, "source": "support"},
    )
    assert res.status_code == 200, res.text

    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text
    shift = res.json()["shift"]
    plan_action_id = shift["plan_action_id"]
    assert plan_action_id, "the foreman stood down — no plan to approve"

    res = client.post(f"/instinct/actions/{plan_action_id}/approve", json={})
    assert res.status_code == 200, res.text
    return mandate_id, shift["shift_id"]


# ---------------------------------------------------------------------------
# 1. The dispatched work appears in the workspace feed
# ---------------------------------------------------------------------------


async def test_approved_shift_files_a_task_per_dispatched_task(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph, dispatcher, recording_bus
):
    """THE SLICE. An approved shift dispatches N tasks; N workspace Tasks must
    exist afterwards, each pointing back at the SHIFT.

    ``ref_id`` is the shift id rather than the run ref so every row from one
    shift groups under one source — the individual run rides in ``metadata``
    where a console can still reach it.

    MUTATION THAT BREAKS THIS: drop the ``_mirror_task_to_feed`` call from the
    dispatch loop in ``mandates/executor.execute_approved_plan`` — the task
    count assert fails at 0."""
    await _seed_default_agent()
    client = _make_client(monkeypatch)

    mandate_id, shift_id = await _run_shift_to_dispatch(client, tmp_path)

    dispatched = RecorderDispatcher.instances[-1].calls
    assert dispatched, "the recorder saw no dispatch — the harness, not the mirror, is broken"

    rows = await _TaskDoc.find(_TaskDoc.workspace_id == WS).to_list()
    mirrored = [t for t in rows if t.source and t.source.type == "belt_shift"]
    assert len(mirrored) == len(dispatched)

    for t in mirrored:
        assert t.source.ref_id == shift_id
        assert t.source.metadata.get("mandate_id") == mandate_id
        assert t.source.metadata.get("run_ref"), "the run ref must survive into metadata"
        assert t.title, "a feed row with no title is invisible in /deep-work"


# ---------------------------------------------------------------------------
# 2. The shift records what it filed
# ---------------------------------------------------------------------------


async def test_shift_records_the_task_ids_it_filed(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph, dispatcher, recording_bus
):
    """The join must be stored, not re-derived. ``ShiftDoc.task_ids`` carries
    exactly the ids of the Tasks the shift filed.

    MUTATION THAT BREAKS THIS: drop the ``record_shift_task_ids`` call after the
    dispatch loop — ``task_ids`` reads ``[]`` and the equality assert fails."""
    await _seed_default_agent()
    client = _make_client(monkeypatch)

    _mandate_id, shift_id = await _run_shift_to_dispatch(client, tmp_path)

    doc = await ShiftDoc.get(shift_id)
    assert doc is not None
    assert doc.task_ids, "the shift recorded no task ids"

    rows = await _TaskDoc.find(_TaskDoc.workspace_id == WS).to_list()
    mirrored_ids = {str(t.id) for t in rows if t.source and t.source.type == "belt_shift"}
    assert set(doc.task_ids) == mirrored_ids


# ---------------------------------------------------------------------------
# 3. The mirror is a courtesy — it may never fail the shift
# ---------------------------------------------------------------------------


async def test_a_feed_mirror_failure_never_fails_the_shift(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph, dispatcher, recording_bus
):
    """A tasks-service outage must cost the feed rows and nothing else. The
    shift still dispatches, the approve still returns 200.

    This is the whole reason the helper swallows ``Exception``: the Belt's job
    is the dispatch, and a broken side-channel that halts real work is a worse
    failure than a missing feed row.

    MUTATION THAT BREAKS THIS: narrow the ``except Exception`` in
    ``_mirror_task_to_feed`` to ``except NotFound`` (or remove the try) — the
    RuntimeError escapes and the approve raises instead of returning 200."""
    await _seed_default_agent()

    async def _boom(*a, **k):
        raise RuntimeError("tasks service is down")

    monkeypatch.setattr(tasks_service, "agent_create_task", _boom)

    client = _make_client(monkeypatch)
    _mandate_id, shift_id = await _run_shift_to_dispatch(client, tmp_path)

    # The dispatch still happened...
    assert RecorderDispatcher.instances[-1].calls

    # ...and — the assertion that actually bites — the shift RAN TO COMPLETION.
    # A 200 from the approve route proves nothing here: the router swallows
    # executor exceptions, so a mirror that killed the shift mid-dispatch still
    # answers 200 with a shift stuck in ``in_gate``. Only the success terminal
    # (state=done + the dispatch outcome) distinguishes the two.
    doc = await ShiftDoc.get(shift_id)
    assert doc is not None
    assert doc.state == "done", f"the mirror failure aborted the shift (state={doc.state})"
    assert doc.outcome and "dispatched" in doc.outcome
    # The only casualty is the feed row.
    assert doc.task_ids == []


# ---------------------------------------------------------------------------
# 4. Tenancy
# ---------------------------------------------------------------------------


async def test_mirrored_tasks_land_in_the_mandates_workspace_only(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph, dispatcher, recording_bus
):
    """The mirror runs from a background executor, not a request — the one
    place a workspace id is easy to lose. Every filed row must carry the
    mandate's workspace and a neighbouring tenant must see none of them.

    MUTATION THAT BREAKS THIS: build the mirror's ``RequestContext`` with a
    hardcoded or empty ``workspace_id`` — the OTHER_WS query stops being empty
    or the WS query stops finding the rows."""
    await _seed_default_agent()
    client = _make_client(monkeypatch)

    await _run_shift_to_dispatch(client, tmp_path)

    ours = [
        t
        for t in await _TaskDoc.find(_TaskDoc.workspace_id == WS).to_list()
        if t.source and t.source.type == "belt_shift"
    ]
    theirs = await _TaskDoc.find(_TaskDoc.workspace_id == OTHER_WS).to_list()

    assert ours, "the mandate's own workspace sees no mirrored rows"
    assert theirs == [], "a mirrored row leaked into another tenant"


# ---------------------------------------------------------------------------
# 5. The run lands → the mirrored task is done
# ---------------------------------------------------------------------------
#
# The mirror's other half. Without it every row T-14 files sits in ``proposed``
# forever — a feed that fills and never drains, which is worse than the empty
# feed the mirror was added to fix.
#
# The join is ``source.metadata.run_ref == <landing action id>``. That holds
# because ``headless.develop`` clears ``station_pending`` by updating the SAME
# action's blob in place, so the id the mandate dispatcher returned as the run
# ref is the id that later lands here — the tests below propose FIRST and mirror
# against the real id rather than asserting that equality by hand.


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
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
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": str(cwd),
        },
    )


def _seed_repo(tmp_path: Path, *, with_remote: bool) -> Path:
    """A seeded working clone. ``with_remote`` picks which of the executor's
    TWO success terminals the landing takes."""
    work = tmp_path / ("work_remote" if with_remote else "work_local")
    if with_remote:
        bare = tmp_path / "origin.git"
        _git(tmp_path, "init", "--bare", str(bare))
        _git(tmp_path, "clone", str(bare), str(work))
    else:
        work.mkdir()
        _git(work, "init")
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")
    (work / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    if with_remote:
        _git(work, "push", "-u", "origin", "main")
    return work


def _allow_repo_root(monkeypatch, root: Path) -> None:
    """Authorize ``root`` for the belt executor's repo-path guard.

    The executor refuses any repo outside ``belt_repo_allowlist`` (default: the
    cwd's parent), so a tmp_path repo lands as a FAILED run with no terminal —
    and a completion test would silently assert against a run that never landed.
    Same shim shape as ``test_belt_console.settings_allowlist``.
    """
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(root)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())


def _good_diff() -> str:
    return (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'hi'\n"
        "+    return 'hello world'\n"
    )


class _FakePrOpener:
    async def open_pr(self, *, repo_path, branch, base_branch, title, body) -> str:
        return "https://github.com/acme/repo/pull/1"


async def _propose_developed_run(store, repo: Path):
    """A code_change Action in the DEVELOPED shape — station_pending cleared and
    a real diff attached, exactly what ``headless.develop`` leaves behind on the
    queued run's own blob."""
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger

    return await store.propose(
        pocket_id=WS,
        title="Station task — developed",
        description="developed from a mandate shift",
        recommendation="land it",
        trigger=ActionTrigger(type="agent", source="belt:mandate-dispatch", reason="test"),
        category=ActionCategory.EXTERNAL,
        priority=ActionPriority.HIGH,
        parameters={
            "_code_change": {
                "kind": "code_change",
                "schema": 2,
                "station_pending": False,
                "repo": str(repo),
                "diff": _good_diff(),
                "base_branch": "main",
                "workspace_id": WS,
                "requested_by": USER,
            }
        },
        assignee=USER,
        workspace_id=WS,
    )


async def _mirror_task_for(run_ref: str, workspace: str = WS) -> str:
    """File the Task T-14's mirror would have filed for this run."""
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
    from pocketpaw_ee.cloud.tasks.dto import AssigneeDTO, CreateTaskRequest, SourceDTO

    ctx = RequestContext(
        user_id=USER,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )
    created = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="Bump lodash",
            summary="",
            assignee=AssigneeDTO(kind="human", id=USER, name=""),
            source=SourceDTO(
                type="belt_shift",
                ref_id="shift-1",
                metadata={"mandate_id": "m1", "shift_no": 1, "run_ref": run_ref},
            ),
        ),
    )
    return str(created.id)


@pytest.mark.parametrize("with_remote", [True, False], ids=["pr-landing", "local-only-landing"])
async def test_landed_run_completes_its_mirrored_feed_task(
    tmp_path, mongo_db, store, journal, graph, monkeypatch, with_remote
):
    """A landed belt run flips its mirrored feed row to ``done``.

    Parametrised across BOTH of the executor's success terminals. The
    with-remote path ends at the PR ``mark_executed``; a repo with no origin
    ends at a separate local-only terminal ~150 lines later. Hooking only the
    first leaves every local landing's row stuck in ``proposed`` — which is why
    this is a parametrised test and not one happy-path case.

    MUTATION THAT BREAKS THIS: remove either ``_close_mirrored_feed_tasks``
    call site in ``belt/executor`` — the matching param fails on status."""
    from pocketpaw_ee.cloud.belt import executor as belt_executor

    _allow_repo_root(monkeypatch, tmp_path)
    repo = _seed_repo(tmp_path, with_remote=with_remote)
    action = await _propose_developed_run(store, repo)
    task_id = await _mirror_task_for(str(action.id))

    before = await _TaskDoc.get(task_id)
    assert before is not None and before.status != "done"

    await belt_executor.execute_approved_change(action, pr_opener=_FakePrOpener())

    after = await _TaskDoc.get(task_id)
    assert after is not None
    assert after.status == "done", "the landed run left its feed row open"
    assert "Landed" in (after.summary or ""), "the landing outcome never reached the row"


async def test_a_landed_run_never_completes_another_runs_task(
    tmp_path, mongo_db, store, journal, graph, monkeypatch
):
    """The join must be by run ref, not "any belt_shift row in the workspace".

    Two mirrored tasks, one landing: only the landing run's own row closes.
    Without this a single landed task would mark a whole shift's work done.

    MUTATION THAT BREAKS THIS: drop the ``source.metadata.run_ref`` clause from
    ``complete_tasks_for_source_ref``'s query — the sibling closes too."""
    from pocketpaw_ee.cloud.belt import executor as belt_executor

    _allow_repo_root(monkeypatch, tmp_path)
    repo = _seed_repo(tmp_path, with_remote=True)
    action = await _propose_developed_run(store, repo)
    mine = await _mirror_task_for(str(action.id))
    sibling = await _mirror_task_for("some-other-run-ref")
    # A NEIGHBOURING TENANT holding a row with the SAME run ref. Without a
    # workspace clause on the query this closes too — a cross-tenant write from
    # a background executor, the exact shape the mirror's own tenancy test
    # guards on the create side.
    other_tenant = await _mirror_task_for(str(action.id), workspace=OTHER_WS)

    await belt_executor.execute_approved_change(action, pr_opener=_FakePrOpener())

    assert (await _TaskDoc.get(mine)).status == "done"
    assert (await _TaskDoc.get(sibling)).status != "done", "a sibling run's task was closed"
    assert (
        await _TaskDoc.get(other_tenant)
    ).status != "done", "another tenant's task was closed"
