# ee/fleet/installer.py — Read a FleetTemplate manifest, install the bundle.
# Created: 2026-04-13 (Move 7 PR-B) — Pure orchestration. Uses existing
# primitives (SoulFactory, ConnectorRegistry, Pocket service) and does not
# introduce new runtime concepts. Each install step is independently
# reported so partial failures are observable.
# Updated: 2026-04-16 — PyYAML import-error message now points at
# `pocketpaw[soul]` (the pocketpaw extra that pulls PyYAML in via
# soul-protocol[engine]) instead of the transitive package name.

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ee.fleet.models import (
    FleetConnector,
    FleetInstallReport,
    FleetInstallStep,
    FleetTemplate,
)

logger = logging.getLogger(__name__)


_BUNDLED_DIR = Path(__file__).parent.parent.parent / "src" / "pocketpaw" / "fleet_templates"


def list_bundled_fleets() -> list[str]:
    """Return the names of bundled fleet templates on disk."""
    if not _BUNDLED_DIR.exists():
        return []
    return sorted(p.stem for p in _BUNDLED_DIR.glob("*.yaml"))


def load_fleet(path_or_name: str | Path) -> FleetTemplate:
    """Load a FleetTemplate from a YAML/JSON file or a bundled name.

    If the argument is one of the bundled fleet names, resolves to the
    packaged YAML. Otherwise treats it as a filesystem path.
    """
    if isinstance(path_or_name, str):
        bundled = _BUNDLED_DIR / f"{path_or_name}.yaml"
        if bundled.exists():
            return _load_from_path(bundled)
    p = Path(path_or_name)
    if not p.exists():
        raise FileNotFoundError(f"Fleet template not found: {path_or_name}")
    return _load_from_path(p)


def _load_from_path(path: Path) -> FleetTemplate:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ImportError(
                "PyYAML is required to load fleet templates. "
                "Install with `pip install pocketpaw[soul]`."
            ) from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    return FleetTemplate.model_validate(data)


async def install_fleet(
    fleet: FleetTemplate,
    *,
    soul_factory: Any | None = None,
    connector_registry: Any | None = None,
    pocket_creator: Any | None = None,
) -> FleetInstallReport:
    """Install a fleet by orchestrating soul + pocket + connector creation.

    Each external dependency is injectable so tests can substitute fakes.
    Production callers pass the real SoulFactory, ConnectorRegistry, and
    pocket service; install_fleet itself remains a pure orchestrator.
    """
    report = FleetInstallReport(fleet=fleet.name)

    soul = await _step_create_soul(report, fleet, soul_factory)
    if soul is None:
        return report
    report.soul_id = getattr(soul, "did", None) or getattr(soul, "name", None)

    pocket = await _step_create_pocket(report, fleet, pocket_creator)
    if pocket is not None:
        report.pocket_id = getattr(pocket, "id", None) or getattr(pocket, "_id", None)

    await _step_register_connectors(report, fleet, connector_registry)

    return report


async def _step_create_soul(
    report: FleetInstallReport,
    fleet: FleetTemplate,
    soul_factory: Any | None,
) -> Any | None:
    start = time.monotonic()
    try:
        if soul_factory is None:
            from soul_protocol.runtime.templates import SoulFactory

            soul_factory = SoulFactory

        template = soul_factory.load_bundled(fleet.soul_template)
        soul_name = fleet.soul_name or template.name
        soul = await soul_factory.from_template(template, name=soul_name)
        report.steps.append(
            FleetInstallStep(
                name=f"create_soul:{template.name}",
                status="succeeded",
                detail=f"Created soul '{soul_name}' from template '{template.name}'",
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
        )
        return soul
    except Exception as exc:
        logger.exception("Fleet install: soul creation failed")
        report.steps.append(
            FleetInstallStep(
                name=f"create_soul:{fleet.soul_template}",
                status="failed",
                detail=str(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
        )
        return None


async def _step_create_pocket(
    report: FleetInstallReport,
    fleet: FleetTemplate,
    pocket_creator: Any | None,
) -> Any | None:
    start = time.monotonic()
    if pocket_creator is None:
        # Pocket creation hooks into ee/cloud/pockets which is mongo-backed
        # and not always available in the test/standalone path. Skip cleanly.
        report.steps.append(
            FleetInstallStep(
                name=f"create_pocket:{fleet.pocket_name}",
                status="skipped",
                detail="Pocket creator not provided (cloud module not loaded)",
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
        )
        return None

    try:
        pocket = await pocket_creator(
            name=fleet.pocket_name,
            description=fleet.pocket_description,
            widgets=fleet.pocket_widgets,
            scope=list(fleet.scopes),
        )
        report.steps.append(
            FleetInstallStep(
                name=f"create_pocket:{fleet.pocket_name}",
                status="succeeded",
                detail=f"Created pocket '{fleet.pocket_name}'",
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
        )
        return pocket
    except Exception as exc:
        logger.exception("Fleet install: pocket creation failed")
        report.steps.append(
            FleetInstallStep(
                name=f"create_pocket:{fleet.pocket_name}",
                status="failed",
                detail=str(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
        )
        return None


async def _step_register_connectors(
    report: FleetInstallReport,
    fleet: FleetTemplate,
    connector_registry: Any | None,
) -> None:
    if not fleet.connectors:
        return

    if connector_registry is None:
        # Same pattern as pocket creation — caller must provide the registry.
        for conn in fleet.connectors:
            report.steps.append(
                FleetInstallStep(
                    name=f"connect:{conn.name}",
                    status="skipped",
                    detail="Connector registry not provided",
                ),
            )
        return

    for conn in fleet.connectors:
        await _register_one_connector(report, conn, connector_registry)


async def _register_one_connector(
    report: FleetInstallReport,
    conn: FleetConnector,
    registry: Any,
) -> None:
    start = time.monotonic()
    try:
        if not registry.has(conn.name):
            status = "skipped" if conn.optional else "failed"
            report.steps.append(
                FleetInstallStep(
                    name=f"connect:{conn.name}",
                    status=status,
                    detail=f"Connector '{conn.name}' not registered",
                    duration_ms=int((time.monotonic() - start) * 1000),
                ),
            )
            return

        await registry.connect(conn.name, conn.config)
        report.steps.append(
            FleetInstallStep(
                name=f"connect:{conn.name}",
                status="succeeded",
                detail=f"Connected '{conn.name}'",
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
        )
    except Exception as exc:
        logger.exception("Fleet install: connector %s failed", conn.name)
        report.steps.append(
            FleetInstallStep(
                name=f"connect:{conn.name}",
                status="failed",
                detail=str(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
        )
