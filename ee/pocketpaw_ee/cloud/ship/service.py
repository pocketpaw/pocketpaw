# ee/pocketpaw_ee/cloud/ship/service.py — the /ship business logic (SHIP-3).
#
# The workspace-scoped surface behind ``/api/v1/ship``: provision a box, register
# an app, deploy it, route a domain, link a database, read logs and box health,
# and PARK a teardown for approval. It is the entity's only caller-facing layer —
# the router is a thin adapter, ``store`` owns the Beanie documents, ``engine``
# owns the live SSH/driver seam, and the long deploy runs as an arq job.
#
# ee/cloud rules honored here:
#   3  every domain view carries a REQUIRED ``workspace_id``.
#   5  module-level ``async def op(workspace_id, user_id, body)``.
#   6  validate at entry — ``body = <Request>.model_validate(body)`` first line.
#   7  every read is tenant-filtered (``store`` takes ``workspace_id`` first and
#      returns None for a foreign id; this module collapses that to ``NotFound``,
#      so a cross-tenant probe reads as 404 and never leaks existence).
#   9  emit on every write; read paths carry an explicit ``# no-event:``.
#   10 CloudError subclasses only — never HTTPException.
#
# SECURITY: nothing here ever touches key material. The box's SSH key is
# decrypted inside ``engine.box_session`` and shredded with the session; env
# VALUES are never accepted or stored (``env_refs`` are NAMES); a database's
# connection string never leaves the box (``DbView.env_var`` is the variable's
# name, per SHIP-1's ``DbResult`` invariant).
#
# DESTROY IS NEVER EXECUTED HERE. ``request_box_destroy`` / ``request_app_destroy``
# mint a placeholder proposal id and park it on the row — see the ``# SHIP-4:``
# seam markers.
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from pydantic import ValidationError as PydanticValidationError

from pocketpaw_ee.cloud._core.errors import ConflictError, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    ShipAppCreated,
    ShipAppUpdated,
    ShipBoxCreated,
    ShipDeployQueued,
    ShipDestroyProposed,
)
from pocketpaw_ee.cloud.ship import engine as ship_engine
from pocketpaw_ee.cloud.ship import enqueue, propose, store
from pocketpaw_ee.cloud.ship.domain import (
    AppId,
    AppMetricsView,
    AppView,
    BoxId,
    BoxMetricsView,
    BoxView,
    DbView,
    DeployId,
    DeployView,
    DestroyProposalView,
    DomainView,
    EnvVarView,
    LifecycleView,
    LogsView,
)
from pocketpaw_ee.cloud.ship.dto import (
    AddDomainRequest,
    AppMetricsOut,
    AppOut,
    BoxOut,
    CreateAppRequest,
    CreateBoxRequest,
    CreateDbRequest,
    CreateVolumeRequest,
    DatabaseOut,
    DbOut,
    DeployOut,
    DomainListOut,
    DomainOut,
    EnvOut,
    EnvVarIn,
    EnvVarOut,
    ImportEnvRequest,
    LifecycleOut,
    LogsOut,
    MetricsOut,
    PendingApprovalOut,
    SetChecksRequest,
    SetEnvRequest,
    SetResourcesRequest,
    SetScaleRequest,
    SetSourceRequest,
    VolumeOut,
)
from pocketpaw_ee.ship_engine.port import CommandFailed

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.models.ship import ShipApp, ShipBox, ShipDeploy

logger = logging.getLogger(__name__)

# Deployment defaults for a freshly provisioned box. Hetzner's ``cx22`` (2 vCPU /
# 4 GB / 40 GB) in ``fsn1`` is the cheapest shape that comfortably runs Dokku
# plus a couple of app containers; both are env-overridable per deployment and
# per request (``CreateBoxRequest.server_type`` / ``.region``).
DEFAULT_SERVER_TYPE = os.environ.get("POCKETPAW_SHIP_SERVER_TYPE", "").strip() or "cx22"
DEFAULT_REGION = os.environ.get("POCKETPAW_SHIP_REGION", "").strip() or "fsn1"

# How many log lines ``GET /ship/apps/{id}/logs`` reads by default.
DEFAULT_LOG_LINES = 100


# --------------------------------------------------------------------------- #
# Boxes
# --------------------------------------------------------------------------- #


