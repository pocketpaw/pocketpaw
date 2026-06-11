# tests/cloud/test_belt_mandates.py — the Belt MANDATE primitive (feat/belt-mandates).
#
# Created: 2026-06-11.
#
# THE HARD GATE — ``test_full_shift_gate_one_clean_chain`` drives the REAL
# production path with NO stubs at the propose/execute seam (the documented
# chain-doubling lesson): the real mandates router (TestClient), the real
# foreman pipeline (mock LLM transport selected via POCKETPAW_MANDATE_LLM —
# the one genuine external boundary), the real Instinct store, the real
# instinct-router approve dispatch over HTTP, and the real plan executor. The
# ONLY other fake is the TaskDispatcher default (the develop-station agent
# session — the second genuine external boundary), patched the same way
# test_belt_trace patches GhCliPrOpener. The Decision-Graph journal +
# projection are the REAL singletons.
#
# Expected chain shape for a dispatched shift — ONE chain, ONE terminal:
#   agent.proposed                              (service.trigger_shift)
#     → human.corrected(disposition=accepted)   (instinct router approve)
#     → decision.completed(passed=True,         (plan executor)
#                          action_outcome="dispatched", task_count=N)
#
# Stood-down shift — ONE chain that opens AND closes in the trigger:
#   agent.proposed → decision.completed(passed=True, action_outcome="stood_down")
#
# Also pinned: budget cap enforced (422, nothing reaches the gate); the
# boundary check reads ACTION fields only (a ``why`` that names the forbidden
# thing passes — that's a refusal, not a violation); patrol intake → sighting;
# deps patrol against a real manifest; tenant isolation on every read.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
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
# fixtures — journal / graph / store / mock-LLM / dispatcher recorder / client
# ---------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path):
    """Fresh on-disk journal wired into the lazy ``get_journal`` lookup —
    production code and the test read the same singleton."""
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
    """Fresh DecisionGraph as the process-global singleton."""
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore wired everywhere the gate reads it (the mandates
    service, the instinct router, and the plan executor all resolve through
    ``pocketpaw.stores.get_instinct_store`` or the router indirection)."""
    st = InstinctStore(tmp_path / "instinct_mandates.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: st)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda: st)
    return st


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """Every test in this module runs the deterministic mock foreman. The
    scripted override is reset after each test."""
    monkeypatch.setenv("POCKETPAW_MANDATE_LLM", "mock")
    foreman.set_mock_plan(None)
    yield
    foreman.set_mock_plan(None)


class RecorderDispatcher:
    """Records dispatch calls — the develop-station boundary, the analogue of
    test_belt_trace's FakePrOpener. The router→executor seam stays REAL."""

    instances: list[RecorderDispatcher] = []

    def __init__(self) -> None:
        self.calls: list[dict] = []
        RecorderDispatcher.instances.append(self)

    async def dispatch(
        self, *, workspace_id, mandate_id, shift_no, plan_action_id, index, task
    ) -> str:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "mandate_id": mandate_id,
                "shift_no": shift_no,
                "plan_action_id": plan_action_id,
                "index": index,
                "task": task,
            }
        )
        return f"{plan_action_id}:t{index}"


@pytest.fixture
def dispatcher(monkeypatch) -> type[RecorderDispatcher]:
    """Make the executor's DEFAULT dispatcher the recorder, keeping the
    router→executor seam real — only the station boundary is replaced.

    The default dispatcher is now ``station`` (feat/belt-autopilot); these tests
    exercise the dispatch SEAM (the recorder), so pin the selection to ``bus``
    and patch ``BusTaskDispatcher`` to the recorder — ``resolve_dispatcher()``
    then returns the recorder. The real StationTaskDispatcher path is covered by
    test_belt_autopilot."""
    RecorderDispatcher.instances = []
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "bus")
    monkeypatch.setattr(mandate_executor, "BusTaskDispatcher", RecorderDispatcher)
    return RecorderDispatcher


def _make_client(monkeypatch, *, workspace_id: str = WS, user_id: str = USER) -> TestClient:
    """One app holding BOTH routers (mandates + instinct) with the real RBAC
    guard, an admin user, license bypassed, enterprise plan."""
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
        "goal": "keep dependencies fresh and CVE-free",
        "kpis": [{"name": "open_cves", "target": 0, "direction": "down"}],
        "says_no": says_no if says_no is not None else ["major version bumps"],
        "boundaries": boundaries if boundaries is not None else ["never touch auth code"],
        "budget": {"max_tasks_per_shift": budget, "gate_minutes_per_week": 15},
        "cadence": "manual",
    }


