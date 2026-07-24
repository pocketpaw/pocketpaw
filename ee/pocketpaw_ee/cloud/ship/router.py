# ee/pocketpaw_ee/cloud/ship/router.py — the thin FastAPI adapter for /ship
# (SHIP-3). Mounted by ``mount_cloud()`` as ``/api/v1/ship``.
#
# Tenancy comes ONLY from the RequestContext — never a body field, never a query
# param — so a caller cannot name another workspace. The service does the work;
# ``_core.http`` maps ``CloudError`` to JSON, so this module never raises
# ``HTTPException`` and never imports a Beanie document.
#
#   POST   /ship/boxes                  provision a box (202-style: pollable)
#   GET    /ship/boxes                  list the workspace's boxes
#   GET    /ship/boxes/{id}/metrics     live cpu / mem / disk for one box
#   DELETE /ship/boxes/{id}             PARK a teardown — never executes it
#   POST   /ship/apps                   register an app on a box (optional source)
#   GET    /ship/apps?box_id=           list apps (optionally one box's)
#   PUT    /ship/apps/{id}/source       point the app at a deploy source (git/image)
#   POST   /ship/apps/{id}/deploy       enqueue a deploy (empty body)
#   GET    /ship/apps/{id}/deploys      the app's deploy attempts, newest first
#   POST   /ship/apps/{id}/domains      route a domain + issue TLS
#   GET    /ship/apps/{id}/domains      the app's routed domains
#   POST   /ship/apps/{id}/db           create + link a database service
#   PUT    /ship/apps/{id}/scale        set per-process container counts
#   PUT    /ship/apps/{id}/checks       configure zero-downtime deploy checks
#   PUT    /ship/apps/{id}/resources    set cpu / memory ceilings
#   POST   /ship/apps/{id}/volumes      create + mount a persistent volume
#   POST   /ship/apps/{id}/restart      restart the app's containers
#   POST   /ship/apps/{id}/rebuild      rebuild from source and restart
#   GET    /ship/apps/{id}/logs         recent app log lines
#   GET    /ship/apps/{id}/env          list env vars (values MASKED)
#   PUT    /ship/apps/{id}/env          upsert a batch of env vars
#   POST   /ship/apps/{id}/env/import   import a .env blob
#   DELETE /ship/apps/{id}/env/{key}    remove one env var
#   DELETE /ship/apps/{id}              PARK a teardown — never executes it
#
# The two app/box DELETEs answer ``{"status": "pending_approval", ...}``. The env
# routes answer ``EnvOut`` — values are ALWAYS masked, never returned in the
# clear.
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.
# Changed 2026-07-23 (feat/ship-9-env-store, SHIP-9): added the four env routes.
# Changed 2026-07-23 (feat/ship-14-source-deploy, SHIP-14): added
# ``PUT /ship/apps/{id}/source`` — point an app at a git repo or a pre-built
# image; the response is masked (never echoes the private-repo token).
# Changed 2026-07-24 (feat/ship-18-ops, SHIP-18): added the Wave 3 ops routes —
# ``PUT .../resources`` (cpu/mem ceilings), ``POST .../volumes`` (persistent
# storage), ``POST .../restart`` + ``POST .../rebuild`` (reversible bounces, they
# answer ``LifecycleOut``, not the app).

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.ship import service as ship_service
from pocketpaw_ee.cloud.ship.dto import (
    AddDomainRequest,
    AppMetricsOut,
    AppOut,
    BoxOut,
    CreateAppRequest,
    CreateBoxRequest,
    CreateDbRequest,
    CreateVolumeRequest,
    DbOut,
    DeployOut,
    DomainListOut,
    DomainOut,
    EnvOut,
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
)

router = APIRouter(prefix="/ship", tags=["Ship"], dependencies=[Depends(require_license)])


def _require_workspace(ctx: RequestContext) -> str:
    """Every /ship route is workspace-scoped; fail closed without one."""
    if not ctx.workspace_id:
        raise Forbidden("ship.no_workspace", "No active workspace")
    return ctx.workspace_id


# ---------------------------------------------------------------------------
# Boxes
# ---------------------------------------------------------------------------


@router.post("/boxes", response_model=BoxOut)
async def create_box(
    body: CreateBoxRequest,
    ctx: RequestContext = Depends(request_context),
) -> BoxOut:
    """Provision a box. Returns immediately in ``provisioning``; poll the list."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.create_box(workspace_id, ctx.user_id, body)
    return ship_service.box_to_wire(view)


@router.get("/boxes", response_model=list[BoxOut])
async def list_boxes(ctx: RequestContext = Depends(request_context)) -> list[BoxOut]:
    workspace_id = _require_workspace(ctx)
    views = await ship_service.list_boxes(workspace_id)
    return [ship_service.box_to_wire(v) for v in views]


@router.get("/boxes/{box_id}/metrics", response_model=MetricsOut)
async def get_box_metrics(
    box_id: str,
    ctx: RequestContext = Depends(request_context),
) -> MetricsOut:
    """Live box health: CPU, memory and root-filesystem use, each 0.0–100.0."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.get_box_metrics(workspace_id, box_id)
    return ship_service.metrics_to_wire(view)


