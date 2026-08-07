# tests/cloud/test_belt_foreman_agent.py — the mandate FOREMAN's agent identity.
#
# Created: 2026-08-07 (feat/coupling-t17-foreman-agent).
#
# What T-17 changed and what each test here holds down:
#   * a mandate NAMES the agent its foreman runs as (``MandateDoc.agent_id``),
#     defaulting to the workspace's seeded ``pocketpaw`` agent;
#   * the foreman's soul is that agent's soul, at the AgentPool convention
#     ``~/.pocketpaw/souls/{workspace}/{slug}.soul`` — no more free-form path;
#   * the planning prompt inherits the agent's system_prompt + scopes;
#   * the Decision-Graph terminal is attributed ``agent:<id>`` instead of an
#     "agent" actor wearing a USER id;
#   * and NONE of it may break a running shift.
#
# THE SHAPE OF THESE TESTS: the identity tests drive the REAL production path
# over HTTP (real mandates router, real service, real foreman pipeline with the
# mock LLM transport, real Instinct store, real instinct-router approve, real
# plan executor, real journal + projection singletons) — the same discipline
# test_belt_mandates.py's gate test uses. The only fakes are the two genuine
# external boundaries: the LLM transport and the develop-station dispatcher.
#
# ISOLATION NOTE (do not remove ``soul_home``): ``agent_soul_path`` resolves
# through ``pocketpaw.config.get_config_dir``, which is ``Path.home() /
# ".pocketpaw"`` and is NOT covered by conftest's ``local_store_home`` guard.
# Without the redirect these tests would materialize real ``.soul`` files into
# the developer's live PocketPaw data directory — the exact hazard
# ``local_store_home`` exists to prevent.

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
from pocketpaw_ee.cloud.mandates import (
    foreman,  # noqa: E402
    soul_link,  # noqa: E402
)
from pocketpaw_ee.cloud.mandates.domain import MandateDoc  # noqa: E402
from pocketpaw_ee.cloud.mandates.router import router as mandates_router  # noqa: E402
from pocketpaw_ee.instinct.router import router as instinct_router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "w1"
USER = "u1"
OTHER_USER = "u2"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def soul_home(tmp_path, monkeypatch) -> Path:
    """Redirect ``get_config_dir`` so agent souls land in tmp, never in ``~``.

    ``soul_link.agent_soul_path`` imports ``get_config_dir`` lazily from
    ``pocketpaw.config``, so patching the module attribute is what takes
    effect."""
    import pocketpaw.config as pp_config

    home = tmp_path / "pocketpaw-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pp_config, "get_config_dir", lambda: home)
    return home


@pytest.fixture
def journal(tmp_path: Path):
    """Fresh on-disk journal wired into the lazy ``get_journal`` lookup."""
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
    st = InstinctStore(tmp_path / "instinct_foreman_agent.db")
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


class CapturingMockLlm(foreman.MockLlm):
    """The deterministic mock, plus a record of the prompt it was handed.

    Subclasses rather than replaces ``MockLlm`` so the planning behaviour the
    other tests depend on is unchanged — we only need to SEE the prompt."""

    prompts: list[str] = []

    async def plan(self, *, prompt: str, context: foreman.ForemanContext) -> str:
        CapturingMockLlm.prompts.append(prompt)
        return await super().plan(prompt=prompt, context=context)


@pytest.fixture
def captured_prompts(monkeypatch) -> list[str]:
    """Capture the real prompt ``build_prompt`` produced on the shift path."""
    CapturingMockLlm.prompts = []
    monkeypatch.setattr(foreman, "MockLlm", CapturingMockLlm)
    return CapturingMockLlm.prompts


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


def _charter(budget: int = 3) -> dict:
    return {
        "goal": "keep dependencies fresh and CVE-free",
        "kpis": [{"name": "open_cves", "target": 0, "direction": "down"}],
        "says_no": ["major version bumps"],
        "boundaries": ["never touch auth code"],
        "budget": {"max_tasks_per_shift": budget, "gate_minutes_per_week": 15},
        "cadence": "manual",
    }