async def create_box(workspace_id: str, user_id: str, body: CreateBoxRequest) -> BoxView:
    """Accept a box provision. Returns immediately with a pollable box.

    The actual provision runs as the SHIP-2 arq job; the returned box is in
    ``provisioning`` until it answers over SSH.
    """
    body = CreateBoxRequest.model_validate(body)
    box = await enqueue.enqueue_provision(
        workspace_id=workspace_id,
        provider=body.provider,
        server_type=body.server_type or DEFAULT_SERVER_TYPE,
        region=body.region or DEFAULT_REGION,
    )
    view = _box_view(box)
    await emit(
        ShipBoxCreated(
            data={
                "id": view.id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "provider": view.provider,
                "status": view.status,
            }
        )
    )
    return view


async def list_boxes(workspace_id: str) -> list[BoxView]:
    """Every box the workspace owns, newest first."""
    # no-event: read-only path; emit only on writes (cloud rule #9).
    return [_box_view(box) for box in await store.list_boxes(workspace_id)]


async def get_box_metrics(
    workspace_id: str,
    box_id: str,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> BoxMetricsView:
    """Read live CPU / memory / disk off the box.

    A box that is not ``ready`` has nothing to answer with — that is a 409, not
    a fabricated row of zeroes.
    """
    # no-event: read-only path; emit only on writes (cloud rule #9).
    box = await _require_box(workspace_id, box_id)
    if box.status != "ready":
        raise ConflictError("ship.box_not_ready", f"Box is {box.status}, not ready")
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            cpu, mem, disk = await ship_engine.read_box_metrics(session)
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict("ship.metrics_failed", exc) from exc
    return BoxMetricsView(
        workspace_id=workspace_id, box_id=str(box.id), cpu=cpu, mem=mem, disk=disk
    )


async def request_box_destroy(workspace_id: str, user_id: str, box_id: str) -> DestroyProposalView:
    """PARK a box teardown for human approval. Destroys NOTHING."""
    box = await _require_box(workspace_id, box_id)
    # File a REAL Instinct proposal (SHIP-4). Nothing is destroyed here — the
    # approve path (``ship.executor.execute_approved_ship_action``) is the only
    # code that may ever call the engine's ``destroy`` verb. A box that already
    # carries a pending proposal reuses it rather than filing a duplicate.
    if box.pending_destroy_proposal_id:
        proposal_id = box.pending_destroy_proposal_id
    else:
        proposal_id = await propose.propose_ship_action(
            workspace_id=workspace_id,
            verb="destroy_box",
            box_id=str(box.id),
            target_label=f"box {box.ip or str(box.id)}",
            requested_by=user_id,
        )
    box = await store.park_box_destroy(box, proposal_id=proposal_id)
    return await _emit_destroy_proposal(
        workspace_id, user_id, kind="box", target_id=str(box.id), proposal_id=proposal_id
    )


# --------------------------------------------------------------------------- #
# Apps
# --------------------------------------------------------------------------- #


async def create_app(workspace_id: str, user_id: str, body: CreateAppRequest) -> AppView:
    """Register an app on one of the workspace's boxes."""
    body = CreateAppRequest.model_validate(body)
    box = await _require_box(workspace_id, body.box_id)
    if await store.find_app_by_name(workspace_id, str(box.id), body.name):
        raise ConflictError("ship.app_exists", f"An app named '{body.name}' already exists")

    app = await store.create_app(
        workspace_id=workspace_id,
        box_id=str(box.id),
        name=body.name,
        build_path=body.build_path,
        git_ref=body.git_ref,
        image=body.image,
        env_refs=body.env_refs,
        prod=body.prod,
        source_kind=body.source_kind,
        repo_url=body.repo_url,
        repo_ref=body.repo_ref,
        repo_token=body.token,
    )
    view = _app_view(app)
    await emit(ShipAppCreated(data={**_app_event_payload(view), "user_id": user_id}))
    return view


async def list_apps(workspace_id: str, *, box_id: str | None = None) -> list[AppView]:
    """Every app the workspace owns, optionally narrowed to one box."""
    # no-event: read-only path; emit only on writes (cloud rule #9).
    return [_app_view(app) for app in await store.list_apps(workspace_id, box_id=box_id)]


async def set_app_source(
    workspace_id: str, user_id: str, app_id: str, body: SetSourceRequest
) -> AppView:
    """Point the app at a deploy source (``image`` or ``git``). Returns the app
    view (masked — the private-repo token is never echoed).

    The token is handed to the store, which is the SOLE place it is encrypted; it
    never lives in a view, a wire DTO, an event, or a log.
    """
    body = SetSourceRequest.model_validate(body)
    app = await _require_app(workspace_id, app_id)
    app = await store.set_app_source(
        app,
        source_kind=body.source_kind,
        repo_url=body.repo_url,
        repo_ref=body.repo_ref,
        repo_token=body.token,
    )
    view = _app_view(app)
    await emit(ShipAppUpdated(data={**_app_event_payload(view), "user_id": user_id}))
    return view


async def deploy_app(workspace_id: str, user_id: str, app_id: str) -> DeployView:
    """Enqueue a deploy for the app. Returns immediately with a pollable attempt."""
    app = await _require_app(workspace_id, app_id)
    _require_deployable_source(app)
    # The box must exist and be reachable before we spend a worker slot on it.
    box = await _require_box(workspace_id, app.box_id)
    if box.status != "ready":
        raise ConflictError("ship.box_not_ready", f"Box is {box.status}, not ready")

    # Flip the app BEFORE dispatching. The worker may pick the job up
    # immediately and write a terminal status; writing "deploying" afterwards
    # would clobber it. A dispatch failure leaves the app "deploying" with a
    # "queued" attempt — a visibly stuck deploy, which is the honest state.
    app = await store.set_app_status(app, "deploying")
    deploy = await enqueue.enqueue_deploy(
        workspace_id=workspace_id, app_id=str(app.id), image=app.image
    )
    view = _deploy_view(deploy)
    await emit(
        ShipDeployQueued(
            data={
                "id": view.id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "app_id": view.app_id,
                "status": view.status,
            }
        )
    )
    await emit(ShipAppUpdated(data=_app_event_payload(_app_view(app))))
    return view


async def deploy_app_or_propose(
    workspace_id: str, user_id: str, app_id: str
) -> DeployView | DestroyProposalView:
    """Deploy the app — unless it is PROD-flagged, in which case propose it.

    The agent-facing seam (SHIP-4). A non-prod deploy is reversible and runs
    directly; a production deploy is a gated verb, so this files an Instinct
    proposal instead and returns the proposal view. The HTTP route keeps calling
    ``deploy_app`` directly — an operator hitting the API with their own
    credentials is not the same actor as an agent acting on their behalf.
    """
    app = await _require_app(workspace_id, app_id)
    if not app.prod:
        return await deploy_app(workspace_id, user_id, app_id)

    _require_deployable_source(app)
    proposal_id = await propose.propose_ship_action(
        workspace_id=workspace_id,
        verb="deploy_app",
        box_id=app.box_id,
        app_id=str(app.id),
        target_label=f"app {app.name} (production)",
        params={"image": app.image},
        requested_by=user_id,
    )
    # A prod deploy is a gated verb, not a teardown — reuse the proposal VIEW
    # (the wire shape is identical: what was proposed, on what, under which id)
    # but do NOT emit ``ShipDestroyProposed``, which would tell every listener a
    # teardown is pending when nothing is being torn down.
    # no-event: the propose helper already opened the Decision-Graph chain; a
    # dedicated ship.deploy_proposed event lands with the console work that
    # renders it.
    return DestroyProposalView(
        workspace_id=workspace_id,
        target_kind="app",
        target_id=str(app.id),
        proposal_id=proposal_id,
    )


async def list_deploys(workspace_id: str, app_id: str) -> list[DeployView]:
    """One app's deploy attempts, newest first."""
    # no-event: read-only path; emit only on writes (cloud rule #9).
    app = await _require_app(workspace_id, app_id)
    return [_deploy_view(d) for d in await store.list_deploys(workspace_id, str(app.id))]


async def add_domain(
    workspace_id: str,
    user_id: str,
    app_id: str,
    body: AddDomainRequest,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> DomainView:
    """Route a domain to the app and (by default) issue TLS for it."""
    body = AddDomainRequest.model_validate(body)
    app, box = await _require_app_on_ready_box(workspace_id, app_id)
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            result = await session.engine.add_domain(
                app.name, body.domain, enable_tls=body.enable_tls
            )
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict("ship.domain_failed", exc) from exc

    scheme = "https" if result.tls_enabled else "http"
    app = await store.record_app_domain(
        app,
        domain=result.domain,
        tls_enabled=result.tls_enabled,
        url=f"{scheme}://{result.domain}",
    )
    await emit(ShipAppUpdated(data={**_app_event_payload(_app_view(app)), "user_id": user_id}))
    return DomainView(
        workspace_id=workspace_id,
        app_id=str(app.id),
        domain=result.domain,
        tls_enabled=result.tls_enabled,
    )


async def list_domains(workspace_id: str, app_id: str) -> list[DomainView]:
    """The domains currently routed to the app (as recorded at add time)."""
    # no-event: read-only path; emit only on writes (cloud rule #9).
    app = await _require_app(workspace_id, app_id)
    return [
        DomainView(
            workspace_id=workspace_id,
            app_id=str(app.id),
            domain=d.domain,
            tls_enabled=d.tls_enabled,
        )
        for d in app.domains
    ]


async def create_db(
    workspace_id: str,
    user_id: str,
    app_id: str,
    body: CreateDbRequest,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> DbView:
    """Create a database service and link it to the app.

    Only the NAME of the injected connection-string variable is recorded — the
    connection string itself stays on the box.
    """
    body = CreateDbRequest.model_validate(body)
    app, box = await _require_app_on_ready_box(workspace_id, app_id)
    service = (body.service or f"{app.name}-db").strip()
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            result = await session.engine.db_create(app.name, service, body.db_type)
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict("ship.db_failed", exc) from exc

    app = await store.record_app_db(
        app, service=result.service, env_var=result.exposed_env_var, db_type=body.db_type
    )
    await emit(ShipAppUpdated(data={**_app_event_payload(_app_view(app)), "user_id": user_id}))
    return DbView(
        workspace_id=workspace_id,
        app_id=str(app.id),
        linked_app=result.linked_app,
        service=result.service,
        env_var=result.exposed_env_var,
    )


async def set_scale(
    workspace_id: str,
    user_id: str,
    app_id: str,
    body: SetScaleRequest,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> AppView:
    """Set the app's per-process container counts (SHIP-17, ``ps:scale``)."""
    body = SetScaleRequest.model_validate(body)
    app, box = await _require_app_on_ready_box(workspace_id, app_id)
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            await session.engine.scale(app.name, body.scale)
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict("ship.scale_failed", exc) from exc

    app = await store.set_app_scale(app, scale=body.scale)
    view = _app_view(app)
    await emit(ShipAppUpdated(data={**_app_event_payload(view), "user_id": user_id}))
    return view


async def set_checks(
    workspace_id: str,
    user_id: str,
    app_id: str,
    body: SetChecksRequest,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> AppView:
    """Configure zero-downtime deploy checks (SHIP-17, Dokku ``checks``)."""
    body = SetChecksRequest.model_validate(body)
    app, box = await _require_app_on_ready_box(workspace_id, app_id)
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            await session.engine.set_healthcheck(
                app.name, enabled=body.zero_downtime, path=body.healthcheck_path
            )
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict("ship.checks_failed", exc) from exc

    app = await store.set_app_checks(
        app, zero_downtime=body.zero_downtime, healthcheck_path=body.healthcheck_path
    )
    view = _app_view(app)
    await emit(ShipAppUpdated(data={**_app_event_payload(view), "user_id": user_id}))
    return view


async def set_resources(
    workspace_id: str,
    user_id: str,
    app_id: str,
    body: SetResourcesRequest,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> AppView:
    """Set the app's CPU/memory ceilings (SHIP-18, Dokku ``resource:limit``)."""
    body = SetResourcesRequest.model_validate(body)
    app, box = await _require_app_on_ready_box(workspace_id, app_id)
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            await session.engine.set_resources(app.name, cpu=body.cpu, memory_mb=body.memory_mb)
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict("ship.resources_failed", exc) from exc

    app = await store.set_app_resources(app, cpu=body.cpu, memory_mb=body.memory_mb)
    view = _app_view(app)
    await emit(ShipAppUpdated(data={**_app_event_payload(view), "user_id": user_id}))
    return view


async def create_volume(
    workspace_id: str,
    user_id: str,
    app_id: str,
    body: CreateVolumeRequest,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> AppView:
    """Create a persistent volume and mount it into the app (SHIP-18)."""
    body = CreateVolumeRequest.model_validate(body)
    app, box = await _require_app_on_ready_box(workspace_id, app_id)
    name = (body.name or f"{app.name}-data").strip()
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            result = await session.engine.create_volume(
                app.name, name=name, mount_path=body.mount_path
            )
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict("ship.volume_failed", exc) from exc

    app = await store.record_app_volume(
        app, name=result.name, mount_path=result.mount_path, host_path=result.host_path
    )
    view = _app_view(app)
    await emit(ShipAppUpdated(data={**_app_event_payload(view), "user_id": user_id}))
    return view


async def restart_app(
    workspace_id: str,
    user_id: str,
    app_id: str,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> LifecycleView:
    """Restart the app's containers (SHIP-18, ``ps:restart``). Reversible bounce —
    no persisted state changes, so no store write."""
    return await _lifecycle(workspace_id, app_id, action="restart", session_factory=session_factory)


async def rebuild_app(
    workspace_id: str,
    user_id: str,
    app_id: str,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> LifecycleView:
    """Rebuild the app from its source/image and restart it (SHIP-18,
    ``ps:rebuild``). Reversible — no persisted state changes."""
    return await _lifecycle(workspace_id, app_id, action="rebuild", session_factory=session_factory)


async def _lifecycle(
    workspace_id: str,
    app_id: str,
    *,
    action: str,
    session_factory: ship_engine.BoxSessionFactory | None,
) -> LifecycleView:
    """Shared body for the reversible lifecycle verbs (restart / rebuild).

    Both drive the engine and return a confirmation; neither persists state, so
    there is nothing to store and no ShipAppUpdated to emit (the app's recorded
    config is unchanged — a follow-up ``GET`` reflects the live status).
    """
    app, box = await _require_app_on_ready_box(workspace_id, app_id)
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            engine_verb = session.engine.restart if action == "restart" else session.engine.rebuild
            result = await engine_verb(app.name)
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict(f"ship.{action}_failed", exc) from exc

    return LifecycleView(workspace_id=workspace_id, app_id=str(app.id), action=result.action)


async def get_logs(
    workspace_id: str,
    app_id: str,
    *,
    num: int = DEFAULT_LOG_LINES,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> LogsView:
    """Read the app's most recent log lines (already redacted by the driver)."""
    # no-event: read-only path; emit only on writes (cloud rule #9).
    app, box = await _require_app_on_ready_box(workspace_id, app_id)
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            chunk = await session.engine.logs(app.name, num=num)
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict("ship.logs_failed", exc) from exc
    return LogsView(workspace_id=workspace_id, app_id=str(app.id), lines=tuple(chunk.lines))


async def get_app_metrics(
    workspace_id: str,
    app_id: str,
    *,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> AppMetricsView:
    """Read one app's live health: process state + REAL per-container CPU/mem.

    Dokku's ``ps:report`` gives only deploy/run STATE; the driver adds real
    CPU%/mem% from ``docker stats`` (``None`` when the box can't report them, so
    the view shows "—" not a false 0). A box that is not ``ready`` is a 409, not
    a fabricated row — mirrors ``get_box_metrics``.
    """
    # no-event: read-only path; emit only on writes (cloud rule #9).
    app, box = await _require_app_on_ready_box(workspace_id, app_id)
    factory = session_factory or ship_engine.box_session
    try:
        async with factory(box) as session:
            snap = await session.engine.metrics(app.name)
    except ship_engine.ENGINE_FAILURES as exc:
        raise _engine_conflict("ship.metrics_failed", exc) from exc
    return AppMetricsView(
        workspace_id=workspace_id,
        app_id=str(app.id),
        deployed=snap.deployed,
        running=snap.running,
        processes=snap.processes,
        cpu=snap.cpu_pct,
        mem=snap.mem_pct,
        disk=snap.disk_used_pct if snap.disk_used_pct >= 0 else None,
    )


async def request_app_destroy(workspace_id: str, user_id: str, app_id: str) -> DestroyProposalView:
    """PARK an app teardown for human approval. Destroys NOTHING."""
    app = await _require_app(workspace_id, app_id)
    # File a REAL Instinct proposal (SHIP-4). Nothing is destroyed here — only
    # the approve path may call the engine's ``destroy`` verb. An app that
    # already carries a pending proposal reuses it rather than filing a duplicate.
    if app.pending_destroy_proposal_id:
        proposal_id = app.pending_destroy_proposal_id
    else:
        proposal_id = await propose.propose_ship_action(
            workspace_id=workspace_id,
            verb="destroy_app",
            box_id=app.box_id,
            app_id=str(app.id),
            target_label=f"app {app.name}",
            requested_by=user_id,
        )
    app = await store.park_app_destroy(app, proposal_id=proposal_id)
    return await _emit_destroy_proposal(
        workspace_id, user_id, kind="app", target_id=str(app.id), proposal_id=proposal_id
    )


# --------------------------------------------------------------------------- #
# App env (SHIP-9). Every function is workspace-scoped (a foreign id 404s via
# ``_require_app``); values are masked on the way out and never decrypted here —
# the sole decryption is ``store.decrypt_app_env`` at deploy time.
# --------------------------------------------------------------------------- #


async def get_app_env(workspace_id: str, app_id: str) -> list[EnvVarView]:
    """The app's env vars, masked. Read-only."""
    # no-event: read-only path; emit only on writes (cloud rule #9).
    app = await _require_app(workspace_id, app_id)
    return _env_views(app)


async def set_app_env(
    workspace_id: str, user_id: str, app_id: str, body: SetEnvRequest
) -> list[EnvVarView]:
    """Upsert a batch of env vars (add new, overwrite existing). Returns the
    resulting masked env."""
    body = SetEnvRequest.model_validate(body)
    app = await _require_app(workspace_id, app_id)
    app = await _apply_env_writes(app, body.vars)
    await emit(ShipAppUpdated(data={**_app_event_payload(_app_view(app)), "user_id": user_id}))
    return _env_views(app)


async def import_app_env(
    workspace_id: str, user_id: str, app_id: str, body: ImportEnvRequest
) -> list[EnvVarView]:
    """Parse a ``.env`` blob and upsert every valid line. Invalid keys are
    skipped (not a 422) so one stray line never rejects a bulk paste."""
    body = ImportEnvRequest.model_validate(body)
    app = await _require_app(workspace_id, app_id)
    app = await _apply_env_writes(app, _dotenv_to_vars(body.dotenv))
    await emit(ShipAppUpdated(data={**_app_event_payload(_app_view(app)), "user_id": user_id}))
    return _env_views(app)


async def delete_app_env(
    workspace_id: str, user_id: str, app_id: str, key: str
) -> list[EnvVarView]:
    """Remove one env var. Idempotent — deleting an absent key returns the
    unchanged (masked) env."""
    app = await _require_app(workspace_id, app_id)
    app = await store.delete_app_env(app, key)
    await emit(ShipAppUpdated(data={**_app_event_payload(_app_view(app)), "user_id": user_id}))
    return _env_views(app)


# --------------------------------------------------------------------------- #
# Wire mapping (ee/cloud rule 8 — mapping lives in service.py)
# --------------------------------------------------------------------------- #


def box_to_wire(view: BoxView) -> BoxOut:
    return BoxOut(
        id=view.id,
        provider=view.provider,
        ip=view.ip,
        status=view.status,  # type: ignore[arg-type] — the doc's Literal is the same set
        price_monthly=view.price_monthly,
    )


def app_to_wire(view: AppView) -> AppOut:
    return AppOut(
        id=view.id,
        name=view.name,
        box_id=view.box_id,
        status=view.status,
        urls=list(view.urls),
        source_kind=view.source_kind,  # type: ignore[arg-type] — the doc's Literal is the same set
        repo_url=view.repo_url,
        repo_ref=view.repo_ref,
        databases=[DatabaseOut(name=n, db_type=t, env_var=v) for (n, t, v) in view.databases],
        scale=dict(view.scale),
        zero_downtime=view.zero_downtime,
        healthcheck_path=view.healthcheck_path,
        volumes=[VolumeOut(name=n, mount_path=m, host_path=h) for (n, m, h) in view.volumes],
        cpu_limit=view.cpu_limit,
        memory_limit_mb=view.memory_limit_mb,
    )


def lifecycle_to_wire(view: LifecycleView) -> LifecycleOut:
    return LifecycleOut(app_id=view.app_id, action=view.action)  # type: ignore[arg-type]


def deploy_to_wire(view: DeployView) -> DeployOut:
    return DeployOut(
        id=view.id,
        app_id=view.app_id,
        status=view.status,  # type: ignore[arg-type] — the doc's Literal is the same set
        started_at=view.started_at,
        finished_at=view.finished_at,
    )


def logs_to_wire(view: LogsView) -> LogsOut:
    return LogsOut(lines=list(view.lines))


def metrics_to_wire(view: BoxMetricsView) -> MetricsOut:
    return MetricsOut(cpu=view.cpu, mem=view.mem, disk=view.disk)


def app_metrics_to_wire(view: AppMetricsView) -> AppMetricsOut:
    return AppMetricsOut(
        deployed=view.deployed,
        running=view.running,
        processes=view.processes,
        cpu=view.cpu,
        mem=view.mem,
        disk=view.disk,
    )


def domain_to_wire(view: DomainView) -> DomainOut:
    return DomainOut(domain=view.domain, tls_enabled=view.tls_enabled)


def domains_to_wire(views: list[DomainView]) -> DomainListOut:
    return DomainListOut(domains=[domain_to_wire(v) for v in views])


def db_to_wire(view: DbView) -> DbOut:
    return DbOut(service=view.service, linked_app=view.linked_app, env_var=view.env_var)


def proposal_to_wire(view: DestroyProposalView) -> PendingApprovalOut:
    return PendingApprovalOut(proposal_id=view.proposal_id)


def env_to_wire(views: list[EnvVarView]) -> EnvOut:
    return EnvOut(
        vars=[
            EnvVarOut(key=v.key, masked_value=v.masked_value, scope=v.scope)  # type: ignore[arg-type]
            for v in views
        ]
    )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _box_view(box: ShipBox) -> BoxView:
    return BoxView(
        id=BoxId(str(box.id)),
        workspace_id=box.workspace,
        provider=box.provider,
        ip=box.ip,
        status=box.status,
        price_monthly=box.price_monthly,
        pending_destroy_proposal_id=box.pending_destroy_proposal_id,
    )


def _app_view(app: ShipApp) -> AppView:
    return AppView(
        id=AppId(str(app.id)),
        workspace_id=app.workspace,
        box_id=app.box_id,
        name=app.name,
        status=app.status,
        build_path=app.build_path,
        git_ref=app.git_ref,
        image=app.image,
        prod=app.prod,
        urls=tuple(app.urls),
        env_refs=tuple(app.env_refs),
        source_kind=app.source_kind,
        repo_url=app.repo_url,
        repo_ref=app.repo_ref,
        databases=tuple((d.name, d.db_type, d.env_var) for d in app.databases),
        scale=dict(app.scale),
        zero_downtime=app.zero_downtime,
        healthcheck_path=app.healthcheck_path,
        volumes=tuple((v.name, v.mount_path, v.host_path) for v in app.volumes),
        cpu_limit=app.cpu_limit,
        memory_limit_mb=app.memory_limit_mb,
        pending_destroy_proposal_id=app.pending_destroy_proposal_id,
    )


def _deploy_view(deploy: ShipDeploy) -> DeployView:
    return DeployView(
        id=DeployId(str(deploy.id)),
        workspace_id=deploy.workspace,
        app_id=deploy.app_id,
        status=deploy.status,
        started_at=deploy.started_at,
        finished_at=deploy.finished_at,
        image=deploy.image,
        log_summary=deploy.log_summary,
    )


def _app_event_payload(view: AppView) -> dict:
    """Event payload for an app write — ids + status + URLs, never secrets."""
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "box_id": view.box_id,
        "name": view.name,
        "status": view.status,
        "urls": list(view.urls),
    }


# The mask NEVER reveals enough to reconstruct a secret: a short value is fully
# hidden, a longer one shows only a 3-char suffix so an operator can tell two
# secrets apart without seeing either. Masking is the service invariant that
# keeps plaintext off every response — the mask is computed here, at write time,
# and stored; the read path only ever echoes it.
_MASK_HIDDEN = "••••••"
_MASK_MIN_LEN = 6


def _mask_env_value(value: str) -> str:
    if len(value) <= _MASK_MIN_LEN:
        return _MASK_HIDDEN
    return f"…{value[-3:]}"


async def _apply_env_writes(app: ShipApp, vars_in: list[EnvVarIn]) -> ShipApp:
    """Mask + hand a validated batch to the store (which encrypts + persists)."""
    writes = [
        store.EnvVarWrite(key=v.key, masked=_mask_env_value(v.value), scope=v.scope, value=v.value)
        for v in vars_in
    ]
    return await store.upsert_app_env(app, writes)


def _dotenv_to_vars(blob: str) -> list[EnvVarIn]:
    """Parse a ``.env`` blob into validated ``EnvVarIn``. Blank lines and ``#``
    comments are ignored; each line splits on the FIRST ``=``; surrounding quotes
    are stripped. A line whose key fails the POSIX-name grammar (or whose value
    fails validation) is SKIPPED — ``EnvVarIn`` is the single source of that
    grammar, so the parser never re-implements it.
    """
    out: list[EnvVarIn] = []
    for raw in blob.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):  # a common .env nicety
            key = key[len("export ") :].strip()
        value = _strip_surrounding_quotes(value.strip())
        try:
            out.append(EnvVarIn(key=key, value=value))
        except PydanticValidationError:
            continue  # skip an invalid line; a bulk paste shouldn't 422
    return out


def _strip_surrounding_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _env_views(app: ShipApp) -> list[EnvVarView]:
    """Masked views from the app's stored env (sorted, stable order). Reads the
    stored ``masked`` hint — NEVER decrypts."""
    return [
        EnvVarView(
            workspace_id=app.workspace,
            app_id=str(app.id),
            key=key,
            masked_value=var.masked,
            scope=var.scope,
        )
        for key, var in sorted(app.env_vars.items())
    ]


async def _require_box(workspace_id: str, box_id: str) -> ShipBox:
    """Load a box or 404. A cross-tenant id is indistinguishable from a missing
    one — the store's tenant filter returns None either way."""
    box = await store.get_box(workspace_id, box_id)
    if box is None:
        raise NotFound("ship.box", box_id)
    return box


async def _require_app(workspace_id: str, app_id: str) -> ShipApp:
    """Load an app or 404 (same tenant-filter collapse as ``_require_box``)."""
    app = await store.get_app(workspace_id, app_id)
    if app is None:
        raise NotFound("ship.app", app_id)
    return app


def _require_deployable_source(app: ShipApp) -> None:
    """Reject a deploy for an app that has nothing to ship (SHIP-14).

    A ``git`` app needs a ``repo_url``; an ``image`` app needs an ``image``. The
    two error codes stay distinct so the console can guide the fix precisely.
    """
    if app.source_kind == "git":
        if not app.repo_url:
            raise ValidationError(
                "ship.app_no_source",
                "The app has no repo to deploy — set a git source first",
            )
    elif not app.image:
        raise ValidationError(
            "ship.app_no_image",
            "The app has no image to deploy — set one when creating it",
        )


async def _require_app_on_ready_box(workspace_id: str, app_id: str) -> tuple[ShipApp, ShipBox]:
    """Load an app together with its box, refusing a box that cannot answer."""
    app = await _require_app(workspace_id, app_id)
    box = await _require_box(workspace_id, app.box_id)
    if box.status != "ready":
        raise ConflictError("ship.box_not_ready", f"Box is {box.status}, not ready")
    return app, box


async def _emit_destroy_proposal(
    workspace_id: str, user_id: str, *, kind: str, target_id: str, proposal_id: str
) -> DestroyProposalView:
    view = DestroyProposalView(
        workspace_id=workspace_id,
        target_kind=kind,
        target_id=target_id,
        proposal_id=proposal_id,
    )
    await emit(
        ShipDestroyProposed(
            data={
                "workspace_id": workspace_id,
                "user_id": user_id,
                "target_kind": kind,
                "target_id": target_id,
                "proposal_id": proposal_id,
            }
        )
    )
    return view


def _engine_conflict(code: str, exc: BaseException) -> ConflictError:
    """Map an engine/reachability failure to a 409 with a safe reason.

    SHIP-1 redacts ``CommandFailed``'s command + stderr tail before the exception
    exists, so the tail is safe to surface; anything else (an unreachable box, a
    timeout) is reported by class name only — a raw ``OSError`` message can carry
    the box's address, which is not this response's job to publish.
    """
    detail = exc.stderr_tail if isinstance(exc, CommandFailed) else type(exc).__name__
    logger.warning("ship engine call failed (%s)", code)
    return ConflictError(code, f"The deploy engine refused the request: {detail}"[:500])
