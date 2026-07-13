# tests/cloud/test_belt_trace.py — Belt & Pulley Decision-Graph chain (BS-4).
#
# Created: 2026-06-10 (feat/belt-trace, BS-4 — one station run = ONE Decision
# chain).
#
# THE HARD GATE — this drives the REAL production path with NO stubs at the
# seams between propose / approve / execute (the chain-doubling lesson,
# 2026-05-26): the real ``belt.py`` propose handler (ContextVars set in-test),
# the real Instinct store, the real Instinct router dispatch over a TestClient,
# and the real ``ee.cloud.belt.executor`` against a LOCAL bare-repo git fixture
# (reused from BS-3's ``test_belt_gate.py``). The ONLY fake is the PR opener —
# that is the one genuine external boundary (``gh pr create``), allowed. The
# Decision Graph journal + projection are the REAL singletons wired into the
# lazy global lookups (same fixture shape as ``test_instinct_decision_events``).
#
# Expected chain shape (documented per the brief) — N = 3 events, ONE chain per
# station run, sharing ONE correlation_id minted at propose:
#
#   EXECUTED (propose → approve → apply → PR):
#     agent.proposed                                  (belt.py propose)
#       → human.corrected(disposition=accepted)       (router approve)
#       → decision.completed(passed=True,             (executor mark_executed)
#                            action_outcome="landed",
#                            pr_url=..., branch=..., files_changed=N)
#
#   REJECTED (propose → reject):
#     agent.proposed                                  (belt.py propose)
#       → human.corrected(disposition=rejected)       (router reject)
#       → decision.completed(passed=False,            (router reject — executor
#                            action_outcome="rejected", never runs)
#                            reason=<comment>)
#
#   FAILED (propose → approve → apply conflict):
#     agent.proposed → human.corrected(accepted)
#       → decision.completed(passed=False, action_outcome="failed",
#                            error_class="ApplyConflict", reason=...)
#
# Causation walk: agent.proposed (origin, causation_id=None) ←
#   human.corrected (caused by agent.proposed) ← decision.completed (caused by
#   human.corrected). Each event cites the prior via causation_id.
#
# The doubled-terminal trap is the thing under test: every assertion counts the
# terminal events and proves EXACTLY ONE decision.completed lands per run, and a
# SECOND full cycle produces a SECOND distinct, clean chain.

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

_PATH = os.environ.get("PATH", "/usr/bin:/bin")

pytest.importorskip("pocketpaw_ee")

from unittest.mock import AsyncMock  # noqa: E402