def _create_mandate(client: TestClient, repo_dir: Path, **extra) -> dict:
    body = {
        "name": "deps freshness",
        "surface": {"repo_id": str(repo_dir)},
        "charter": _charter(),
    }
    body.update(extra)
    return client.post("/belt/mandates", json=body)


async def _seed_default_agent() -> str:
    """Seed the workspace's default ``pocketpaw`` agent, as workspace-create
    does. Returns its id."""
    doc, _created = await agents_service.seed_default_agent(WS, USER)
    assert doc is not None
    return str(doc.id)


def _events(journal, action: str) -> list:
    return [e for e in journal.replay_from(0) if e.action == action]


def _chain(journal, correlation_id: UUID) -> list:
    return [e for e in journal.replay_from(0) if e.correlation_id == correlation_id]


async def _seed_sighting(client: TestClient, mandate_id: str, text: str) -> None:
    res = client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": text, "severity": 4, "source": "support"},
    )
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# 1. The mandate carries an agent identity
# ---------------------------------------------------------------------------


async def test_new_mandate_binds_the_workspace_default_agent(tmp_path, mongo_db, monkeypatch):
    """A mandate created with no ``agent_id`` inherits the workspace's seeded
    default ``pocketpaw`` agent — the foreman gets a real identity without the
    user having to pick one.

    MUTATION THAT BREAKS THIS: drop the ``_default_foreman_agent_id`` fallback
    in ``create_mandate`` (bind ``None`` instead) — ``agent_id`` comes back
    ``None`` and both asserts fail."""
    agent_id = await _seed_default_agent()
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    res = _create_mandate(client, repo)
    assert res.status_code == 200, res.text
    mandate = res.json()["mandate"]

    # The response names the resolved agent...
    assert mandate["agent_id"] == agent_id
    assert mandate["agent_name"] == "PocketPaw"

    # ...and it is PERSISTED, not merely computed on the read path.
    doc = await MandateDoc.get(mandate["id"])
    assert doc is not None
    assert doc.agent_id == agent_id


async def test_pre_t17_mandate_resolves_to_the_default_agent_on_read(
    tmp_path, mongo_db, monkeypatch
):
    """A mandate row written BEFORE this change stores no ``agent_id`` at all.
    The detail read must still name the agent the shift will actually use —
    otherwise the console shows "no agent" for every existing mandate.

    This is the zero-migration claim, tested rather than asserted: the doc is
    inserted with ``agent_id=None`` exactly as an old row deserializes."""
    agent_id = await _seed_default_agent()
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    legacy = MandateDoc(
        workspace=WS,
        name="legacy mandate",
        surface={"repo_id": str(repo)},
        charter={"goal": "old goal"},
        agent_id=None,
    )
    await legacy.insert()

    res = client.get(f"/belt/mandates/{legacy.id}")
    assert res.status_code == 200, res.text
    assert res.json()["agent_id"] == agent_id


async def test_explicit_agent_id_is_permission_gated(tmp_path, mongo_db, monkeypatch):
    """Binding an explicit agent goes through ``ensure_can_use`` — another
    user's PRIVATE agent is a 404, never a bind. A leaked agent id must not
    become a mandate's judgment seat.

    MUTATION THAT BREAKS THIS: drop the ``ensure_can_use`` call in
    ``create_mandate`` — the private agent binds and the 404 assert fails."""
    await _seed_default_agent()
    from pocketpaw_ee.cloud.models.agent import Agent as AgentDoc

    private = AgentDoc(
        workspace=WS,
        name="Someone else's Ops",
        slug="ops-private",
        owner=OTHER_USER,
        visibility="private",
    )
    await private.insert()

    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    res = _create_mandate(client, repo, agent_id=str(private.id))
    assert res.status_code == 404, res.text

    # A workspace-visible agent DOES bind.
    shared = AgentDoc(
        workspace=WS,
        name="Ops",
        slug="ops",
        owner=OTHER_USER,
        visibility="workspace",
    )
    await shared.insert()
    res = _create_mandate(client, repo, agent_id=str(shared.id))
    assert res.status_code == 200, res.text
    assert res.json()["mandate"]["agent_id"] == str(shared.id)
    assert res.json()["mandate"]["agent_name"] == "Ops"


