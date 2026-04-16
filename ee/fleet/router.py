# ee/fleet/router.py — REST surface for the fleet install subsystem.
# Created: 2026-04-16 (feat/fleet-rest-router) — Exposes the Python
# primitives shipped in the fleet installer + journal-emission patches so
# paw-enterprise's InstallFleetPanel can list bundled templates and
# trigger an install over HTTP. Matches the existing ee router pattern:
# internal ``prefix="/fleet"`` + registered via _EE_ROUTERS at
# ``/api/v1``, giving ``/api/v1/fleet/templates`` and
# ``/api/v1/fleet/install``.
#
# Journal emission is opt-in per request. When ``journal=true`` the
# router lazily opens a local SQLite journal at
# ``~/.pocketpaw/journal/fleet.db`` so every install is observable even
# without an org-scoped journal wiring. The heavier per-org Journal
# dependency-injection can supersede this later without breaking callers.

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ee.fleet import (
    FleetInstallReport,
    FleetTemplate,
    install_fleet,
    list_bundled_fleets,
    load_fleet,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fleet", tags=["Fleet"])


_DEFAULT_JOURNAL_PATH = Path.home() / ".pocketpaw" / "journal" / "fleet.db"


# ---------------------------------------------------------------------------
# Request / response envelopes
# ---------------------------------------------------------------------------


class FleetTemplatesResponse(BaseModel):
    """List response for ``GET /fleet/templates``.

    Wraps the templates in a top-level envelope so the payload has space
    for future pagination / total counts without a breaking change.
    """

    templates: list[FleetTemplate]
    total: int


class ActorSpec(BaseModel):
    """Optional caller identity forwarded to the journal on install.

    When omitted the installer's built-in ``system:fleet-installer``
    actor is recorded instead. Keeps the router stateless while still
    letting richer clients (paw-enterprise) attribute installs to the
    logged-in operator.
    """

    kind: str = "user"
    id: str
    scope_context: list[str] = Field(default_factory=list)


class InstallFleetRequest(BaseModel):
    """Body for ``POST /fleet/install``.

    ``journal`` opts into the v0.3.1 correlated-event trio. ``actor``
    lets a caller attribute the install to a specific identity.
    """

    template_name: str
    journal: bool = True
    actor: ActorSpec | None = None


# ---------------------------------------------------------------------------
# Internal helpers — isolated so tests can patch them without touching
# the filesystem or soul-protocol internals.
# ---------------------------------------------------------------------------


def _load_all_bundled() -> list[FleetTemplate]:
    """Resolve every bundled fleet name to a full FleetTemplate.

    Templates that fail to parse are skipped with a warning — one bad
    template shouldn't sink the whole list endpoint for every caller.
    """

    templates: list[FleetTemplate] = []
    for name in list_bundled_fleets():
        try:
            templates.append(load_fleet(name))
        except Exception as exc:  # noqa: BLE001 — observability only.
            logger.warning("Skipping bundled fleet %s: %s", name, exc)
    return templates


def _open_default_journal() -> Any | None:
    """Open (or create) the default fleet journal at the canonical path.

    Returns ``None`` when soul-protocol is not installed or the journal
    cannot be opened — the installer tolerates a missing journal and
    the request still succeeds.
    """

    try:
        from soul_protocol.engine.journal import open_journal
    except ImportError:
        logger.warning(
            "Fleet install: soul-protocol not available, skipping journal emission. "
            "Install `pocketpaw[soul]` or pass journal=false to silence this.",
        )
        return None

    try:
        _DEFAULT_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        return open_journal(_DEFAULT_JOURNAL_PATH)
    except Exception:  # noqa: BLE001 — observability only.
        logger.exception("Fleet install: failed to open default journal")
        return None


def _resolve_actor(spec: ActorSpec | None) -> Any | None:
    """Translate an ``ActorSpec`` payload to a soul-protocol Actor.

    Returns ``None`` when no spec was supplied so the installer's
    default system actor is used instead.
    """

    if spec is None:
        return None
    try:
        from soul_protocol.spec.journal import Actor
    except ImportError:
        return None
    return Actor(kind=spec.kind, id=spec.id, scope_context=list(spec.scope_context))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/templates", response_model=FleetTemplatesResponse)
async def get_templates() -> FleetTemplatesResponse:
    """Return every bundled fleet template the server knows about.

    This is what paw-enterprise's InstallFleetPanel calls on mount to
    populate its picker. Each entry is the full ``FleetTemplate`` so
    the UI can show description, connectors, widgets, and scopes
    without a second round-trip.
    """

    templates = _load_all_bundled()
    return FleetTemplatesResponse(templates=templates, total=len(templates))


@router.post("/install", response_model=FleetInstallReport)
async def post_install(req: InstallFleetRequest) -> FleetInstallReport:
    """Install a bundled fleet by name.

    Resolves ``template_name`` via ``load_fleet()``, installs it, and
    returns the ``FleetInstallReport`` verbatim. Unknown names return
    404 with a clear message. When ``journal=true`` the installer
    emits the correlated ``fleet.install.started`` /
    ``agent.spawned`` / ``fleet.installed`` event trio.
    """

    try:
        fleet = load_fleet(req.template_name)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Fleet template '{req.template_name}' not found",
        ) from None
    except Exception as exc:
        logger.exception("Fleet install: failed to load template %s", req.template_name)
        raise HTTPException(
            status_code=400,
            detail=f"Failed to load fleet template: {exc}",
        ) from exc

    journal = _open_default_journal() if req.journal else None
    actor = _resolve_actor(req.actor)

    try:
        report = await install_fleet(fleet, journal=journal, actor=actor)
    finally:
        # open_journal returns a resource with a close() method on the
        # SQLite backend. Closing keeps the per-request writer from
        # leaking file handles under load.
        close = getattr(journal, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 — best-effort cleanup.
                logger.debug("Fleet install: journal close failed", exc_info=True)

    return report