import pocketpaw_ee.agent.mcp_servers.belt as belt  # noqa: E402
import pocketpaw_ee.cloud.belt.executor as belt_executor_module  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.cloud.decisions.service import (  # noqa: E402
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# Workspace + user the in-test identity binds. The Belt action's pocket_id
# carries the workspace (belt.py), so the chain scope is workspace-only.
WS = "w1"
USER = "u1"


# ---------------------------------------------------------------------------
# git bare-repo fixture (reused shape from test_belt_gate.py)
# ---------------------------------------------------------------------------


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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A bare repo as origin + a seeded working clone (returns the clone)."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))

    work = tmp_path / "work"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")

    (work / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-u", "origin", "main")
    return work


# ---------------------------------------------------------------------------
# decision-graph + journal + store wiring (real singletons)
# ---------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path):
    """Fresh on-disk journal wired into the lazy ``get_journal`` lookup the
    ``journal_writer`` helper resolves — production code and the test read the
    same singleton."""
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
    """Isolated InstinctStore wired everywhere the gate reads it (the MCP
    handler, the router's ``_store``, and the executor all resolve through
    ``pocketpaw.stores.get_instinct_store`` or the router indirection)."""
    st = InstinctStore(tmp_path / "instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: st)
    return st


@pytest.fixture
def allowlist(repo: Path, monkeypatch) -> None:
    """Point belt_repo_allowlist at the repo's parent so the real repo resolves
    inside the boundary."""
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(repo.parent)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())


# ---------------------------------------------------------------------------
# identity + router client + fake PR opener (the ONE allowed external fake)
# ---------------------------------------------------------------------------


class _identity:
    """Sets the workspace/user/session ContextVars the belt handler reads."""

    def __init__(self, *, workspace=WS, user=USER, session="sess-1"):
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


class _FakeUser:
    def __init__(self, user_id: str = USER, workspace_id: str = WS) -> None:
        self.id = user_id
        self.active_workspace = workspace_id

        class _M:
            def __init__(self, ws):
                self.workspace = ws
                self.role = "admin"

        self.workspaces = [_M(workspace_id)]


class FakePrOpener:
    """Records its call args, returns a fixed URL — never touches GitHub. This
    is the ONLY seam faked: the genuine external boundary (``gh pr create``)."""

    instances: list[FakePrOpener] = []

    def __init__(self, url: str = "https://github.com/acme/repo/pull/1"):
        self.url = url
        self.calls: list[dict] = []
        FakePrOpener.instances.append(self)

    async def open_pr(self, *, repo_path, branch, base_branch, title, body) -> str:
        self.calls.append({"branch": branch, "base_branch": base_branch, "title": title})
        return self.url


def _make_client(store: InstinctStore, user: _FakeUser, monkeypatch) -> TestClient:
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return TestClient(app)


def _patch_pr_opener(monkeypatch, url: str = "https://github.com/acme/repo/pull/1") -> None:
    """Make the executor's DEFAULT opener (the one the router triggers, since
    the router passes no ``pr_opener``) the fake. This keeps the router→executor
    seam REAL — only the external ``gh`` boundary is replaced."""
    monkeypatch.setattr(belt_executor_module, "GhCliPrOpener", lambda: FakePrOpener(url=url))


def _good_diff() -> str:
    return (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'hi'\n"
        "+    return 'hello world'\n"
    )


def _conflict_diff() -> str:
    """A diff that cannot apply against the seeded app.py (line not present)."""
    return (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def hello():\n"
        "-    return 'NONEXISTENT LINE THAT IS NOT IN THE FILE'\n"
        "+    return 'whatever'\n"
        " trailing context that does not match\n"
    )


async def _propose(repo: Path, diff: str, summary: str = "Friendlier greeting.") -> str:
    """Drive the REAL belt propose handler and return the action id."""
    with _identity():
        res = await belt._propose_change_handler(
            {
                "repo": str(repo),
                "base_branch": "main",
                "diff": diff,
                "summary": summary,
                "task": "Make hello() return a greeting.",
            }
        )
    assert res.get("is_error") is not True, res
    return json.loads(res["content"][0]["text"])["action_id"]


def _events(journal, action: str) -> list:
    return [e for e in journal.replay_from(0) if e.action == action]


def _chain(journal, correlation_id: UUID) -> list:
    return [e for e in journal.replay_from(0) if e.correlation_id == correlation_id]


async def _correlation_for(store: InstinctStore, action_id: str) -> UUID:
    action = await store.get_action(action_id)
    blob = action.parameters["_code_change"]
    return UUID(blob["correlation_id"])


# ---------------------------------------------------------------------------
# EXECUTED path — propose → approve → apply → PR → ONE clean chain (N=3)
# ---------------------------------------------------------------------------


async def test_executed_run_is_one_clean_three_event_chain(
    repo, store, allowlist, journal, graph, monkeypatch
):
    """The whole executed station run lands as ONE chain of EXACTLY three
    events sharing one correlation_id minted at propose:
    agent.proposed → human.corrected(accepted) → decision.completed(landed).
    No doubled terminal. The Decision row is queryable via the graph."""
    _patch_pr_opener(monkeypatch)
    user = _FakeUser()
    client = _make_client(store, user, monkeypatch)

    action_id = await _propose(repo, _good_diff())
    corr = await _correlation_for(store, action_id)

    # agent.proposed fired at propose, before any approval.
    proposed = _events(journal, "agent.proposed")
    assert len(proposed) == 1
    assert proposed[0].correlation_id == corr
    assert proposed[0].causation_id is None  # chain origin
    assert proposed[0].payload["action"] == "code_change"

    # Approve over HTTP — the REAL router dispatch fires human.corrected then
    # the REAL executor applies + closes the chain.
    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 200, resp.text

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED, final.outcome

    # EXACTLY three events, one chain, in causal order.
    chain = _chain(journal, corr)
    actions = [e.action for e in chain]
    assert actions == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], actions

    proposed_e, human_e, completed_e = chain
    # Causation walk: each event cites the prior.
    assert proposed_e.causation_id is None
    assert human_e.causation_id == proposed_e.id
    assert completed_e.causation_id == human_e.id

    # human.corrected — accepted, the human user is the actor.
    assert human_e.payload["disposition"] == "accepted"
    assert human_e.actor.kind == "user"
    assert human_e.actor.id == f"user:{USER}"

    # decision.completed — landed, carries the PR url + branch + file count.
    assert completed_e.payload["passed"] is True
    assert completed_e.payload["action_outcome"] == "landed"
    assert "github.com/acme/repo/pull/1" in completed_e.payload["pr_url"]
    assert completed_e.payload["branch"].startswith("feat/belt-")
    assert completed_e.payload["files_changed"] == 1

    # EXACTLY ONE terminal — the doubled-terminal trap.
    assert len(_events(journal, "decision.completed")) == 1
    assert len(_events(journal, "agent.proposed")) == 1
    assert len(_events(journal, "human.corrected")) == 1

    # The folded Decision row is queryable and not rejected.
    assert graph.store.count() == 1
    decisions = await graph.find()
    assert len(decisions) == 1
    d = decisions[0]
    assert d.correlation_id == corr
    assert d.action == "code_change"
    assert len(d.approvers) == 1
    assert d.approvers[0].actor.id == f"user:{USER}"
    assert d.outcome is None or d.outcome.status != "rejected"


# ---------------------------------------------------------------------------
# TWO cycles — back-to-back runs form TWO distinct clean chains
# ---------------------------------------------------------------------------


async def test_two_cycles_form_two_distinct_clean_chains(
    repo, store, allowlist, journal, graph, monkeypatch
):
    """A second full executed cycle produces a SECOND distinct chain — two
    correlation_ids, each a clean 3-event chain, no cross-contamination and no
    doubled terminals across the two runs."""
    _patch_pr_opener(monkeypatch)
    user = _FakeUser()
    client = _make_client(store, user, monkeypatch)

    # Cycle 1
    id_a = await _propose(repo, _good_diff(), summary="First change.")
    corr_a = await _correlation_for(store, id_a)
    assert client.post(f"/instinct/actions/{id_a}/approve").status_code == 200

    # Cycle 2 (a distinct diff value so the two branches differ)
    diff_b = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'hi'\n"
        "+    return 'second'\n"
    )
    id_b = await _propose(repo, diff_b, summary="Second change.")
    corr_b = await _correlation_for(store, id_b)
    assert client.post(f"/instinct/actions/{id_b}/approve").status_code == 200

    assert corr_a != corr_b

    # Each chain is its own clean 3-event walk.
    for corr in (corr_a, corr_b):
        actions = [e.action for e in _chain(journal, corr)]
        assert actions == [
            "agent.proposed",
            "human.corrected",
            "decision.completed",
        ], (corr, actions)

    # Two terminals total — one per run, none doubled.
    assert len(_events(journal, "agent.proposed")) == 2
    assert len(_events(journal, "human.corrected")) == 2
    assert len(_events(journal, "decision.completed")) == 2

    # Two distinct Decision rows.
    assert graph.store.count() == 2
    decisions = await graph.find()
    corrs = {d.correlation_id for d in decisions}
    assert corrs == {corr_a, corr_b}