# ---------------------------------------------------------------------------
# 2. The soul comes from the agent, by the pool's convention
# ---------------------------------------------------------------------------


def test_agent_soul_path_matches_the_agent_pool_convention(soul_home):
    """The foreman must write to the SAME file the AgentPool writes when the
    agent chats, or "the agent's memory" is two disconnected stores.

    Pinned against the formula in ``src/pocketpaw/agents/pool.py::_init_soul``:
    ``get_config_dir() / "souls" / agent_doc.workspace / f"{agent_doc.slug}.soul"``.
    """
    assert soul_link.agent_soul_path(WS, "pocketpaw") == str(
        soul_home / "souls" / WS / "pocketpaw.soul"
    )


async def test_legacy_soul_path_still_wins_over_the_agent_soul(soul_home):
    """THE COMPAT STORY. A mandate created before this change bound a free-form
    ``soul_path`` and has real memories in that file. It keeps winning, verbatim,
    even though an agent is now bound — nothing is migrated or moved."""
    agent = soul_link.ForemanAgent(
        id="a1", workspace_id=WS, name="Ops", slug="ops", system_prompt="be careful"
    )
    resolved = await soul_link.resolve_soul_path(
        workspace_id=WS,
        agent=agent,
        legacy_soul_path="/tmp/legacy-mandate.soul",
    )
    assert resolved == "/tmp/legacy-mandate.soul"

    # With no legacy path it falls through to the agent's soul.
    resolved = await soul_link.resolve_soul_path(workspace_id=WS, agent=agent)
    assert resolved == str(soul_home / "souls" / WS / "ops.soul")


async def test_missing_agent_soul_is_materialized_on_the_write_path(soul_home, monkeypatch):
    """``seed_default_agent`` inserts its doc directly and never ran the
    eager-soul step, so the default agent has a doc but NO soul file. Without
    materialization every agent-resolved shift memory would silently no-op
    forever — the feature would look wired and store nothing.

    Asserts materialization is requested when the file is absent and NOT
    requested when it already exists (no pointless re-persist per shift)."""
    calls: list[str] = []

    async def _spy(agent_id: str) -> bool:
        calls.append(agent_id)
        return True

    monkeypatch.setattr(agents_service, "ensure_soul_materialized", _spy)
    agent = soul_link.ForemanAgent(id="a1", workspace_id=WS, name="Ops", slug="ops")

    # File absent → materialize.
    path = await soul_link.resolve_soul_path(workspace_id=WS, agent=agent, materialize=True)
    assert calls == ["a1"]

    # File present → no second materialization.
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("")
    await soul_link.resolve_soul_path(workspace_id=WS, agent=agent, materialize=True)
    assert calls == ["a1"]