def _create_mandate(client: TestClient, repo_dir: Path, *, budget: int = 3, **charter_kw) -> str:
    res = client.post(
        "/belt/mandates",
        json={
            "name": "deps freshness",
            "surface": {"repo_id": str(repo_dir)},
            "charter": _charter(budget=budget, **charter_kw),
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["mandate"]["id"]


def _events(journal, action: str) -> list:
    return [e for e in journal.replay_from(0) if e.action == action]


def _chain(journal, correlation_id: UUID) -> list:
    return [e for e in journal.replay_from(0) if e.correlation_id == correlation_id]


# ---------------------------------------------------------------------------
# THE PRODUCTION-PATH GATE TEST — create → feedback → shift → approve →
# dispatch → EXACTLY ONE decision.completed
# ---------------------------------------------------------------------------


async def test_full_shift_gate_one_clean_chain(
    tmp_path, mongo_db, store, journal, graph, dispatcher, monkeypatch, recording_bus
):
    """Create mandate → seed 2 feedback sightings → trigger shift (mock LLM
    plans 2 tasks) → the plan lands as a pending ``belt_plan`` Instinct Action →
    approve over the REAL instinct router HTTP path → the REAL plan executor
    dispatches both tasks as belt runs → the chain holds EXACTLY ONE
    decision.completed (the chain-doubling trap)."""
    client = _make_client(monkeypatch)
    repo = tmp_path / "surface-repo"
    repo.mkdir()  # empty repo dir — the deps patrol stays quiet on purpose

    mandate_id = _create_mandate(client, repo)

    # Seed two feedback sightings through the intake patrol.
    for text in ("builds got slower after the last release", "lodash CVE flagged by a customer"):
        res = client.post(
            f"/belt/mandates/{mandate_id}/feedback",
            json={"text": text, "severity": 4, "source": "support"},
        )
        assert res.status_code == 200, res.text

    # Trigger the shift — the mock foreman plans one task per sighting (2).
    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text
    shift = res.json()["shift"]
    assert shift["state"] == "in_gate"
    assert shift["task_count"] == 2
    plan_action_id = shift["plan_action_id"]
    assert plan_action_id

    # The plan landed as a PENDING belt_plan Instinct Action with the blob.
    action = await store.get_action(plan_action_id)
    assert action is not None and action.status == ActionStatus.PENDING
    blob = action.parameters["_belt_plan"]
    assert blob["kind"] == "belt_plan"
    assert blob["workspace_id"] == WS
    assert blob["budget_max_tasks"] == 3
    assert len(blob["plan"]["tasks"]) == 2
    # Every task cites a sighting id.
    for task in blob["plan"]["tasks"]:
        assert task["evidence_refs"], task
    corr = UUID(blob["correlation_id"])

    # agent.proposed fired at the trigger, before any approval.
    proposed = _events(journal, "agent.proposed")
    assert len(proposed) == 1
    assert proposed[0].correlation_id == corr
    assert proposed[0].causation_id is None
    assert proposed[0].payload["action"] == "belt_plan"

    # Approve over HTTP — the REAL router dispatch fires human.corrected then
    # the REAL plan executor re-validates, dispatches, and closes the chain.
    res = client.post(f"/instinct/actions/{plan_action_id}/approve")
    assert res.status_code == 200, res.text

    final = await store.get_action(plan_action_id)
    assert final.status == ActionStatus.EXECUTED, final.outcome

    # Belt runs dispatched — one dispatcher call per approved task.
    assert len(RecorderDispatcher.instances) == 1
    calls = RecorderDispatcher.instances[0].calls
    assert len(calls) == 2
    assert {c["index"] for c in calls} == {1, 2}
    assert all(c["mandate_id"] == mandate_id and c["workspace_id"] == WS for c in calls)

    # EXACTLY three events, one chain, causal order intact.
    chain = _chain(journal, corr)
    assert [e.action for e in chain] == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], [e.action for e in chain]
    proposed_e, human_e, completed_e = chain
    assert human_e.causation_id == proposed_e.id
    assert completed_e.causation_id == human_e.id
    assert human_e.payload["disposition"] == "accepted"
    assert completed_e.payload["passed"] is True
    assert completed_e.payload["action_outcome"] == "dispatched"
    assert completed_e.payload["task_count"] == 2

    # THE TRAP — exactly ONE terminal in the whole journal.
    assert len(_events(journal, "decision.completed")) == 1
    assert len(_events(journal, "agent.proposed")) == 1
    assert len(_events(journal, "human.corrected")) == 1

    # The shift record reflects the dispatch.
    detail = client.get(f"/belt/mandates/{mandate_id}").json()
    assert detail["recent_shifts"][0]["state"] == "done"

    # Pawprints read past-tense: proposed → approved → executed.
    prints = client.get(f"/belt/mandates/{mandate_id}/pawprints").json()["pawprints"]
    kinds = [p["kind"] for p in prints]
    assert kinds == ["proposed", "approved", "executed"], kinds
    assert prints[0]["evidence_refs"]  # the cited sighting ids surface
    # UI contract item shape: {id, mandate_id, shift_no, kind, summary,
    # evidence_refs, ts}.
    for item in prints:
        assert set(item) == {
            "id",
            "mandate_id",
            "shift_no",
            "kind",
            "summary",
            "evidence_refs",
            "ts",
        }, item
        assert item["mandate_id"] == mandate_id
        assert item["summary"].startswith("Shift 1:")

    # UI contract — the plan proposal rode the realtime bus on the
    # ``belt_plan`` topic with {mandate_id, proposal} (workspace_id rides
    # along for the audience fan-out).
    plan_events = [e for e in recording_bus.events if e.type == "belt_plan"]
    assert len(plan_events) == 1
    payload = plan_events[0].data
    assert payload["mandate_id"] == mandate_id
    assert payload["workspace_id"] == WS
    assert payload["proposal"]["plan_action_id"] == plan_action_id
    assert len(payload["proposal"]["tasks"]) == 2


# ---------------------------------------------------------------------------
# no_action → stood_down (a SUCCESS state, one clean 2-event chain)
# ---------------------------------------------------------------------------


async def test_no_action_stands_down(tmp_path, mongo_db, store, journal, graph, monkeypatch):
    """A quiet surface (no sightings) makes the mock foreman return an empty
    plan: the shift stands down as a SUCCESS — chain opens AND closes in the
    trigger with exactly one decision.completed(stood_down); nothing reaches
    the Instinct gate."""
    client = _make_client(monkeypatch)
    repo = tmp_path / "quiet-repo"
    repo.mkdir()

    mandate_id = _create_mandate(client, repo)
    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text
    shift = res.json()["shift"]
    assert shift["state"] == "stood_down"
    assert shift["plan_action_id"] is None
    assert shift["task_count"] == 0
    assert shift["no_action_reason"]

    # One 2-event chain: agent.proposed → decision.completed(stood_down).
    proposed = _events(journal, "agent.proposed")
    completed = _events(journal, "decision.completed")
    assert len(proposed) == 1 and len(completed) == 1
    assert completed[0].correlation_id == proposed[0].correlation_id
    assert completed[0].causation_id == proposed[0].id
    assert completed[0].payload["passed"] is True
    assert completed[0].payload["action_outcome"] == "stood_down"

    # Nothing reached the gate.
    assert await store.pending() == []

    # The pawprints feed reads the stand-down.
    prints = client.get(f"/belt/mandates/{mandate_id}/pawprints").json()["pawprints"]
    assert [p["kind"] for p in prints] == ["stood_down"]


# ---------------------------------------------------------------------------
# budget cap enforced — an over-budget plan never reaches the gate
# ---------------------------------------------------------------------------


async def test_budget_cap_enforced(tmp_path, mongo_db, store, journal, graph, monkeypatch):
    """A misbehaving foreman returning more tasks than the charter budget is
    refused by machine validation (422) — no Instinct Action is created."""
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo, budget=1)

    res = client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "two things broke", "source": "support"},
    )
    sighting_id = res.json()["id"]

    foreman.set_mock_plan(
        {
            "shift_no": 1,
            "no_action": False,
            "no_action_reason": None,
            "tasks": [
                {
                    "title": f"task {i}",
                    "why": "needed",
                    "evidence_refs": [sighting_id],
                    "expected_outcome": "open_cves down",
                    "est_cost_hours": 1.0,
                }
                for i in (1, 2)
            ],
        }
    )
    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 422, res.text
    assert "budget" in res.json()["error"]["message"]
    assert await store.pending() == []  # nothing reached the gate


