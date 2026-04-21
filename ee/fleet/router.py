# ee/fleet/router.py — REST surface for the fleet install subsystem.
# Created: 2026-04-16 (feat/fleet-rest-router) — Exposes the Python
# primitives shipped in the fleet installer + journal-emission patches so
# paw-enterprise's InstallFleetPanel can list bundled templates and
# trigger an install over HTTP. Matches the existing ee router pattern:
# internal ``prefix="/fleet"`` + registered via _EE_ROUTERS at
# ``/api/v1``, giving ``/api/v1/fleet/templates`` and
# ``/api/v1/fleet/install``.
#
# Updated: 2026-04-16 (feat/ee-journal-dep) — dropped the local
# ``~/.pocketpaw/journal/fleet.db`` in favour of the shared
# ``ee.journal_dep.get_journal`` FastAPI dependency. Now every ee/ route
# writes into the same org journal (SOUL_DATA_DIR or ~/.soul/), so the
# audit trail is no longer split across two SQLite files. The request
# body flag ``journal`` still defaults to True; setting it False opts
# out and passes ``None`` into ``install_fleet`` unchanged.
#
# Updated: 2026-04-19 (fix/fleet-install-auth-guard) — P0 security gap.
# ``POST /fleet/install`` used to take only a ``journal`` dependency, so
# any authenticated user (and in fact any caller the journal dep did not
# reject) could spawn agents + pockets into any workspace. The handler
# now requires ``current_active_user`` so unauthenticated callers get
# 401, and the target ``workspace_id`` must be carried in the request
# body so we can enforce that the caller is an ``owner`` or ``admin`` of
# that workspace. Enforcement uses ``check_workspace_action`` against the
# canonical ``fleet.install`` rule registered in
# ``pocketpaw.ee.guards.actions.ACTIONS`` — this piggybacks on the
# existing ``log_denial`` audit wiring so every 403 also lands in the
# audit log. Below-admin roles and non-members get 403.
# ``template_name`` / ``journal`` / ``actor`` stay exactly as before —
# the only shape change is a new required ``workspace_id`` field on
# ``InstallFleetRequest``.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from soul_protocol.engine.journal import Journal

from ee.cloud.auth import current_active_user
from ee.cloud.models.user import User
from ee.fleet import (
    FleetInstallReport,
    FleetTemplate,
    install_fleet,
    list_bundled_fleets,
    load_fleet,
)
from ee.journal_dep import get_journal
from pocketpaw.ee.guards.deps import check_workspace_action
from pocketpaw.ee.guards.rbac import Forbidden as GuardForbidden

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fleet", tags=["Fleet"])


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

    ``workspace_id`` is the target workspace the fleet will be installed
    into. The server enforces that the authenticated caller is an
    ``owner`` or ``admin`` of that workspace before running the
    installer — see ``_require_fleet_install`` below.

    ``journal`` opts into the v0.3.1 correlated-event trio. ``actor``
    lets a caller attribute the install to a specific identity.
    """

    template_name: str
    workspace_id: str
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


def _require_fleet_install(user: User, workspace_id: str) -> None:
    """Raise ``HTTPException(403)`` unless ``user`` is allowed to run
    ``fleet.install`` in ``workspace_id``.

    Delegates to ``check_workspace_action`` so the canonical ACTIONS
    rule (``fleet.install`` → ``WorkspaceRole.ADMIN``) is the single
    source of truth, and so every denial is recorded via
    ``log_denial`` — the RBAC audit wiring the rest of the ee cloud
    routers already relies on. Non-members raise 403 with
    ``workspace.not_member``; members below admin raise 403 with
    ``workspace.insufficient_role``.

    Authentication itself is enforced by ``current_active_user`` on
    the route — this helper only runs after the user is resolved.
    """

    try:
        check_workspace_action(user, workspace_id, "fleet.install")
    except GuardForbidden as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc


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
async def post_install(
    req: InstallFleetRequest,
    user: User = Depends(current_active_user),
    journal: Journal = Depends(get_journal),
) -> FleetInstallReport:
    """Install a bundled fleet by name into the caller's workspace.

    Auth: requires an active user (``current_active_user`` returns 401
    otherwise) who is an ``owner`` or ``admin`` of
    ``req.workspace_id``. Members below admin and non-members both get
    403 — installing a fleet spawns agents + pockets scoped to the
    workspace, so treat it as a workspace-admin action.

    Resolves ``template_name`` via ``load_fleet()``, installs it, and
    returns the ``FleetInstallReport`` verbatim. Unknown names return
    404 with a clear message. When ``journal=true`` (the default) the
    installer receives the org's canonical Journal and emits the
    correlated ``fleet.install.started`` / ``agent.spawned`` /
    ``fleet.installed`` event trio; ``journal=false`` forwards ``None``
    so the installer skips emission.
    """

    # Authz first — never touch the filesystem or the installer before
    # the caller has proven admin+ on the target workspace. A 403 from
    # here does not leak template-loading errors or soul-protocol state.
    _require_fleet_install(user, req.workspace_id)

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

    actor = _resolve_actor(req.actor)
    effective_journal: Journal | None = journal if req.journal else None

    # Journal lifetime is managed by the dependency (process-scoped
    # singleton via lru_cache) — no per-request close, that would defeat
    # the cache and churn SQLite connections under load.
    return await install_fleet(fleet, journal=effective_journal, actor=actor)