async def test_agent_soul_round_trips_for_real(mongo_db, soul_home):
    """THE END-TO-END SOUL CLAIM, with NO spy anywhere.

    Every other test in this file would stay green if the soul write silently
    no-op'd: ``remember_shift`` is best-effort by design, so a failure logs a
    warning and returns False while the shift still succeeds. That is exactly
    the silent-no-op failure mode the materialization fix exists to prevent, so
    it has to be proven directly.

    It also crosses a seam nothing else checks: the file is CREATED by the
    AgentPool (``SoulManager.initialize`` → ``shutdown``) but READ AND WRITTEN
    by ``soul_link`` (``Soul.awaken`` → ``remember`` → ``save_local``). Two
    different entry points into soul-protocol — if they don't round-trip, the
    foreman's memory goes nowhere and every other test still passes."""
    agent_id = await _seed_default_agent()

    # 1. The seeded default agent starts with NO soul file — the gap this fix
    #    exists to close.
    expected = Path(soul_link.agent_soul_path(WS, "pocketpaw"))
    assert not expected.exists()

    # 2. Real materialization (no spy) creates it at the pool convention.
    created = await agents_service.ensure_soul_materialized(agent_id)
    assert created is True
    assert expected.exists(), "the pool did not write the soul where soul_link looks"

    # 3. A shift memory actually LANDS — the return value is the honest signal,
    #    since a failure would degrade silently.
    wrote = await soul_link.remember_shift(
        str(expected), "Mandate shift 1: dispatched 2 task(s) as belt runs"
    )
    assert wrote is True, "the foreman's shift memory did not persist"

    # 4. ...and comes back out, so the next shift's foreman can cite it.
    recalled = await soul_link.recall_for_planning(str(expected), "mandate shift dispatched")
    assert any("dispatched 2 task(s)" in line for line in recalled), recalled


async def test_legacy_directory_soul_still_round_trips(mongo_db, soul_home, tmp_path):
    """The OTHER writer branch. A pre-T-17 mandate could bind a free-form
    ``.soul/`` PROJECT DIRECTORY, where the directory IS the soul. That shape
    needs ``save_local``, not ``export`` — so both branches are gated and a
    "just always use export" simplification cannot slip through."""
    agent_id = await _seed_default_agent()
    await agents_service.ensure_soul_materialized(agent_id)
    archive = Path(soul_link.agent_soul_path(WS, "pocketpaw"))

    # Build a DIRECTORY-shaped soul from the archive.
    from soul_protocol import Soul

    soul_dir = tmp_path / "legacy-mandate.soul"
    soul = await Soul.awaken(archive)
    await soul.save_local(soul_dir)
    assert soul_dir.is_dir()

    wrote = await soul_link.remember_shift(str(soul_dir), "Mandate shift 7: stood down")
    assert wrote is True, "a legacy directory-shaped soul stopped accepting writes"
    assert soul_dir.is_dir(), "the directory soul was clobbered into an archive"

    recalled = await soul_link.recall_for_planning(str(soul_dir), "mandate shift stood down")
    assert any("stood down" in line for line in recalled), recalled


async def test_soul_disabled_agent_binds_no_soul(soul_home):
    """An agent with ``soul_enabled=False`` gets no soul file — the foreman
    honours the agent's own config rather than forcing a soul on it."""
    agent = soul_link.ForemanAgent(
        id="a1", workspace_id=WS, name="Ops", slug="ops", soul_enabled=False
    )
    assert await soul_link.resolve_soul_path(workspace_id=WS, agent=agent) is None


# ---------------------------------------------------------------------------
# 3. The planning prompt inherits the agent's system_prompt + scopes
# ---------------------------------------------------------------------------


def test_prompt_inherits_system_prompt_and_scopes():
    """The foreman plans AS the agent: its standing instructions and its
    assigned scopes ride in the prompt."""
    ctx = foreman.ForemanContext(
        shift_no=1,
        charter={"goal": "g", "boundaries": ["never touch auth code"], "budget": {}},
        agent_name="Ops",
        agent_system_prompt="You are the Ops agent. Prefer reversible changes.",
        agent_scopes=["org:platform:*", "repo:api"],
    )
    prompt = foreman.build_prompt(ctx)

    assert "== WHO YOU ARE (agent: Ops) ==" in prompt
    assert "You are the Ops agent. Prefer reversible changes." in prompt
    assert "org:platform:*" in prompt
    assert "repo:api" in prompt

    # BOUNDARIES stay ahead of the inherited identity — the sim-validated rule
    # is that boundaries are stated FIRST and override everything, and an
    # identity block must not displace them.
    assert prompt.index("== BOUNDARIES") < prompt.index("== WHO YOU ARE")
    assert prompt.index("== WHO YOU ARE") < prompt.index("== CHARTER")