# ---------------------------------------------------------------------------
# boundary check — ACTION fields only; the why narration is never scanned
# ---------------------------------------------------------------------------


async def test_boundary_check_ignores_why(tmp_path, mongo_db, store, journal, graph, monkeypatch):
    """A task whose ``why`` names the forbidden phrase (a refusal explanation)
    PASSES; the same phrase in the ``title`` (an action field) is refused."""
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo, says_no=["major version bumps"])

    res = client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "lodash is stale", "source": "support"},
    )
    sighting_id = res.json()["id"]

    def _plan(title: str, why: str) -> dict:
        return {
            "shift_no": 1,
            "no_action": False,
            "no_action_reason": None,
            "tasks": [
                {
                    "title": title,
                    "why": why,
                    "evidence_refs": [sighting_id],
                    "expected_outcome": "open_cves down; sighting resolved",
                    "est_cost_hours": 1.0,
                }
            ],
        }

    # PASS — the why names the forbidden phrase while refusing it.
    foreman.set_mock_plan(
        _plan(
            "bump lodash to the latest 4.x patch release",
            "We deliberately avoid major version bumps per the charter, so this "
            "stays on 4.x — patch upgrade only.",
        )
    )
    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text
    assert res.json()["shift"]["state"] == "in_gate"

    # FAIL — the same phrase in the TITLE (an action field) is refused.
    foreman.set_mock_plan(
        _plan("do major version bumps across the repo", "the fastest route to zero CVEs")
    )
    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 422, res.text
    assert "boundary" in res.json()["error"]["message"]


