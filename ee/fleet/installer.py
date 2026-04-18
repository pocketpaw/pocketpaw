# ee/fleet/installer.py — Read a FleetTemplate manifest, install the bundle.
# Created: 2026-04-13 (Move 7 PR-B) — Pure orchestration. Uses existing
# primitives (SoulFactory, ConnectorRegistry, Pocket service) and does not
# introduce new runtime concepts. Each install step is independently
# reported so partial failures are observable.
# Updated: 2026-04-16 — PyYAML import-error message now points at
# `pocketpaw[soul]` (the pocketpaw extra that pulls PyYAML in via
# soul-protocol[engine]) instead of the transitive package name.
# Updated: 2026-04-16 (feat/fleet-journal-emission) — install_fleet now
# accepts an optional Journal + Actor and emits a correlated trio of events
# on every run: one `fleet.install.started` (extension namespace), one
# canonical `agent.spawned` per soul created, and one `fleet.installed`
# summary at the end. The journal parameter is opt-in so existing callers
# (tests, CLI without an org) keep working unchanged. Emission errors are
# logged and swallowed — the journal is observability, not control flow.

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from ee.fleet.models import (
    FleetConnector,
    FleetInstallReport,
    FleetInstallStep,
    FleetTemplate,
)

if TYPE_CHECKING:
    from soul_protocol.engine.journal import Journal
    from soul_protocol.spec.journal import Actor

logger = logging.getLogger(__name__)


_SYSTEM_INSTALLER_ACTOR_ID = "system:fleet-installer"


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
    journal: Journal | None = None,
    actor: Actor | None = None,
) -> FleetInstallReport:
    """Install a fleet by orchestrating soul + pocket + connector creation.

    Each external dependency is injectable so tests can substitute fakes.
    Production callers pass the real SoulFactory, ConnectorRegistry, and
    pocket service; install_fleet itself remains a pure orchestrator.

    When a ``journal`` is supplied, the installer emits a correlated event
    trio for the run:

    * ``fleet.install.started`` (extension namespace) when the run begins.
    * ``agent.spawned`` (canonical namespace) for each soul created.
    * ``fleet.installed`` (extension namespace) only when the soul step
      succeeded — a partial install stops at the step boundary and leaves
      no terminal event, so projections and UI tailers can see the gap.

    All three events share a single ``correlation_id`` (generated per run)
    and carry the fleet's declared ``scopes`` verbatim. If ``actor`` is
    omitted, a ``system:fleet-installer`` actor is recorded so events are
    attributable without an org root soul present.

    Journal errors are logged and swallowed. The installer's return value
    is the existing :class:`FleetInstallReport` regardless of emission.
    """
    report = FleetInstallReport(fleet=fleet.name)

    correlation_id = uuid4()
    scope = _resolve_scope(fleet)
    resolved_actor = actor if actor is not None else _default_system_actor(scope)

    _emit(
        journal,
        action="fleet.install.started",
        actor=resolved_actor,
        scope=scope,
        correlation_id=correlation_id,
        payload={
            "fleet": fleet.name,
            "version": fleet.version,
            "soul_template": fleet.soul_template,
        },
    )

    soul = await _step_create_soul(report, fleet, soul_factory)
    if soul is None:
        # Partial install: no agent.spawned, no fleet.installed. The
        # report itself already shows the failed step.
        return report
    report.soul_id = getattr(soul, "did", None) or getattr(soul, "name", None)

    _emit(
        journal,
        action="agent.spawned",
        actor=resolved_actor,
        scope=scope,
        correlation_id=correlation_id,
        payload=_agent_spawned_payload(fleet, soul),
    )

    pocket = await _step_create_pocket(report, fleet, pocket_creator)
    if pocket is not None:
        report.pocket_id = getattr(pocket, "id", None) or getattr(pocket, "_id", None)

    await _step_register_connectors(report, fleet, connector_registry)

    _emit(
        journal,
        action="fleet.installed",
        actor=resolved_actor,
        scope=scope,
        correlation_id=correlation_id,
        payload={
            "fleet": fleet.name,
            "soul_id": report.soul_id,
            "pocket_id": report.pocket_id,
            "succeeded": report.succeeded(),
            "step_count": len(report.steps),
            "failed_steps": [s.name for s in report.failed_steps()],
        },
    )

    return report


# ---------------------------------------------------------------------------
# Journal helpers — all tolerant of a None journal so production callers can
# opt in without branching at every call site.
# ---------------------------------------------------------------------------


def _resolve_scope(fleet: FleetTemplate) -> list[str]:
    """Pick the scope for journal events. Fall back to a fleet-qualified tag
    so the EventEntry's non-empty scope invariant holds even when a template
    author forgot to declare scopes.
    """
    if fleet.scopes:
        return list(fleet.scopes)
    return [f"fleet:{fleet.name}"]


def _default_system_actor(scope: list[str]) -> Actor:
    """Build the ``system:fleet-installer`` actor used when a caller does
    not supply a root/admin actor. Scope context mirrors the event scope so
    later audits can see the permissions the installer was acting under.
    """
    from soul_protocol.spec.journal import Actor

    return Actor(kind="system", id=_SYSTEM_INSTALLER_ACTOR_ID, scope_context=list(scope))


def _agent_spawned_payload(fleet: FleetTemplate, soul: Any) -> dict[str, Any]:
    """Assemble the canonical ``agent.spawned`` payload. The soul object is
    duck-typed — the fleet installer accepts any factory that returns
    something with ``did`` and/or ``name`` attributes, so we mirror that here.
    """
    did = getattr(soul, "did", None)
    name = getattr(soul, "name", None)
    return {
        "soul_id": did or name,
        "did": did,
        "name": name,
        "archetype": fleet.soul_template,
        "fleet": fleet.name,
    }


def _emit(
    journal: Journal | None,
    *,
    action: str,
    actor: Actor,
    scope: list[str],
    correlation_id: UUID,
    payload: dict[str, Any],
) -> None:
    """Append one event to the journal, swallowing and logging any failure.

    The installer's job is to install — journal emission is a side channel
    for observability. A broken journal must not translate into a broken
    install, so every failure mode (import, validation, backend I/O) is
    logged at warning level and discarded.
    """
    if journal is None:
        return
    try:
        from soul_protocol.spec.journal import EventEntry

        entry = EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=actor,
            action=action,
            scope=list(scope),
            correlation_id=correlation_id,
            payload=payload,
        )
        journal.append(entry)
    except Exception as exc:  # noqa: BLE001 — see docstring.
        logger.warning("Fleet install: journal emission for %s failed: %s", action, exc)


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
