# tests/cloud/test_belt_autopilot.py — the Belt MANDATE autopilot + the REAL
# station task dispatcher (feat/belt-autopilot).
#
# Created: 2026-06-11.
#
# Two pieces under test:
#
#   PIECE 1 — the StationTaskDispatcher. When an approved plan dispatches, the
#     REAL station dispatcher (selected by POCKETPAW_MANDATE_DISPATCHER=station,
#     the default) files a real ``code_change`` Instinct Action per task in a
#     QUEUED state (station_pending=True, no diff). The console Runs read model
#     surfaces it as status=queued / stage=station. This is the closest REAL
#     thing to a headless station run: a genuinely headless diff-producing run
#     is NOT reachable (the belt develop station is an interactive chat-agent
#     loop), so we assert the persisted run record + its queued state, NOT a bus
#     echo. The ``bus`` env value restores the announce-only default — the
#     existing test_belt_mandates suite already proves that path with its
#     RecorderDispatcher patch.
#
#   PIECE 2 — autopilot. POST .../autopilot {action:start} persists
#     autopilot={on, users}, runs ONE cycle immediately (Foresight-seeded sim
#     personas → structured feedback through the EXISTING feedback service path),
#     and spawns a background loop; {action:stop} cancels it. The mock UserSim is
#     deterministic + seeded so the sightings are stable. A full loop test chains
#     autopilot → shift (mock foreman cites the autopilot sightings) → resolve
#     approve → the StationTaskDispatcher path.
#
# All tests run the deterministic mock LLM/UserSim (POCKETPAW_MANDATE_LLM=mock).

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.belt import service as belt_service  # noqa: E402
from pocketpaw_ee.cloud.decisions.service import (  # noqa: E402
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.cloud.mandates import autopilot as autopilot_mod  # noqa: E402
from pocketpaw_ee.cloud.mandates import executor as mandate_executor  # noqa: E402
from pocketpaw_ee.cloud.mandates import foreman  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.router import router as mandates_router  # noqa: E402
from pocketpaw_ee.instinct.router import router as instinct_router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "w1"
USER = "u1"


# ---------------------------------------------------------------------------
# fixtures — journal / graph / store / mock LLM + UserSim / dispatcher / client
# ---------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    journal_dep.reset_journal_cache()
    original = journal_dep._cached_journal

    def _stub() -> object:
        return j

    journal_dep._cached_journal = _stub  # type: ignore[assignment]
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
    st = InstinctStore(tmp_path / "instinct_autopilot.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: st)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda: st)
    return st


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """Deterministic mock foreman AND mock UserSim (both read
    POCKETPAW_MANDATE_LLM); the station dispatcher is the default. Reset after."""
    monkeypatch.setenv("POCKETPAW_MANDATE_LLM", "mock")
    monkeypatch.delenv("POCKETPAW_MANDATE_DISPATCHER", raising=False)
    foreman.set_mock_plan(None)
    yield
    foreman.set_mock_plan(None)


@pytest.fixture(autouse=True)
def _drain_autopilot_tasks():
    """Cancel any autopilot background tasks a test left running, so a leaked
    loop never bleeds into the next test (the registry is process-local)."""
    yield
    autopilot_mod._TASKS.clear()


def _make_client(monkeypatch, *, workspace_id: str = WS, user_id: str = USER) -> TestClient:
    import pocketpaw_ee.cloud.workspace.service as ws_svc

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


def _charter(budget: int = 3, says_no=None, boundaries=None) -> dict:
    return {
        "goal": "keep the surface healthy",
        "kpis": [{"name": "open_cves", "target": 0, "direction": "down"}],
        "says_no": says_no if says_no is not None else ["major version bumps"],
        "boundaries": boundaries if boundaries is not None else ["never touch auth code"],
        "budget": {"max_tasks_per_shift": budget, "gate_minutes_per_week": 15},
        "cadence": "manual",
    }


def _seed_repo(tmp_path: Path, name: str) -> Path:
    """A real git repo with a README + a couple of commits so the autopilot
    surface read has something to chew on (README + commit titles)."""
    import subprocess

    repo = tmp_path / name
    repo.mkdir()
    (repo / "README.md").write_text("# Demo\nA demo product for autopilot.\n", encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    import os

    full_env = {**os.environ, **env}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=full_env)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=full_env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: initial demo"], cwd=repo, check=True, env=full_env
    )
    (repo / "CHANGELOG.md").write_text("changes\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=full_env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "docs: add changelog"], cwd=repo, check=True, env=full_env
    )
    return repo


def _create_mandate(client: TestClient, repo_dir: Path, *, budget: int = 3, **charter_kw) -> str:
    res = client.post(
        "/belt/mandates",
        json={
            "name": "surface health",
            "surface": {"repo_id": str(repo_dir)},
            "charter": _charter(budget=budget, **charter_kw),
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["mandate"]["id"]


# ---------------------------------------------------------------------------
# PIECE 2 — autopilot start persists state + runs ONE cycle → sightings exist
# with source autopilot:*; stop cancels the background task.
# ---------------------------------------------------------------------------


async def test_autopilot_start_seeds_sightings_then_stop_cancels(
    tmp_path, mongo_db, store, monkeypatch, recording_bus
):
    client = _make_client(monkeypatch)
    repo = _seed_repo(tmp_path, "ap-repo")
    mandate_id = _create_mandate(client, repo)

    # START with 3 users — the response carries the persisted state AND the
    # immediate cycle's sightings already exist.
    res = client.post(
        f"/belt/mandates/{mandate_id}/autopilot", json={"action": "start", "users": 3}
    )
    assert res.status_code == 200, res.text
    detail = res.json()["mandate"]
    assert detail["autopilot"] == {"on": True, "users": 3}

    # The persisted state survives a re-read.
    again = client.get(f"/belt/mandates/{mandate_id}").json()
    assert again["autopilot"] == {"on": True, "users": 3}

    # The immediate cycle filed feedback sightings with source "autopilot:*".
    sightings = client.get(f"/belt/mandates/{mandate_id}/sightings").json()["sightings"]
    autop = [s for s in sightings if str(s["evidence"].get("source", "")).startswith("autopilot:")]
    assert autop, sightings
    # Severity is in range and the source names a persona.
    for s in autop:
        assert s["patrol"] == "feedback"
        assert 1 <= s["severity"] <= 5
        assert s["evidence"]["source"].startswith("autopilot:")

    # The autopilot-changed event rode the bus.
    changed = [e for e in recording_bus.events if e.type == "mandate.autopilot_changed"]
    assert changed and changed[-1].data == {
        "workspace_id": WS,
        "mandate_id": mandate_id,
        "on": True,
        "users": 3,
    }

    # STOP persists the off state (and cancels the background task — the task
    # lifecycle itself is asserted directly against the module below, since the
    # TestClient runs each request on its own short-lived event loop).
    res = client.post(f"/belt/mandates/{mandate_id}/autopilot", json={"action": "stop"})
    assert res.status_code == 200, res.text
    assert res.json()["mandate"]["autopilot"]["on"] is False
    assert client.get(f"/belt/mandates/{mandate_id}").json()["autopilot"]["on"] is False


async def test_autopilot_background_task_lifecycle(tmp_path, mongo_db, store, monkeypatch):
    """The background loop's start/stop lifecycle, asserted directly against the
    autopilot module (the TestClient path can't observe the task because each
    HTTP request runs on its own short-lived event loop). START registers a live
    task; STOP cancels + de-registers it."""
    repo = _seed_repo(tmp_path, "lifecycle-repo")
    created = await mandate_service.create_mandate(
        WS, USER, {"name": "m", "surface": {"repo_id": str(repo)}, "charter": _charter()}
    )
    mandate_id = created["mandate"]["id"]

    assert not autopilot_mod.is_running(mandate_id)
    # run_immediate=False so the long interval sleep keeps the task alive to observe.
    await autopilot_mod.start_autopilot(WS, mandate_id, 2, run_immediate=False)
    assert autopilot_mod.is_running(mandate_id)

    await autopilot_mod.stop_autopilot(mandate_id)
    assert not autopilot_mod.is_running(mandate_id)
    # Idempotent — a second stop is a no-op.
    await autopilot_mod.stop_autopilot(mandate_id)


async def test_autopilot_users_clamped_and_deterministic(tmp_path, mongo_db, store, monkeypatch):
    """``users`` is clamped to 1-10 by the DTO (an over-max request is a 422),
    and the mock personas are deterministic + seeded."""
    client = _make_client(monkeypatch)
    repo = _seed_repo(tmp_path, "clamp-repo")
    mandate_id = _create_mandate(client, repo)

    # Over the cap → 422 (DTO clamp), nothing started.
    res = client.post(
        f"/belt/mandates/{mandate_id}/autopilot", json={"action": "start", "users": 99}
    )
    assert res.status_code == 422, res.text
    assert not autopilot_mod.is_running(mandate_id)

    # The mock personas are deterministic for the same count.
    a = [p.name for p in autopilot_mod.build_personas(4)]
    b = [p.name for p in autopilot_mod.build_personas(4)]
    assert a == b and len(a) == 4


async def test_autopilot_default_users_is_three(tmp_path, mongo_db, store, monkeypatch):
    """Omitting ``users`` defaults to 3 (the brief's default)."""
    client = _make_client(monkeypatch)
    repo = _seed_repo(tmp_path, "default-repo")
    mandate_id = _create_mandate(client, repo)
    res = client.post(f"/belt/mandates/{mandate_id}/autopilot", json={"action": "start"})
    assert res.status_code == 200, res.text
    assert res.json()["mandate"]["autopilot"]["users"] == 3


# ---------------------------------------------------------------------------
# PIECE 1 + 2 full loop — autopilot seeds sightings → shift (foreman cites them)
# → resolve approve → the StationTaskDispatcher files queued station runs.
# ---------------------------------------------------------------------------


async def test_full_loop_autopilot_to_station_runs(
    tmp_path, mongo_db, store, journal, graph, monkeypatch, recording_bus
):
    """The end-to-end mandate loop with the REAL station dispatcher (default):
    autopilot cycle seeds feedback sightings → trigger shift (the mock foreman
    plans tasks citing those sightings) → resolve-approve → the REAL
    StationTaskDispatcher files a queued ``code_change`` station run per task that
    the console Runs read model surfaces as status=queued / stage=station."""
    client = _make_client(monkeypatch)
    repo = _seed_repo(tmp_path, "loop-repo")
    mandate_id = _create_mandate(client, repo, budget=3)

    # 1. Autopilot one cycle → feedback sightings from sim personas.
    res = client.post(
        f"/belt/mandates/{mandate_id}/autopilot", json={"action": "start", "users": 2}
    )
    assert res.status_code == 200, res.text
    sightings = client.get(f"/belt/mandates/{mandate_id}/sightings").json()["sightings"]
    autop = [s for s in sightings if str(s["evidence"].get("source", "")).startswith("autopilot:")]
    assert autop, "autopilot must have seeded feedback sightings"

    # Stop autopilot so its background loop doesn't seed mid-shift.
    client.post(f"/belt/mandates/{mandate_id}/autopilot", json={"action": "stop"})

    # 2. Trigger the shift — the mock foreman plans one task per sighting,
    #    citing the autopilot sighting ids.
    shift = client.post(f"/belt/mandates/{mandate_id}/shift").json()["shift"]
    assert shift["state"] == "in_gate"
    assert shift["task_count"] >= 1
    plan_action_id = shift["plan_action_id"]

    # The plan cites the autopilot sightings (evidence_refs are sighting ids).
    action = await store.get_action(plan_action_id)
    blob = action.parameters["_belt_plan"]
    cited = {ref for t in blob["plan"]["tasks"] for ref in t["evidence_refs"]}
    autop_ids = {s["id"] for s in autop}
    assert cited & autop_ids, "the foreman must cite at least one autopilot sighting"

    n_tasks = len(blob["plan"]["tasks"])

    # 3. Resolve-approve every task → the REAL StationTaskDispatcher runs (no
    #    recorder patch — this is the genuine station path).
    res = client.post(
        f"/belt/mandates/{mandate_id}/plan/resolve",
        json={
            "shift_no": shift["no"],
            "decisions": [{"index": i, "decision": "approve"} for i in range(n_tasks)],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["shift"]["state"] == "done"

    final = await store.get_action(plan_action_id)
    assert final.status == ActionStatus.EXECUTED, final.outcome

    # 4. The station dispatcher filed a REAL code_change run per approved task,
    #    in a QUEUED state — assert the persisted run record, NOT a bus echo.
    runs = await belt_service.list_runs(WS)
    station_runs = [r for r in runs["runs"] if r["status"] == "queued"]
    assert len(station_runs) == n_tasks, station_runs
    for r in station_runs:
        assert r["stage"] == "station"
        assert r["repo"] == str(repo)  # the station is pre-bound to the mandate repo
        assert r["task"]  # the task text rides on the run

    # The underlying Instinct Action is a real pending code_change with the
    # station_pending marker (the queued station run, not a diff).
    sample = await store.get_action(station_runs[0]["action_id"])
    assert sample.status == ActionStatus.PENDING
    cc = sample.parameters["_code_change"]
    assert cc["kind"] == "code_change"
    assert cc["station_pending"] is True
    assert cc["mandate_id"] == mandate_id

    # A queued station run is NOT applyable — a stray approve fails loud
    # (StationPending) rather than applying a non-existent diff.
    from pocketpaw_ee.cloud.belt.executor import execute_approved_change

    await store.approve(sample.id)
    approved = await store.get_action(sample.id)
    await execute_approved_change(approved)
    after = await store.get_action(sample.id)
    assert after.status == ActionStatus.FAILED
    assert "station" in (after.error or "").lower()


# ---------------------------------------------------------------------------
# PIECE 1 — dispatcher selection: env=bus restores the announce-only default.
# ---------------------------------------------------------------------------


def test_dispatcher_selection_env(monkeypatch):
    """POCKETPAW_MANDATE_DISPATCHER selects the dispatcher: default + 'station'
    → the REAL StationTaskDispatcher; 'bus' → the announce-only BusTaskDispatcher
    (the prior default, still proven by test_belt_mandates' RecorderDispatcher)."""
    monkeypatch.delenv("POCKETPAW_MANDATE_DISPATCHER", raising=False)
    assert isinstance(mandate_executor.resolve_dispatcher(), mandate_executor.StationTaskDispatcher)

    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "station")
    assert isinstance(mandate_executor.resolve_dispatcher(), mandate_executor.StationTaskDispatcher)

    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "bus")
    assert isinstance(mandate_executor.resolve_dispatcher(), mandate_executor.BusTaskDispatcher)

    # An unrecognized value falls back to the station default.
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "nonsense")
    assert isinstance(mandate_executor.resolve_dispatcher(), mandate_executor.StationTaskDispatcher)


async def test_bus_dispatcher_announces_only(tmp_path, mongo_db, store, monkeypatch, recording_bus):
    """With env=bus the approved plan dispatches via the announce-only path: a
    ``belt_run_updated`` event fires per task and NO code_change station run is
    created (the prior behavior the existing suite relies on)."""
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "bus")
    client = _make_client(monkeypatch)
    repo = tmp_path / "bus-repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)

    client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "one signal", "severity": 4, "source": "support"},
    )
    shift = client.post(f"/belt/mandates/{mandate_id}/shift").json()["shift"]
    res = client.post(
        f"/belt/mandates/{mandate_id}/plan/resolve",
        json={"shift_no": shift["no"], "decisions": [{"index": 0, "decision": "approve"}]},
    )
    assert res.status_code == 200, res.text

    # No code_change station runs were created (bus path only announces).
    runs = await belt_service.list_runs(WS)
    assert runs["runs"] == []
    # The announce event fired (status=dispatched, stage=station).
    announces = [e for e in recording_bus.events if e.type == "belt_run_updated"]
    assert announces and any(e.data.get("status") == "dispatched" for e in announces)


# ---------------------------------------------------------------------------
# tenant isolation — autopilot endpoint never confirms a foreign mandate exists.
# ---------------------------------------------------------------------------


async def test_autopilot_tenant_isolation(tmp_path, mongo_db, store, monkeypatch):
    client_w1 = _make_client(monkeypatch)
    repo = _seed_repo(tmp_path, "iso-repo")
    mandate_id = _create_mandate(client_w1, repo)

    client_w2 = _make_client(monkeypatch, workspace_id="w2", user_id="u2")
    # A foreign workspace can't start autopilot on w1's mandate — 404, never a
    # confirmation that the mandate exists, and no task is registered.
    res = client_w2.post(f"/belt/mandates/{mandate_id}/autopilot", json={"action": "start"})
    assert res.status_code == 404, res.text
    assert not autopilot_mod.is_running(mandate_id)

    res = client_w2.post(f"/belt/mandates/{mandate_id}/autopilot", json={"action": "stop"})
    assert res.status_code == 404, res.text

    # w1 still owns it.
    res = client_w1.post(f"/belt/mandates/{mandate_id}/autopilot", json={"action": "start"})
    assert res.status_code == 200, res.text