# ---------------------------------------------------------------------------
# REJECT path — propose → reject → ONE clean chain (N=3), router owns close
# ---------------------------------------------------------------------------


async def test_rejected_run_is_one_clean_three_event_chain(
    repo, store, allowlist, journal, graph, monkeypatch
):
    """A rejected station run lands as ONE clean chain:
    agent.proposed → human.corrected(rejected) → decision.completed(rejected,
    reason=<comment>). The executor never runs (no PR opener call), the router
    owns the close, and there is exactly one terminal."""
    user = _FakeUser()
    client = _make_client(store, user, monkeypatch)
    FakePrOpener.instances.clear()

    action_id = await _propose(repo, _good_diff(), summary="A change to reject.")
    corr = await _correlation_for(store, action_id)

    resp = client.post(
        f"/instinct/actions/{action_id}/reject",
        json={"reason": "not now — out of scope"},
    )
    assert resp.status_code == 200, resp.text

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.REJECTED

    chain = _chain(journal, corr)
    actions = [e.action for e in chain]
    assert actions == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], actions

    proposed_e, human_e, completed_e = chain
    assert human_e.causation_id == proposed_e.id
    assert completed_e.causation_id == human_e.id

    assert human_e.payload["disposition"] == "rejected"
    assert human_e.payload["note"] == "not now — out of scope"

    assert completed_e.payload["passed"] is False
    assert completed_e.payload["action_outcome"] == "rejected"
    assert completed_e.payload["reason"] == "not now — out of scope"

    # Exactly one terminal; the executor never ran (no PR opener instantiated).
    assert len(_events(journal, "decision.completed")) == 1
    assert FakePrOpener.instances == []

    # The folded Decision row is marked rejected.
    decisions = await graph.find()
    assert len(decisions) == 1
    assert decisions[0].outcome is not None
    assert decisions[0].outcome.status == "rejected"


