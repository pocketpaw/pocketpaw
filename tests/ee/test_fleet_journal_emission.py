# tests/ee/test_fleet_journal_emission.py — Verify fleet installer emits
# correlated journal events (fleet.install.started, agent.spawned per soul,
# fleet.installed summary) when a Journal is passed; stays silent when it
# isn't; and suppresses the terminal fleet.installed event on partial
# install so projections never see a completion marker without the work.
# Created: 2026-04-16 (feat/fleet-journal-emission).

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from soul_protocol.engine.journal import open_journal
from soul_protocol.spec.journal import Actor

from ee.fleet import FleetConnector, FleetTemplate, install_fleet

# ---------------------------------------------------------------------------
# Fixtures — parallel the existing test_fleet_installer.py shape so both
# suites exercise install_fleet through the same factory contract.
# ---------------------------------------------------------------------------


def _basic_fleet(**overrides) -> FleetTemplate:
    defaults = {
        "name": "sales-fleet",
        "soul_template": "arrow",
        "pocket_name": "Pipeline",
        "pocket_description": "Live pipeline",
        "scopes": ["org:sales:*"],
    }
    defaults.update(overrides)
    return FleetTemplate(**defaults)


def _fake_factory(*, soul_did: str = "did:soul:fake-1", template_name: str = "Arrow"):
    factory = MagicMock()

    template = MagicMock()
    template.name = template_name
    factory.load_bundled = MagicMock(return_value=template)

    soul = MagicMock()
    soul.did = soul_did
    soul.name = template_name
    factory.from_template = AsyncMock(return_value=soul)
    return factory, soul


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def fake_pocket_creator():
    pocket = MagicMock()
    pocket.id = "pocket_fake_1"
    return AsyncMock(return_value=pocket)


@pytest.fixture
def fake_registry():
    registry = MagicMock()
    registry.has = MagicMock(return_value=True)
    registry.connect = AsyncMock(return_value=True)
    return registry


# ---------------------------------------------------------------------------
# Happy path — journal supplied, install succeeds end-to-end.
# ---------------------------------------------------------------------------


class TestEmissionHappyPath:
    @pytest.mark.asyncio
    async def test_emits_started_spawned_installed_in_order(
        self, journal, fake_pocket_creator, fake_registry
    ) -> None:
        factory, _ = _fake_factory()

        fleet = _basic_fleet(
            connectors=[FleetConnector(name="hubspot", config={"poll_minutes": 15})],
        )
        report = await install_fleet(
            fleet,
            soul_factory=factory,
            connector_registry=fake_registry,
            pocket_creator=fake_pocket_creator,
            journal=journal,
        )

        assert report.succeeded()

        events = journal.query(limit=100)
        actions = [e.action for e in events]
        assert actions == [
            "fleet.install.started",
            "agent.spawned",
            "fleet.installed",
        ]

    @pytest.mark.asyncio
    async def test_all_events_share_one_correlation_id(self, journal, fake_pocket_creator) -> None:
        factory, _ = _fake_factory()

        await install_fleet(
            _basic_fleet(),
            soul_factory=factory,
            pocket_creator=fake_pocket_creator,
            journal=journal,
        )

        events = journal.query(limit=100)
        corr_ids = {e.correlation_id for e in events}
        assert len(events) == 3
        assert len(corr_ids) == 1
        assert next(iter(corr_ids)) is not None

    @pytest.mark.asyncio
    async def test_events_carry_declared_fleet_scope(self, journal, fake_pocket_creator) -> None:
        factory, _ = _fake_factory()
        fleet = _basic_fleet(scopes=["org:sales:*", "team:ae"])

        await install_fleet(
            fleet,
            soul_factory=factory,
            pocket_creator=fake_pocket_creator,
            journal=journal,
        )

        events = journal.query(limit=100)
        for event in events:
            assert event.scope == ["org:sales:*", "team:ae"]

    @pytest.mark.asyncio
    async def test_agent_spawned_payload_has_canonical_fields(
        self, journal, fake_pocket_creator
    ) -> None:
        factory, soul = _fake_factory(soul_did="did:soul:arrow-42", template_name="Arrow")

        await install_fleet(
            _basic_fleet(),
            soul_factory=factory,
            pocket_creator=fake_pocket_creator,
            journal=journal,
        )

        spawned = [e for e in journal.query(limit=100) if e.action == "agent.spawned"]
        assert len(spawned) == 1
        payload = spawned[0].payload
        assert isinstance(payload, dict)
        assert payload["did"] == "did:soul:arrow-42"
        assert payload["soul_id"] == "did:soul:arrow-42"
        assert payload["archetype"] == "arrow"
        assert payload["fleet"] == "sales-fleet"
        assert payload["name"] == "Arrow"

    @pytest.mark.asyncio
    async def test_default_actor_is_system_fleet_installer(
        self, journal, fake_pocket_creator
    ) -> None:
        factory, _ = _fake_factory()

        await install_fleet(
            _basic_fleet(),
            soul_factory=factory,
            pocket_creator=fake_pocket_creator,
            journal=journal,
        )

        # SQLite backend persists actor kind + id (not scope_context — that
        # is a known backend-level projection, not a spec loss). Asserting
        # only the fields that round-trip keeps this test aligned with the
        # journal's storage contract.
        for event in journal.query(limit=100):
            assert event.actor.kind == "system"
            assert event.actor.id == "system:fleet-installer"

    @pytest.mark.asyncio
    async def test_explicit_root_actor_is_recorded_on_every_event(
        self, journal, fake_pocket_creator
    ) -> None:
        factory, _ = _fake_factory()
        root_actor = Actor(
            kind="root",
            id="did:soul:root-01",
            scope_context=["org:*"],
        )

        await install_fleet(
            _basic_fleet(),
            soul_factory=factory,
            pocket_creator=fake_pocket_creator,
            journal=journal,
            actor=root_actor,
        )

        for event in journal.query(limit=100):
            assert event.actor.kind == "root"
            assert event.actor.id == "did:soul:root-01"

    @pytest.mark.asyncio
    async def test_fleet_installed_payload_summarises_outcome(
        self, journal, fake_pocket_creator, fake_registry
    ) -> None:
        factory, _ = _fake_factory()
        fleet = _basic_fleet(
            connectors=[FleetConnector(name="hubspot")],
        )

        await install_fleet(
            fleet,
            soul_factory=factory,
            connector_registry=fake_registry,
            pocket_creator=fake_pocket_creator,
            journal=journal,
        )

        terminal = [e for e in journal.query(limit=100) if e.action == "fleet.installed"]
        assert len(terminal) == 1
        payload = terminal[0].payload
        assert payload["fleet"] == "sales-fleet"
        assert payload["soul_id"] == "did:soul:fake-1"
        assert payload["pocket_id"] == "pocket_fake_1"
        assert payload["succeeded"] is True
        assert payload["step_count"] >= 1
        assert payload["failed_steps"] == []