# ---------------------------------------------------------------------------
# patrol intake → sighting; deps patrol against a real manifest
# ---------------------------------------------------------------------------


async def test_feedback_intake_creates_sighting(tmp_path, mongo_db, store, monkeypatch):
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)

    res = client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "checkout flow feels slow", "source": "slack"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["patrol"] == "feedback"
    assert body["severity"] == 3  # default when omitted

    listed = client.get(f"/belt/mandates/{mandate_id}/sightings").json()["sightings"]
    assert len(listed) == 1
    assert listed[0]["summary"] == "checkout flow feels slow"
    assert listed[0]["evidence"]["source"] == "slack"


async def test_teaching_feedback_shape(tmp_path, mongo_db, store, monkeypatch):
    """The gate UI's teaching shape ({kind, reason, shift_no?, task_title?})
    returns {ok: true} and still lands as a feedback Sighting the foreman's
    next digest will see — discriminated from the general shape on ``kind``."""
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)

    res = client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={
            "kind": "reject",
            "reason": "too risky during the release freeze",
            "shift_no": 1,
            "task_title": "bump lodash",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}

    listed = client.get(f"/belt/mandates/{mandate_id}/sightings").json()["sightings"]
    assert len(listed) == 1
    s = listed[0]
    assert s["patrol"] == "feedback"
    assert s["evidence"]["kind"] == "reject"
    assert s["evidence"]["source"] == "gate"
    assert s["evidence"]["task_title"] == "bump lodash"
    assert "too risky" in s["summary"]

    # The general shape keeps working side by side.
    res = client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "autopilot ping", "source": "autopilot"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["patrol"] == "feedback"


async def test_deps_patrol_flags_known_stale_manifest_entries(tmp_path, mongo_db, store):
    """The deps patrol parses a REAL pyproject.toml and files sightings for
    entries in the (demo-bar) stub advisory table — deduped on re-run."""
    repo = tmp_path / "pyrepo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n'
        'dependencies = ["requests>=2.28", "totally-fine-pkg==1.0"]\n',
        encoding="utf-8",
    )
    created = await mandate_service.create_mandate(
        WS,
        USER,
        {"name": "m", "surface": {"repo_id": str(repo)}, "charter": _charter()},
    )
    mandate_id = created["mandate"]["id"]

    out = await mandate_service.run_patrols(WS, USER, mandate_id)
    assert len(out["sightings"]) == 1
    s = out["sightings"][0]
    assert s["patrol"] == "deps"
    assert s["evidence"]["package"] == "requests"
    assert s["evidence"]["cve"].startswith("CVE-")

    # Re-running the patrol does not duplicate the sighting.
    again = await mandate_service.run_patrols(WS, USER, mandate_id)
    assert again["sightings"] == []