# ---------------------------------------------------------------------------
# FAILED path — propose → approve → apply conflict → ONE chain (failed terminal)
# ---------------------------------------------------------------------------


async def test_failed_apply_is_one_clean_three_event_chain(
    repo, store, allowlist, journal, graph, monkeypatch
):
    """An approved run whose diff cannot apply closes the chain with a single
    failed terminal: agent.proposed → human.corrected(accepted) →
    decision.completed(passed=False, action_outcome="failed",
    error_class="ApplyConflict"). No PR opened, no doubled terminal."""
    _patch_pr_opener(monkeypatch)
    user = _FakeUser()
    client = _make_client(store, user, monkeypatch)
    FakePrOpener.instances.clear()

    action_id = await _propose(repo, _conflict_diff(), summary="A conflicting change.")
    corr = await _correlation_for(store, action_id)

    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 200, resp.text

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.FAILED

    chain = _chain(journal, corr)
    actions = [e.action for e in chain]
    assert actions == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], actions

    proposed_e, human_e, completed_e = chain
    assert human_e.causation_id == proposed_e.id
    assert completed_e.causation_id == human_e.id

    assert human_e.payload["disposition"] == "accepted"
    assert completed_e.payload["passed"] is False
    assert completed_e.payload["action_outcome"] == "failed"
    assert completed_e.payload["error_class"] == "ApplyConflict"

    # Exactly one terminal; the apply conflict means the PR opener was never
    # reached (the executor bails before the push/PR step).
    assert len(_events(journal, "decision.completed")) == 1
    assert FakePrOpener.instances == [] or FakePrOpener.instances[0].calls == []


# ---------------------------------------------------------------------------
# bulk-approve — code_change item lands as one clean chain too
# ---------------------------------------------------------------------------


async def test_bulk_approve_code_change_is_one_clean_chain(
    repo, store, allowlist, journal, graph, monkeypatch
):
    """A code_change Action approved via the BULK endpoint forms the same
    clean 3-event chain — bulk-approve has no edit surface, so disposition is
    accepted, and the executor still owns a single landed terminal."""
    _patch_pr_opener(monkeypatch)
    user = _FakeUser()
    client = _make_client(store, user, monkeypatch)

    action_id = await _propose(repo, _good_diff(), summary="Bulk change.")
    corr = await _correlation_for(store, action_id)

    resp = client.post(
        "/instinct/actions/bulk-approve",
        json={"ids": [action_id], "note": "ship it"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["affected"]) == 1

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED, final.outcome

    chain = _chain(journal, corr)
    actions = [e.action for e in chain]
    assert actions == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], actions
    assert chain[1].payload["disposition"] == "accepted"
    assert chain[1].payload.get("note") == "ship it"
    assert chain[2].payload["action_outcome"] == "landed"
    assert len(_events(journal, "decision.completed")) == 1