# ---------------------------------------------------------------------------
# Backward compatibility — no journal means no emission + no failure.
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    @pytest.mark.asyncio
    async def test_install_without_journal_still_works(self, fake_pocket_creator) -> None:
        factory, _ = _fake_factory()

        report = await install_fleet(
            _basic_fleet(),
            soul_factory=factory,
            pocket_creator=fake_pocket_creator,
            # journal omitted entirely
        )

        assert report.succeeded()
        assert report.soul_id == "did:soul:fake-1"

    @pytest.mark.asyncio
    async def test_install_with_journal_none_emits_nothing(
        self, tmp_path: Path, fake_pocket_creator
    ) -> None:
        # Open a second, unrelated journal and confirm the installer call
        # below does not touch it. This is the backward-compat guarantee
        # for callers that pass journal=None explicitly.
        unrelated = open_journal(tmp_path / "unrelated.db")
        try:
            factory, _ = _fake_factory()
            await install_fleet(
                _basic_fleet(),
                soul_factory=factory,
                pocket_creator=fake_pocket_creator,
                journal=None,
            )
            assert unrelated.query(limit=100) == []
        finally:
            unrelated.close()


# ---------------------------------------------------------------------------
# Partial install — soul step fails, no terminal fleet.installed event.
# ---------------------------------------------------------------------------


class TestPartialInstall:
    @pytest.mark.asyncio
    async def test_soul_failure_emits_started_only_no_terminal(self, journal) -> None:
        factory = MagicMock()
        factory.load_bundled = MagicMock(side_effect=FileNotFoundError("template missing"))

        report = await install_fleet(
            _basic_fleet(),
            soul_factory=factory,
            journal=journal,
        )

        assert not report.succeeded()
        events = journal.query(limit=100)
        actions = [e.action for e in events]
        assert actions == ["fleet.install.started"]
        # No agent.spawned, no fleet.installed — projections and UI
        # tailers should never see a completion marker for a run that
        # never produced a soul.
        assert "agent.spawned" not in actions
        assert "fleet.installed" not in actions

    @pytest.mark.asyncio
    async def test_connector_failure_still_emits_terminal_event(
        self, journal, fake_pocket_creator
    ) -> None:
        # Soul creation succeeded, so the install run did produce a soul.
        # A downstream connector failure shouldn't suppress the terminal
        # event — the caller already sees it in report.failed_steps and
        # the journal payload reflects it too.
        factory, _ = _fake_factory()
        registry = MagicMock()
        registry.has = MagicMock(return_value=True)
        registry.connect = AsyncMock(side_effect=RuntimeError("network down"))

        fleet = _basic_fleet(connectors=[FleetConnector(name="hubspot")])
        report = await install_fleet(
            fleet,
            soul_factory=factory,
            connector_registry=registry,
            pocket_creator=fake_pocket_creator,
            journal=journal,
        )

        assert not report.succeeded()
        events = journal.query(limit=100)
        actions = [e.action for e in events]
        assert "fleet.install.started" in actions
        assert "agent.spawned" in actions
        assert "fleet.installed" in actions

        terminal = next(e for e in events if e.action == "fleet.installed")
        assert terminal.payload["succeeded"] is False
        assert "connect:hubspot" in terminal.payload["failed_steps"]


# ---------------------------------------------------------------------------
# Scope fallback — when a fleet declares no scopes the installer still
# needs to produce a non-empty scope list (EventEntry invariant).
# ---------------------------------------------------------------------------


class TestScopeFallback:
    @pytest.mark.asyncio
    async def test_empty_scopes_fall_back_to_fleet_tag(self, journal, fake_pocket_creator) -> None:
        factory, _ = _fake_factory()
        fleet = _basic_fleet(scopes=[])

        await install_fleet(
            fleet,
            soul_factory=factory,
            pocket_creator=fake_pocket_creator,
            journal=journal,
        )

        for event in journal.query(limit=100):
            assert event.scope == ["fleet:sales-fleet"]
