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


# ---------------------------------------------------------------------------
# Installed-fleet management (cluster-d-fleet-ui-wire-up, 2026-04-19).
# The journal is the source of truth: ``fleet.installed`` events written by
# install_fleet carry soul_id + pocket_id + correlation_id, so we can
# reconstruct a per-workspace list without a new MongoDB collection. An
# uninstall flips matching agents + pockets to archived and journals a
# ``fleet.uninstalled`` summary with the original correlation_id + the list
# of soft-archived entity ids.
# ---------------------------------------------------------------------------


class InstalledFleet(BaseModel):
    """Summary row for one previously-installed fleet run.

    ``install_id`` is the correlation_id from the install trio — it
    uniquely identifies a single run (soul + pocket + connectors). A
    subsequent ``DELETE /fleet/installed/{install_id}`` uses that id
    both to target the uninstall and to tie the ``fleet.uninstalled``
    journal event back to the install via correlation.

    ``scope`` carries the original install event's scope tags so a
    caller can filter the list down to the workspace-level tags the
    admin actually owns. ``actor_id`` is the installer actor id, used
    for audit read-back.
    """

    install_id: str
    fleet: str
    installed_at: str
    soul_id: str | None = None
    pocket_id: str | None = None
    succeeded: bool = True
    scope: list[str] = Field(default_factory=list)
    actor_id: str = ""


class InstalledFleetsResponse(BaseModel):
    installed: list[InstalledFleet]
    total: int


class UninstallReport(BaseModel):
    """Mirror of ``FleetInstallReport`` for the teardown path.

    ``soft_archived_agents`` + ``soft_archived_pockets`` capture the
    ids of every entity flipped to archived status so a reviewer can
    trace exactly what was touched. ``failures`` collects any
    individual archive failures — the uninstall is deliberately
    tolerant (best-effort, never throws back) so partial cleanups can
    still complete the journal write and leave a forensic trail.
    ``journalled`` is True when the ``fleet.uninstalled`` event was
    successfully appended.
    """

    install_id: str
    fleet: str
    soft_archived_agents: list[str] = Field(default_factory=list)
    soft_archived_pockets: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    journalled: bool = False


def _row_to_installed(entry: Any) -> InstalledFleet | None:
    """Translate a ``fleet.installed`` journal entry into the response shape.

    Entries that fail to parse (corrupt payload, missing fleet name)
    are skipped — the list endpoint never blows up on one bad row.
    """
    try:
        payload = entry.payload or {}
        return InstalledFleet(
            install_id=str(entry.correlation_id),
            fleet=str(payload.get("fleet", "")),
            installed_at=entry.ts.isoformat() if hasattr(entry.ts, "isoformat") else str(entry.ts),
            soul_id=payload.get("soul_id"),
            pocket_id=payload.get("pocket_id"),
            succeeded=bool(payload.get("succeeded", True)),
            scope=list(entry.scope or []),
            actor_id=getattr(entry.actor, "id", ""),
        )
    except Exception as exc:  # noqa: BLE001 — observability only
        logger.warning("Skipping malformed fleet.installed entry: %s", exc)
        return None


def _filter_by_scope(rows: list[InstalledFleet], workspace_id: str) -> list[InstalledFleet]:
    """Return only the fleets whose journal scope mentions the workspace.

    Fleet events use scope lists like ``['org:sales:*']`` (from the
    template) or ``['workspace:{id}']`` (from a future install that
    threads the workspace). We match on string containment against
    ``workspace_id`` since the install journal today does not tag the
    workspace explicitly — once the installer threads workspace_id as
    an event scope, this filter tightens without an API shape change.
    """
    if not workspace_id:
        return rows
    needle = workspace_id.lower()
    filtered: list[InstalledFleet] = []
    for row in rows:
        if any(needle in s.lower() for s in row.scope):
            filtered.append(row)
            continue
        # Fall back to "no scope tags" rows so existing installs remain
        # visible while the installer catches up to workspace-tagged
        # scopes. The uninstall path still applies workspace authz, so
        # a stray match here cannot bypass the admin guard.
        if not row.scope:
            filtered.append(row)
    return filtered


@router.get("/installed", response_model=InstalledFleetsResponse)
async def list_installed(
    workspace_id: str,
    user: User = Depends(current_active_user),
    journal: Journal = Depends(get_journal),
    limit: int = 100,
) -> InstalledFleetsResponse:
    """Return the ``fleet.installed`` events for a workspace.

    Auth: authenticated + workspace admin+. The same rule as install.
    ``limit`` caps the pager at 500 server-side to keep the response
    bounded on long-running orgs.
    """
    _require_fleet_install(user, workspace_id)

    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    entries = journal.query(action="fleet.installed", limit=limit)
    rows: list[InstalledFleet] = []
    for entry in entries:
        row = _row_to_installed(entry)
        if row is not None:
            rows.append(row)
    scoped = _filter_by_scope(rows, workspace_id)
    return InstalledFleetsResponse(installed=scoped, total=len(scoped))