async def test_patrols_toggles_scope_the_sense_loop(tmp_path, mongo_db, store):
    """UI contract — a mandate created with ``patrols: ["feedback"]`` never
    runs the deps patrol, even against a manifest full of stale entries."""
    repo = tmp_path / "pyrepo2"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = ["requests>=2.28"]\n',
        encoding="utf-8",
    )
    created = await mandate_service.create_mandate(
        WS,
        USER,
        {
            "name": "m2",
            "surface": {"repo_id": str(repo)},
            "charter": _charter(),
            "patrols": ["feedback"],
        },
    )
    assert created["mandate"]["patrols"] == ["feedback"]
    out = await mandate_service.run_patrols(WS, USER, created["mandate"]["id"])
    assert out["sightings"] == []  # deps patrol toggled off


# ---------------------------------------------------------------------------
# tenant isolation — reads never confirm a foreign mandate exists
# ---------------------------------------------------------------------------


async def test_tenant_isolation_on_reads(tmp_path, mongo_db, store, monkeypatch):
    client_w1 = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client_w1, repo)

    client_w2 = _make_client(monkeypatch, workspace_id="w2", user_id="u2")
    # Detail, sightings, pawprints, feedback: all 404 — never confirm existence.
    assert client_w2.get(f"/belt/mandates/{mandate_id}").status_code == 404
    assert client_w2.get(f"/belt/mandates/{mandate_id}/sightings").status_code == 404
    assert client_w2.get(f"/belt/mandates/{mandate_id}/pawprints").status_code == 404
    res = client_w2.post(f"/belt/mandates/{mandate_id}/feedback", json={"text": "x", "source": "s"})
    assert res.status_code == 404
    # The list is workspace-scoped.
    assert client_w2.get("/belt/mandates").json()["mandates"] == []
    assert len(client_w1.get("/belt/mandates").json()["mandates"]) == 1


# ---------------------------------------------------------------------------
# reject path — the router closes the chain; the shift records the rejection
# ---------------------------------------------------------------------------


async def test_reject_closes_chain_once(
    tmp_path, mongo_db, store, journal, graph, dispatcher, monkeypatch
):
    """Rejecting the plan at the gate closes the chain in the ROUTER (the plan
    executor never runs): agent.proposed → human.corrected(rejected) →
    decision.completed(rejected) — exactly one terminal, zero dispatches."""
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)
    client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "minor papercut", "source": "support"},
    )
    shift = client.post(f"/belt/mandates/{mandate_id}/shift").json()["shift"]
    plan_action_id = shift["plan_action_id"]

    res = client.post(
        f"/instinct/actions/{plan_action_id}/reject",
        json={"reason": "not this week — freeze is on"},
    )
    assert res.status_code == 200, res.text

    final = await store.get_action(plan_action_id)
    assert final.status == ActionStatus.REJECTED

    completed = _events(journal, "decision.completed")
    assert len(completed) == 1
    assert completed[0].payload["passed"] is False
    assert completed[0].payload["action_outcome"] == "rejected"
    human = _events(journal, "human.corrected")
    assert len(human) == 1
    assert human[0].payload["disposition"] == "rejected"
    assert completed[0].causation_id == human[0].id

    # No dispatches happened.
    assert all(not inst.calls for inst in RecorderDispatcher.instances)

    # The shift record reflects the rejection and pawprints read it.
    prints = client.get(f"/belt/mandates/{mandate_id}/pawprints").json()["pawprints"]
    assert [p["kind"] for p in prints] == ["proposed", "rejected"]


# ---------------------------------------------------------------------------
# plan/resolve — the console gate action, mapped onto the real instinct path
# ---------------------------------------------------------------------------


