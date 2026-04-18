# tests/cloud/test_fleet_installer.py — Move 7 PR-B.
# Created: 2026-04-13 — Manifest loader, install orchestration with mocked
# soul/connector/pocket dependencies, partial-failure reporting, bundled
# Sales Fleet contract, and the install report shape.

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ee.fleet import (
    FleetConnector,
    FleetInstallReport,
    FleetTemplate,
    install_fleet,
    list_bundled_fleets,
    load_fleet,
)

# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------


class TestLoader:
    def test_loads_yaml_manifest(self, tmp_path: Path) -> None:
        path = tmp_path / "custom.yaml"
        path.write_text(
            textwrap.dedent(
                """
                name: custom-fleet
                soul_template: arrow
                pocket_name: Custom Pocket
                scopes:
                  - org:sales:*
                """,
            ).strip(),
            encoding="utf-8",
        )
        fleet = load_fleet(path)
        assert fleet.name == "custom-fleet"
        assert fleet.soul_template == "arrow"
        assert fleet.scopes == ["org:sales:*"]

    def test_loads_json_manifest(self, tmp_path: Path) -> None:
        path = tmp_path / "custom.json"
        path.write_text(
            json.dumps(
                {
                    "name": "json-fleet",
                    "soul_template": "flash",
                    "pocket_name": "JSON Pocket",
                }
            ),
            encoding="utf-8",
        )
        fleet = load_fleet(path)
        assert fleet.name == "json-fleet"

    def test_loads_bundled_by_name(self) -> None:
        names = list_bundled_fleets()
        if not names:
            pytest.skip("No bundled fleets available")
        fleet = load_fleet(names[0])
        assert isinstance(fleet, FleetTemplate)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_fleet(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# install_fleet — orchestration
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


def _fake_factory(soul_template_name: str = "arrow"):
    """Return an object that quacks like SoulFactory — load_bundled + from_template."""
    factory = MagicMock()

    template = MagicMock()
    template.name = soul_template_name.capitalize()
    factory.load_bundled = MagicMock(return_value=template)

    soul = MagicMock()
    soul.did = "did:soul:fake-1"
    soul.name = template.name
    factory.from_template = AsyncMock(return_value=soul)
    return factory, soul


@pytest.fixture
def fake_pocket_creator():
    pocket = MagicMock()
    pocket.id = "pocket_fake_1"
    creator = AsyncMock(return_value=pocket)
    return creator, pocket


@pytest.fixture
def fake_registry():
    registry = MagicMock()
    registry.has = MagicMock(return_value=True)
    registry.connect = AsyncMock(return_value=True)
    return registry


class TestInstallOrchestration:
    @pytest.mark.asyncio
    async def test_install_creates_soul_pocket_and_connectors(
        self, fake_pocket_creator, fake_registry
    ) -> None:
        factory, soul = _fake_factory()
        creator, pocket = fake_pocket_creator

        fleet = _basic_fleet(
            connectors=[FleetConnector(name="hubspot", config={"poll_minutes": 15})],
        )
        report = await install_fleet(
            fleet,
            soul_factory=factory,
            connector_registry=fake_registry,
            pocket_creator=creator,
        )

        assert report.succeeded()
        assert report.soul_id == "did:soul:fake-1"
        assert report.pocket_id == "pocket_fake_1"
        statuses = [step.status for step in report.steps]
        assert "succeeded" in statuses
        assert all(s != "failed" for s in statuses)

    @pytest.mark.asyncio
    async def test_install_skips_pocket_when_creator_missing(self) -> None:
        factory, _ = _fake_factory()
        report = await install_fleet(
            _basic_fleet(),
            soul_factory=factory,
            connector_registry=None,
            pocket_creator=None,
        )
        skipped = [s for s in report.steps if s.status == "skipped"]
        assert any("create_pocket" in s.name for s in skipped)
        assert report.pocket_id is None

    @pytest.mark.asyncio
    async def test_install_marks_optional_missing_connector_as_skipped(
        self, fake_pocket_creator
    ) -> None:
        factory, _ = _fake_factory()
        creator, _ = fake_pocket_creator
        registry = MagicMock()
        registry.has = MagicMock(return_value=False)

        fleet = _basic_fleet(
            connectors=[FleetConnector(name="missing-connector", optional=True)],
        )
        report = await install_fleet(
            fleet,
            soul_factory=factory,
            connector_registry=registry,
            pocket_creator=creator,
        )
        connector_step = next(s for s in report.steps if "missing-connector" in s.name)
        assert connector_step.status == "skipped"

    @pytest.mark.asyncio
    async def test_install_marks_required_missing_connector_as_failed(
        self, fake_pocket_creator
    ) -> None:
        factory, _ = _fake_factory()
        creator, _ = fake_pocket_creator
        registry = MagicMock()
        registry.has = MagicMock(return_value=False)

        fleet = _basic_fleet(
            connectors=[FleetConnector(name="critical-connector", optional=False)],
        )
        report = await install_fleet(
            fleet,
            soul_factory=factory,
            connector_registry=registry,
            pocket_creator=creator,
        )
        assert not report.succeeded()
        failed = report.failed_steps()
        assert len(failed) == 1
        assert "critical-connector" in failed[0].name

    @pytest.mark.asyncio
    async def test_install_swallows_per_step_exceptions(self, fake_pocket_creator) -> None:
        factory, _ = _fake_factory()
        creator, _ = fake_pocket_creator
        registry = MagicMock()
        registry.has = MagicMock(return_value=True)
        registry.connect = AsyncMock(side_effect=RuntimeError("network down"))

        fleet = _basic_fleet(connectors=[FleetConnector(name="hubspot")])
        report = await install_fleet(
            fleet,
            soul_factory=factory,
            connector_registry=registry,
            pocket_creator=creator,
        )
        failed = report.failed_steps()
        assert len(failed) == 1
        assert "network down" in failed[0].detail

    @pytest.mark.asyncio
    async def test_install_returns_early_on_soul_failure(self) -> None:
        factory = MagicMock()
        factory.load_bundled = MagicMock(side_effect=FileNotFoundError("template missing"))

        fleet = _basic_fleet()
        report = await install_fleet(fleet, soul_factory=factory)
        assert not report.succeeded()
        assert report.soul_id is None
        # Pocket + connector steps shouldn't even appear.
        assert all("create_pocket" not in s.name for s in report.steps)


# ---------------------------------------------------------------------------
# Bundled Sales Fleet — contract check
# ---------------------------------------------------------------------------


class TestSalesFleetBundle:
    def test_sales_fleet_is_bundled(self) -> None:
        names = list_bundled_fleets()
        assert "sales-fleet" in names

    def test_sales_fleet_has_arrow_soul_and_sales_scope(self) -> None:
        fleet = load_fleet("sales-fleet")
        assert fleet.soul_template == "arrow"
        assert "org:sales:*" in fleet.scopes

    def test_sales_fleet_lists_widgets_and_connectors(self) -> None:
        fleet = load_fleet("sales-fleet")
        assert len(fleet.pocket_widgets) >= 2
        assert any(c.name == "hubspot" for c in fleet.connectors)
        assert all(c.optional for c in fleet.connectors), (
            "All Sales Fleet connectors should be optional so the demo install "
            "works without external API keys."
        )


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


class TestInstallReport:
    def test_succeeded_when_no_failed_steps(self) -> None:
        report = FleetInstallReport(fleet="x")
        assert report.succeeded()

    def test_failed_steps_filters(self) -> None:
        from ee.fleet.models import FleetInstallStep

        report = FleetInstallReport(
            fleet="x",
            steps=[
                FleetInstallStep(name="a", status="succeeded"),
                FleetInstallStep(name="b", status="failed"),
                FleetInstallStep(name="c", status="skipped"),
            ],
        )
        failed = report.failed_steps()
        assert len(failed) == 1
        assert failed[0].name == "b"
        assert not report.succeeded()
