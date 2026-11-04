# ee/pocketpaw_ee/sites/dashboard_router.py — REST surface of Sites AI dashboards, under
# /sites/by-pocket/{pocket_id}/data/dashboards (the pinned wire contract in
# ``site_data.dashboard_dto``):
#
#   GET    ""                               DashboardList
#   POST   ""                {name}         SiteDashboardOut                (manage)
#   GET    /{id}                            SiteDashboardOut
#   PATCH  /{id}   If-Match: <version>   {name?, widgets?}  SiteDashboardOut  (manage)
#   DELETE /{id}                            204                             (manage)
#   POST   /{id}/run         {widget_ids?, force?}  RunOut
#   POST   /{id}/widgets:test  {sql, viz, chart_type?, mapping}  TestWidgetOut  (manage)
#   GET    /{id}/health                     HealthOut
#   GET    /{id}/revisions                  RevisionsOut
#   POST   /{id}/revisions/{v}:restore      SiteDashboardOut                (manage)
#
# Thin handlers over ``sites.dashboard_service``. Every route takes ``request_context`` +
# ``fabric.read``; "manage" routes also require ``sites.dashboard_manage`` (ADMIN), and
# every dashboard reports ``can_manage`` for the caller. Bodies over 1 MB are 413. A
# PATCH without ``If-Match`` is 428 ``precondition_required``.
#
# Mounting: ``sites.router`` includes this router right after the Data router and
# BEFORE its own routes, so ``.../data/dashboards`` isn't taken by the legacy
# ``.../data/{table}`` read.
"""Sites AI dashboard routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import action_allowed_any_workspace, require_action_any_workspace
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.sites import dashboard_service as svc
from pocketpaw_ee.sites.data_router import _write_body_limit
from pocketpaw_ee.sites.site_data.dashboard_dto import (
    CreateDashboardBody,
    DashboardList,
    HealthOut,
    PatchDashboardBody,
    RevisionsOut,
    RunBody,
    RunOut,
    SiteDashboardOut,
    TestWidgetBody,
    TestWidgetOut,
)

router = APIRouter(tags=["Sites"])

BASE = "/sites/by-pocket/{pocket_id}/data/dashboards"
_READ = require_action_any_workspace("fabric.read")
_MANAGE = require_action_any_workspace("sites.dashboard_manage")
_CAN_MANAGE = action_allowed_any_workspace("sites.dashboard_manage")


def _ids(ctx: RequestContext, pocket_id: str) -> dict[str, str]:
    return {"workspace_id": ctx.workspace_id or "", "user_id": ctx.user_id, "pocket_id": pocket_id}


def _version(if_match: str | None) -> int:
    text = (if_match or "").strip().removeprefix("W/").strip('"').strip()
    if not text.isdigit():
        raise CloudError(
            428, "precondition_required", "Send If-Match with the version you last read."
        )
    return int(text)


@router.get(BASE, response_model=DashboardList)
async def list_site_dashboards(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    can_manage: bool = Depends(_CAN_MANAGE),
) -> DashboardList:
    """The site's dashboards, why none can run yet (``unavailable_reason``) and whether
    the caller may create or generate one (``can_manage``)."""
    out = await svc.list_dashboards(**_ids(ctx, pocket_id))
    return out.model_copy(update={"can_manage": can_manage})


@router.post(BASE, response_model=SiteDashboardOut)
async def create_site_dashboard(
    pocket_id: str,
    body: CreateDashboardBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_MANAGE),
    ___: None = Depends(_write_body_limit),
) -> SiteDashboardOut:
    """A blank dashboard."""
    doc = await svc.create_dashboard(**_ids(ctx, pocket_id), name=body.name)
    return svc.dashboard_out(doc, True)


@router.get(BASE + "/{dashboard_id}", response_model=SiteDashboardOut)
async def get_site_dashboard(
    pocket_id: str,
    dashboard_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    can_manage: bool = Depends(_CAN_MANAGE),
) -> SiteDashboardOut:
    doc = await svc.get_dashboard(**_ids(ctx, pocket_id), dashboard_id=dashboard_id)
    return svc.dashboard_out(doc, can_manage)


@router.patch(BASE + "/{dashboard_id}", response_model=SiteDashboardOut)
async def patch_site_dashboard(
    pocket_id: str,
    dashboard_id: str,
    body: PatchDashboardBody,
    if_match: str | None = Header(default=None, alias="If-Match"),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_MANAGE),
    ___: None = Depends(_write_body_limit),
) -> SiteDashboardOut:
    """Rename and / or replace the full widget list; 409 ``version_conflict`` with the
    current dashboard when ``If-Match`` is stale."""
    doc = await svc.patch_dashboard(
        **_ids(ctx, pocket_id),
        dashboard_id=dashboard_id,
        if_match=_version(if_match),
        name=body.name,
        widgets=body.widgets,
    )
    return svc.dashboard_out(doc, True)


@router.delete(BASE + "/{dashboard_id}", status_code=204)
async def delete_site_dashboard(
    pocket_id: str,
    dashboard_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_MANAGE),
) -> Response:
    await svc.delete_dashboard(**_ids(ctx, pocket_id), dashboard_id=dashboard_id)
    return Response(status_code=204)


@router.post(BASE + "/{dashboard_id}/run", response_model=RunOut)
async def run_site_dashboard(
    pocket_id: str,
    dashboard_id: str,
    body: RunBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    can_manage: bool = Depends(_CAN_MANAGE),
) -> RunOut:
    """Every (or the named) widget's result, from the shared cache; ``force`` counts
    only for a manager."""
    return await svc.run_dashboard(
        **_ids(ctx, pocket_id),
        dashboard_id=dashboard_id,
        widget_ids=body.widget_ids,
        force=body.force,
        can_manage=can_manage,
    )


@router.post(BASE + "/{dashboard_id}/widgets:test", response_model=TestWidgetOut)
async def test_site_dashboard_widget(
    pocket_id: str,
    dashboard_id: str,
    body: TestWidgetBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_MANAGE),
    ___: None = Depends(_write_body_limit),
) -> TestWidgetOut:
    """Run a draft widget once: result, status and spec."""
    return await svc.test_widget(
        **_ids(ctx, pocket_id),
        dashboard_id=dashboard_id,
        sql=body.sql,
        viz=body.viz,
        chart_type=body.chart_type,
        mapping=body.mapping,
    )


@router.get(BASE + "/{dashboard_id}/health", response_model=HealthOut)
async def site_dashboard_health(
    pocket_id: str,
    dashboard_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
) -> HealthOut:
    """Schema drift: whether the schema changed, broken widgets, new tables."""
    return await svc.health(**_ids(ctx, pocket_id), dashboard_id=dashboard_id)


@router.get(BASE + "/{dashboard_id}/revisions", response_model=RevisionsOut)
async def site_dashboard_revisions(
    pocket_id: str,
    dashboard_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
) -> RevisionsOut:
    return await svc.list_revisions(**_ids(ctx, pocket_id), dashboard_id=dashboard_id)


@router.post(BASE + "/{dashboard_id}/revisions/{version}:restore", response_model=SiteDashboardOut)
async def restore_site_dashboard_revision(
    pocket_id: str,
    dashboard_id: str,
    version: int,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_MANAGE),
) -> SiteDashboardOut:
    doc = await svc.restore_revision(
        **_ids(ctx, pocket_id), dashboard_id=dashboard_id, version=version
    )
    return svc.dashboard_out(doc, True)