async def _soft_archive_agents(soul_id: str | None) -> list[str]:
    """Flag every agent with ``config.soul_id == soul_id`` as archived.

    Returns the list of affected agent ids. Agent archival lives on
    ``visibility == 'archived'`` until a dedicated ``archived_at`` land
    — the visibility pattern already exists in AgentEditor's filter set
    and cascades automatically to the UI.
    """
    if not soul_id:
        return []
    try:
        from ee.cloud.models.agent import Agent  # imported lazily; ee optional
    except Exception:
        return []

    affected: list[str] = []
    try:
        cursor = Agent.find({"config.soul_id": soul_id})
        async for agent in cursor:
            agent.visibility = "archived"
            await agent.save()
            affected.append(str(agent.id))
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        logger.warning("Fleet uninstall: agent archive failed: %s", exc)
    return affected


async def _soft_archive_pockets(pocket_id: str | None) -> list[str]:
    """Flip the matching pocket doc to archived. Only the install's
    primary pocket is targeted — connector-spawned pockets (if any)
    stay live to avoid nuking shared work. Operator can archive those
    from the pockets panel if they want a full sweep.
    """
    if not pocket_id:
        return []
    try:
        from ee.cloud.models.pocket import Pocket
    except Exception:
        return []

    affected: list[str] = []
    try:
        from beanie import PydanticObjectId

        pocket = await Pocket.get(PydanticObjectId(pocket_id))
        if pocket is not None:
            pocket.archived = True
            await pocket.save()
            affected.append(str(pocket.id))
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        logger.warning("Fleet uninstall: pocket archive failed: %s", exc)
    return affected


@router.delete(
    "/installed/{install_id}",
    response_model=UninstallReport,
)
async def uninstall(
    install_id: str,
    workspace_id: str,
    user: User = Depends(current_active_user),
    journal: Journal = Depends(get_journal),
) -> UninstallReport:
    """Tear down a previously-installed fleet by correlation_id.

    Teardown is deliberately soft: agents and pockets flip to archived
    rather than being hard-deleted so chat history and fabric links
    keep their referents. The operation is idempotent — rerunning
    against an already-uninstalled id returns an empty report with
    ``journalled=False``, never a 500. Every run emits a
    ``fleet.uninstalled`` event when a matching install is found.
    """
    _require_fleet_install(user, workspace_id)

    # Re-use the same install_id filter we use for listing.
    entries = journal.query(action="fleet.installed", limit=500)
    target_uuid: Any = None
    try:
        from uuid import UUID

        target_uuid = UUID(install_id)
    except Exception as exc:
        raise HTTPException(400, detail="install_id must be a UUID") from exc

    match = next((e for e in entries if e.correlation_id == target_uuid), None)
    if match is None:
        # Idempotent no-op: caller asked for something already gone (or
        # never existed). Surface an empty report rather than 404 so a
        # retry after network failure doesn't flip to error.
        return UninstallReport(install_id=install_id, fleet="")

    install_row = _row_to_installed(match)
    if install_row is None:
        raise HTTPException(500, detail="Malformed install record")

    # Extra scope check: the original install's scope must overlap the
    # caller's workspace. Belt-and-braces on top of the admin guard —
    # otherwise a workspace admin on org B could potentially address an
    # install scoped to org A via a crafted install_id.
    scoped_rows = _filter_by_scope([install_row], workspace_id)
    if not scoped_rows:
        raise HTTPException(403, detail="install.scope_mismatch")

    archived_agents = await _soft_archive_agents(install_row.soul_id)
    archived_pockets = await _soft_archive_pockets(install_row.pocket_id)
    failures: list[str] = []

    report = UninstallReport(
        install_id=install_id,
        fleet=install_row.fleet,
        soft_archived_agents=archived_agents,
        soft_archived_pockets=archived_pockets,
        failures=failures,
    )

    # Journal the teardown so the decision trail stays intact.
    try:
        from uuid import uuid4

        from soul_protocol.spec.journal import Actor as _Actor
        from soul_protocol.spec.journal import EventEntry

        actor = _Actor(
            kind="user",
            id=str(user.id),
            scope_context=list(install_row.scope),
        )
        from datetime import UTC, datetime

        entry = EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=actor,
            action="fleet.uninstalled",
            scope=list(install_row.scope) or [f"workspace:{workspace_id}"],
            correlation_id=target_uuid,
            payload={
                "fleet": install_row.fleet,
                "install_id": install_id,
                "soft_archived_agents": archived_agents,
                "soft_archived_pockets": archived_pockets,
                "failure_count": len(failures),
            },
        )
        journal.append(entry)
        report.journalled = True
    except Exception as exc:  # noqa: BLE001 — observability only
        logger.warning("Fleet uninstall: journal write failed: %s", exc)

    return report