@router.delete("/boxes/{box_id}", response_model=PendingApprovalOut)
async def request_box_destroy(
    box_id: str,
    ctx: RequestContext = Depends(request_context),
) -> PendingApprovalOut:
    """PARK a box teardown for human approval. Destroys nothing."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.request_box_destroy(workspace_id, ctx.user_id, box_id)
    return ship_service.proposal_to_wire(view)


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------


@router.post("/apps", response_model=AppOut)
async def create_app(
    body: CreateAppRequest,
    ctx: RequestContext = Depends(request_context),
) -> AppOut:
    workspace_id = _require_workspace(ctx)
    view = await ship_service.create_app(workspace_id, ctx.user_id, body)
    return ship_service.app_to_wire(view)


@router.get("/apps", response_model=list[AppOut])
async def list_apps(
    box_id: str | None = Query(default=None),
    ctx: RequestContext = Depends(request_context),
) -> list[AppOut]:
    workspace_id = _require_workspace(ctx)
    views = await ship_service.list_apps(workspace_id, box_id=box_id)
    return [ship_service.app_to_wire(v) for v in views]


@router.put("/apps/{app_id}/source", response_model=AppOut)
async def set_app_source(
    app_id: str,
    body: SetSourceRequest,
    ctx: RequestContext = Depends(request_context),
) -> AppOut:
    """Point the app at a deploy source (git repo or pre-built image). The
    response is masked — the private-repo token is never echoed."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.set_app_source(workspace_id, ctx.user_id, app_id, body)
    return ship_service.app_to_wire(view)


@router.post("/apps/{app_id}/deploy", response_model=DeployOut)
async def deploy_app(
    app_id: str,
    ctx: RequestContext = Depends(request_context),
) -> DeployOut:
    """Enqueue a deploy. Takes no body — the app already carries its source."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.deploy_app(workspace_id, ctx.user_id, app_id)
    return ship_service.deploy_to_wire(view)


@router.get("/apps/{app_id}/deploys", response_model=list[DeployOut])
async def list_deploys(
    app_id: str,
    ctx: RequestContext = Depends(request_context),
) -> list[DeployOut]:
    workspace_id = _require_workspace(ctx)
    views = await ship_service.list_deploys(workspace_id, app_id)
    return [ship_service.deploy_to_wire(v) for v in views]


@router.post("/apps/{app_id}/domains", response_model=DomainOut)
async def add_domain(
    app_id: str,
    body: AddDomainRequest,
    ctx: RequestContext = Depends(request_context),
) -> DomainOut:
    workspace_id = _require_workspace(ctx)
    view = await ship_service.add_domain(workspace_id, ctx.user_id, app_id, body)
    return ship_service.domain_to_wire(view)


@router.get("/apps/{app_id}/domains", response_model=DomainListOut)
async def list_domains(
    app_id: str,
    ctx: RequestContext = Depends(request_context),
) -> DomainListOut:
    workspace_id = _require_workspace(ctx)
    views = await ship_service.list_domains(workspace_id, app_id)
    return ship_service.domains_to_wire(views)


@router.post("/apps/{app_id}/db", response_model=DbOut)
async def create_db(
    app_id: str,
    ctx: RequestContext = Depends(request_context),
    body: CreateDbRequest | None = None,
) -> DbOut:
    """Create a database service and link it. The body is optional — the service
    name defaults to ``<app-name>-db``."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.create_db(
        workspace_id, ctx.user_id, app_id, body or CreateDbRequest()
    )
    return ship_service.db_to_wire(view)


@router.put("/apps/{app_id}/scale", response_model=AppOut)
async def set_app_scale(
    app_id: str,
    body: SetScaleRequest,
    ctx: RequestContext = Depends(request_context),
) -> AppOut:
    """Set the app's per-process container counts (SHIP-17, ``ps:scale``)."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.set_scale(workspace_id, ctx.user_id, app_id, body)
    return ship_service.app_to_wire(view)


@router.put("/apps/{app_id}/checks", response_model=AppOut)
async def set_app_checks(
    app_id: str,
    body: SetChecksRequest,
    ctx: RequestContext = Depends(request_context),
) -> AppOut:
    """Configure zero-downtime deploy checks (SHIP-17, Dokku ``checks``)."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.set_checks(workspace_id, ctx.user_id, app_id, body)
    return ship_service.app_to_wire(view)