def test_prompt_is_unchanged_for_an_agentless_mandate():
    """ADDITIVE-ONLY GUARANTEE. With no agent bound the prompt is byte-identical
    to the pre-T-17 prompt, so a mandate that never got an identity is not
    silently re-tuned by this change."""
    ctx = foreman.ForemanContext(
        shift_no=1,
        charter={"goal": "g", "boundaries": [], "budget": {}},
    )
    prompt = foreman.build_prompt(ctx)

    assert "WHO YOU ARE" not in prompt
    # The boundaries block runs straight into the charter block, exactly as it
    # did before the agent block was inserted between them.
    assert "that is correct behavior.\n\n== CHARTER (verbatim) ==" in prompt


async def test_shift_threads_the_agent_identity_into_the_real_prompt(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph, captured_prompts
):
    """END-TO-END: the identity reaches the prompt over the REAL shift path, not
    just in a hand-built context. Guards the service→foreman wiring, which a
    unit test of ``build_prompt`` alone cannot see."""
    from pocketpaw_ee.cloud.models.agent import Agent as AgentDoc
    from pocketpaw_ee.cloud.models.agent import AgentConfig

    agent = AgentDoc(
        workspace=WS,
        name="Ops",
        slug="ops",
        owner=USER,
        visibility="workspace",
        config=AgentConfig(
            system_prompt="You are the Ops agent. Prefer reversible changes.",
            scopes=["org:platform:*"],
        ),
    )
    await agent.insert()

    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    res = _create_mandate(client, repo, agent_id=str(agent.id))
    assert res.status_code == 200, res.text
    mandate_id = res.json()["mandate"]["id"]

    await _seed_sighting(client, mandate_id, "lodash CVE flagged by a customer")

    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text

    assert captured_prompts, "the foreman was never called"
    prompt = captured_prompts[-1]
    assert "== WHO YOU ARE (agent: Ops) ==" in prompt
    assert "Prefer reversible changes." in prompt
    assert "org:platform:*" in prompt


# ---------------------------------------------------------------------------
# 4. The journal terminal is attributed to the AGENT
# ---------------------------------------------------------------------------


async def test_chain_terminal_is_attributed_to_the_agent_not_the_user(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph, dispatcher, recording_bus
):
    """THE ATTRIBUTION FIX. The ``decision.completed`` terminal a shift produces
    must name the FOREMAN'S AGENT.

    Before T-17 it was ``Actor(kind="agent", id="user:<user_id>")`` — an "agent"
    actor carrying a USER id — so every agent's terminals were filed under
    whichever human approved them and a per-agent track record could not be
    assembled from the journal at all.

    MUTATION THAT BREAKS THIS: revert the actor id in
    ``mandates/executor._emit_chain_close`` to ``f"user:{user_id}"`` — the
    actor id assert fails."""
    agent_id = await _seed_default_agent()
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    res = _create_mandate(client, repo)
    mandate_id = res.json()["mandate"]["id"]
    await _seed_sighting(client, mandate_id, "lodash CVE flagged by a customer")

    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text
    plan_action_id = res.json()["shift"]["plan_action_id"]
    assert plan_action_id

    # Approve over the REAL instinct router → the REAL plan executor runs.
    res = client.post(f"/instinct/actions/{plan_action_id}/approve", json={})
    assert res.status_code == 200, res.text

    completed = _events(journal, "decision.completed")
    assert completed, "the chain never closed"
    terminal = completed[-1]
    assert terminal.actor.kind == "agent"
    assert terminal.actor.id == f"agent:{agent_id}"
    # The old shape must be gone, not merely accompanied.
    assert terminal.actor.id != f"user:{USER}"

    # Still EXACTLY ONE terminal — the attribution change must not disturb the
    # chain-doubling discipline the gate test protects.
    assert len(_chain(journal, terminal.correlation_id)) == len(
        {e.id for e in _chain(journal, terminal.correlation_id)}
    )
    assert (
        len(
            [
                e
                for e in _chain(journal, terminal.correlation_id)
                if e.action == "decision.completed"
            ]
        )
        == 1
    )