async def test_resolve_mixed_verdicts_dispatches_kept_tasks_one_terminal(
    tmp_path, mongo_db, store, journal, graph, dispatcher, monkeypatch
):
    """approve + reject + edit on a 3-task plan: the kept two tasks (one with
    the edited title) dispatch as belt runs through the REAL approve-with-edits
    path; the rejected task lands as a teaching sighting; the chain closes with
    EXACTLY ONE decision.completed; pawprints read kind=edited."""
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)

    for text in ("signal one", "signal two", "signal three"):
        client.post(
            f"/belt/mandates/{mandate_id}/feedback",
            json={"text": text, "severity": 3, "source": "support"},
        )
    shift = client.post(f"/belt/mandates/{mandate_id}/shift").json()["shift"]
    assert shift["task_count"] == 3
    plan_action_id = shift["plan_action_id"]
    action = await store.get_action(plan_action_id)
    corr = UUID(action.parameters["_belt_plan"]["correlation_id"])

    res = client.post(
        f"/belt/mandates/{mandate_id}/plan/resolve",
        json={
            "shift_no": shift["no"],
            "decisions": [
                {"index": 0, "decision": "approve"},
                {"index": 1, "decision": "reject", "reason": "not worth the risk"},
                {"index": 2, "decision": "edit", "edited_title": "tighter scoped fix"},
            ],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["shift"]["state"] == "done"

    final = await store.get_action(plan_action_id)
    assert final.status == ActionStatus.EXECUTED, final.outcome

    # Two kept tasks dispatched; the edited one carries the new title.
    calls = RecorderDispatcher.instances[0].calls
    assert len(calls) == 2
    titles = [c["task"]["title"] for c in calls]
    assert "tighter scoped fix" in titles

    # The chain closed EXACTLY once, through the real instinct edit path.
    chain = _chain(journal, corr)
    assert [e.action for e in chain] == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ]
    assert chain[1].payload["disposition"] == "edited"
    assert chain[2].payload["passed"] is True
    assert chain[2].payload["action_outcome"] == "dispatched"
    assert chain[2].payload["task_count"] == 2
    assert len(_events(journal, "decision.completed")) == 1

    # The rejected task became a teaching sighting with the human's reason.
    sightings = client.get(f"/belt/mandates/{mandate_id}/sightings").json()["sightings"]
    teaching = [s for s in sightings if s["evidence"].get("kind") == "reject"]
    assert len(teaching) == 1
    assert "not worth the risk" in teaching[0]["summary"]
    assert teaching[0]["evidence"]["shift_no"] == shift["no"]

    # Pawprints read the edited approval.
    prints = client.get(f"/belt/mandates/{mandate_id}/pawprints").json()["pawprints"]
    assert [p["kind"] for p in prints] == ["proposed", "edited", "executed"]


async def test_resolve_all_reject_closes_chain_once(
    tmp_path, mongo_db, store, journal, graph, dispatcher, monkeypatch
):
    """All tasks rejected → the REAL reject path closes the chain once; zero
    dispatches; the reasons land as teaching sightings."""
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)
    client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "one thing", "source": "support"},
    )
    shift = client.post(f"/belt/mandates/{mandate_id}/shift").json()["shift"]

    res = client.post(
        f"/belt/mandates/{mandate_id}/plan/resolve",
        json={
            "shift_no": shift["no"],
            "decisions": [{"index": 0, "decision": "reject", "reason": "freeze week"}],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["shift"]["state"] == "done"

    final = await store.get_action(shift["plan_action_id"])
    assert final.status == ActionStatus.REJECTED

    completed = _events(journal, "decision.completed")
    assert len(completed) == 1
    assert completed[0].payload["action_outcome"] == "rejected"
    assert all(not inst.calls for inst in RecorderDispatcher.instances)

    teaching = [
        s
        for s in client.get(f"/belt/mandates/{mandate_id}/sightings").json()["sightings"]
        if s["evidence"].get("kind") == "reject"
    ]
    assert len(teaching) == 1 and "freeze week" in teaching[0]["summary"]


async def test_resolve_requires_complete_decisions(
    tmp_path, mongo_db, store, journal, graph, monkeypatch
):
    """Every task needs exactly one decision; a partial verdict set is a 422
    and the plan stays pending at the gate."""
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)
    for text in ("a", "b"):
        client.post(
            f"/belt/mandates/{mandate_id}/feedback",
            json={"text": text, "source": "support"},
        )
    shift = client.post(f"/belt/mandates/{mandate_id}/shift").json()["shift"]
    assert shift["task_count"] == 2

    res = client.post(
        f"/belt/mandates/{mandate_id}/plan/resolve",
        json={"shift_no": shift["no"], "decisions": [{"index": 0, "decision": "approve"}]},
    )
    assert res.status_code == 422, res.text
    assert "missing indices" in res.json()["error"]["message"]
    final = await store.get_action(shift["plan_action_id"])
    assert final.status == ActionStatus.PENDING