@router.put("/apps/{app_id}/resources", response_model=AppOut)
async def set_app_resources(
    app_id: str,
    body: SetResourcesRequest,
    ctx: RequestContext = Depends(request_context),
) -> AppOut:
    """Set the app's CPU/memory ceilings (SHIP-18, Dokku ``resource:limit``)."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.set_resources(workspace_id, ctx.user_id, app_id, body)
    return ship_service.app_to_wire(view)


@router.post("/apps/{app_id}/volumes", response_model=AppOut)
async def create_volume(
    app_id: str,
    body: CreateVolumeRequest,
    ctx: RequestContext = Depends(request_context),
) -> AppOut:
    """Create a persistent volume and mount it into the app (SHIP-18)."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.create_volume(workspace_id, ctx.user_id, app_id, body)
    return ship_service.app_to_wire(view)


@router.post("/apps/{app_id}/restart", response_model=LifecycleOut)
async def restart_app(
    app_id: str,
    ctx: RequestContext = Depends(request_context),
) -> LifecycleOut:
    """Restart the app's containers (SHIP-18, ``ps:restart``) — a reversible bounce."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.restart_app(workspace_id, ctx.user_id, app_id)
    return ship_service.lifecycle_to_wire(view)


@router.post("/apps/{app_id}/rebuild", response_model=LifecycleOut)
async def rebuild_app(
    app_id: str,
    ctx: RequestContext = Depends(request_context),
) -> LifecycleOut:
    """Rebuild the app from source and restart it (SHIP-18, ``ps:rebuild``)."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.rebuild_app(workspace_id, ctx.user_id, app_id)
    return ship_service.lifecycle_to_wire(view)


@router.get("/apps/{app_id}/logs", response_model=LogsOut)
async def get_logs(
    app_id: str,
    num: int = Query(default=ship_service.DEFAULT_LOG_LINES, ge=1, le=1000),
    ctx: RequestContext = Depends(request_context),
) -> LogsOut:
    workspace_id = _require_workspace(ctx)
    view = await ship_service.get_logs(workspace_id, app_id, num=num)
    return ship_service.logs_to_wire(view)


@router.get("/apps/{app_id}/metrics", response_model=AppMetricsOut)
async def get_app_metrics(
    app_id: str,
    ctx: RequestContext = Depends(request_context),
) -> AppMetricsOut:
    workspace_id = _require_workspace(ctx)
    view = await ship_service.get_app_metrics(workspace_id, app_id)
    return ship_service.app_metrics_to_wire(view)


# ---------------------------------------------------------------------------
# Env (SHIP-9) — values are ALWAYS masked on the way out
# ---------------------------------------------------------------------------


@router.get("/apps/{app_id}/env", response_model=EnvOut)
async def get_app_env(
    app_id: str,
    ctx: RequestContext = Depends(request_context),
) -> EnvOut:
    """The app's env vars, values masked."""
    workspace_id = _require_workspace(ctx)
    views = await ship_service.get_app_env(workspace_id, app_id)
    return ship_service.env_to_wire(views)


@router.put("/apps/{app_id}/env", response_model=EnvOut)
async def set_app_env(
    app_id: str,
    body: SetEnvRequest,
    ctx: RequestContext = Depends(request_context),
) -> EnvOut:
    """Upsert a batch of env vars (add new, overwrite existing). Returns the
    resulting masked env."""
    workspace_id = _require_workspace(ctx)
    views = await ship_service.set_app_env(workspace_id, ctx.user_id, app_id, body)
    return ship_service.env_to_wire(views)


@router.post("/apps/{app_id}/env/import", response_model=EnvOut)
async def import_app_env(
    app_id: str,
    body: ImportEnvRequest,
    ctx: RequestContext = Depends(request_context),
) -> EnvOut:
    """Import a ``.env`` blob (invalid lines skipped). Returns the masked env."""
    workspace_id = _require_workspace(ctx)
    views = await ship_service.import_app_env(workspace_id, ctx.user_id, app_id, body)
    return ship_service.env_to_wire(views)


@router.delete("/apps/{app_id}/env/{key}", response_model=EnvOut)
async def delete_app_env(
    app_id: str,
    key: str,
    ctx: RequestContext = Depends(request_context),
) -> EnvOut:
    """Remove one env var. Idempotent. Returns the masked env."""
    workspace_id = _require_workspace(ctx)
    views = await ship_service.delete_app_env(workspace_id, ctx.user_id, app_id, key)
    return ship_service.env_to_wire(views)


@router.delete("/apps/{app_id}", response_model=PendingApprovalOut)
async def request_app_destroy(
    app_id: str,
    ctx: RequestContext = Depends(request_context),
) -> PendingApprovalOut:
    """PARK an app teardown for human approval. Destroys nothing."""
    workspace_id = _require_workspace(ctx)
    view = await ship_service.request_app_destroy(workspace_id, ctx.user_id, app_id)
    return ship_service.proposal_to_wire(view)