async def test_agent_id_rides_onto_the_queued_station_run(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph, recording_bus
):
    """The BELT executor's terminal can only name the agent if the agent rode
    onto the ``code_change`` blob when the task was queued. This drives the REAL
    ``StationTaskDispatcher`` (not the recorder) and asserts the handoff.

    Without this the belt-executor half of the attribution fix would be dead
    code: the read side would be correct and nothing would ever populate it.

    MUTATION THAT BREAKS THIS: drop ``"agent_id"`` from the StationTaskDispatcher
    blob, or stop injecting it into ``task_payload`` in ``execute_approved_plan``.
    """
    agent_id = await _seed_default_agent()
    # The REAL station dispatcher — the default, pinned explicitly here.
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "station")

    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    res = _create_mandate(client, repo)
    mandate_id = res.json()["mandate"]["id"]
    await _seed_sighting(client, mandate_id, "lodash CVE flagged by a customer")

    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    plan_action_id = res.json()["shift"]["plan_action_id"]
    res = client.post(f"/instinct/actions/{plan_action_id}/approve", json={})
    assert res.status_code == 200, res.text

    # Find the queued station run the dispatcher filed and read its blob.
    actions = await store.list_actions()
    queued = [
        a
        for a in actions
        if isinstance(a.parameters, dict) and "_code_change" in (a.parameters or {})
    ]
    assert queued, "the station dispatcher filed no code_change run"
    blob = queued[-1].parameters["_code_change"]
    assert blob["station_pending"] is True
    assert blob["agent_id"] == agent_id


async def test_legacy_plan_blob_without_an_agent_id_still_dispatches(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph, dispatcher, recording_bus
):
    """A plan PROPOSED before this deploy and APPROVED after it carries no
    ``agent_id`` on its blob. It must still dispatch — which is why
    ``BELT_PLAN_SCHEMA`` is deliberately NOT bumped — and it keeps the honest
    legacy attribution rather than inventing an agent.

    MUTATION THAT BREAKS THIS: bump BELT_PLAN_SCHEMA, or make the executor read
    ``blob["agent_id"]`` strictly — the dispatch fails and the run_refs assert
    goes empty."""
    await _seed_default_agent()
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    res = _create_mandate(client, repo)
    mandate_id = res.json()["mandate"]["id"]
    await _seed_sighting(client, mandate_id, "lodash CVE flagged by a customer")
    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    plan_action_id = res.json()["shift"]["plan_action_id"]

    # Strip the key to reproduce a pre-T-17 blob exactly.
    action = await store.get_action(plan_action_id)
    blob = dict(action.parameters["_belt_plan"])
    blob.pop("agent_id", None)
    assert "agent_id" not in blob
    action.parameters["_belt_plan"] = blob
    await mandate_executor.execute_approved_plan(action, dispatcher=RecorderDispatcher())

    completed = _events(journal, "decision.completed")
    assert completed, "the legacy blob never dispatched"
    # No agent to name → the honest legacy attribution, not a fabricated one.
    assert completed[-1].actor.id == f"user:{USER}"


# ---------------------------------------------------------------------------
# 5. Identity must never break a running shift
# ---------------------------------------------------------------------------


async def test_shift_runs_when_the_workspace_has_no_default_agent(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph
):
    """A workspace whose agent seed never ran has NO default ``pocketpaw``
    agent. Creating a mandate and running a shift must both still work — an
    identity gap degrades the mandate, it does not disable it.

    MUTATION THAT BREAKS THIS: make ``_default_foreman_agent_id`` raise instead
    of returning None on a miss — mandate creation 500s."""
    # deliberately NO _seed_default_agent()
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    res = _create_mandate(client, repo)
    assert res.status_code == 200, res.text
    assert res.json()["mandate"]["agent_id"] is None
    mandate_id = res.json()["mandate"]["id"]

    await _seed_sighting(client, mandate_id, "lodash CVE flagged by a customer")
    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text
    assert res.json()["shift"]["state"] == "in_gate"


async def test_deleted_agent_falls_back_to_the_workspace_default(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph, captured_prompts
):
    """A mandate bound to an agent that is later DELETED must keep shifting. The
    resolver falls back to the workspace default rather than leaving the
    mandate stranded.

    MUTATION THAT BREAKS THIS: remove the fallback in ``resolve_foreman_agent``
    (return None when the bound agent is missing) — the prompt loses the default
    agent's inherited system_prompt and the assert fails."""
    default_id = await _seed_default_agent()
    from pocketpaw_ee.cloud.models.agent import Agent as AgentDoc
    from pocketpaw_ee.cloud.models.agent import AgentConfig

    doomed = AgentDoc(
        workspace=WS,
        name="Doomed",
        slug="doomed",
        owner=USER,
        visibility="workspace",
        config=AgentConfig(system_prompt="I will not survive"),
    )
    await doomed.insert()

    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    res = _create_mandate(client, repo, agent_id=str(doomed.id))
    mandate_id = res.json()["mandate"]["id"]

    # The agent is deleted out from under the mandate.
    await doomed.delete()

    await _seed_sighting(client, mandate_id, "lodash CVE flagged by a customer")
    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text

    # It fell back to the workspace default, not to nothing.
    detail = client.get(f"/belt/mandates/{mandate_id}").json()
    assert detail["agent_id"] == default_id
    assert "I will not survive" not in captured_prompts[-1]
    assert "default assistant in this workspace" in captured_prompts[-1]


async def test_disabled_agent_does_not_hold_the_judgment_seat(mongo_db, soul_home):
    """A soft-disabled agent (AW-4 revokes it EVERYWHERE) must not keep running
    a mandate's foreman. It falls back to the workspace default."""
    default_id = await _seed_default_agent()
    from pocketpaw_ee.cloud.models.agent import Agent as AgentDoc

    revoked = AgentDoc(
        workspace=WS,
        name="Revoked",
        slug="revoked",
        owner=USER,
        visibility="workspace",
        disabled=True,
    )
    await revoked.insert()

    resolved = await soul_link.resolve_foreman_agent(WS, str(revoked.id))
    assert resolved is not None
    assert resolved.id == default_id


async def test_cross_tenant_agent_never_binds(mongo_db, soul_home):
    """An agent id belonging to ANOTHER workspace must never become this
    mandate's foreman, even if it is somehow stored on the doc."""
    default_id = await _seed_default_agent()
    from pocketpaw_ee.cloud.models.agent import Agent as AgentDoc

    foreign = AgentDoc(
        workspace="other-workspace",
        name="Foreign",
        slug="foreign",
        owner=OTHER_USER,
        visibility="workspace",
    )
    await foreign.insert()

    resolved = await soul_link.resolve_foreman_agent(WS, str(foreign.id))
    assert resolved is not None
    assert resolved.id == default_id
    assert resolved.workspace_id == WS


async def test_identity_failure_never_wedges_the_shift(
    tmp_path, mongo_db, monkeypatch, soul_home, store, journal, graph
):
    """BELT AND BRACES. Even a hard failure inside the identity path — a bad
    import, an unexpected raise from the agents service — must cost the mandate
    its inherited identity and NOTHING ELSE. The shift is the valuable thing.

    MUTATION THAT BREAKS THIS: remove the try/except around the identity block
    in ``trigger_shift`` — the shift 500s instead of degrading."""
    await _seed_default_agent()
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    res = _create_mandate(client, repo)
    mandate_id = res.json()["mandate"]["id"]
    await _seed_sighting(client, mandate_id, "lodash CVE flagged by a customer")

    async def _explode(*_a, **_k):
        raise RuntimeError("identity subsystem is down")

    monkeypatch.setattr(soul_link, "resolve_foreman_agent", _explode)

    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text
    assert res.json()["shift"]["state"] == "in_gate"
