# ee/pocketpaw_ee/sites/secrets_router.py: REST surface for per-site secrets.
#
#   GET    /sites/by-pocket/{pocket_id}/secrets          names + status + pending
#   PUT    /sites/by-pocket/{pocket_id}/secrets/{name}   body {value}, owner only
#   DELETE /sites/by-pocket/{pocket_id}/secrets/{name}   owner only
#
# Thin handlers over ``sites.site_secrets``. No response ever carries a value. The
# workspace gate (``fabric.read`` / ``fabric.write`` plus the ``sites`` plan feature)
# runs here; the pocket gate (edit access to list, ownership to write, 404 outside the
# caller's workspace) runs in the service. Mounted in ``cloud.mount_cloud`` and
# audited by ``tests/cloud/auth/test_route_auth_audit.py``.
"""Per-site secret routes (names only on the way out)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.sites import site_secrets
from pocketpaw_ee.sites.site_secrets import SiteSecretPut, SiteSecretsView, SiteSecretView

router = APIRouter(
    tags=["Sites"],
    dependencies=[Depends(require_plan_feature("sites"))],
)

_BASE = "/sites/by-pocket/{pocket_id}/secrets"


@router.get(_BASE, response_model=SiteSecretsView)
async def list_site_secrets(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteSecretsView:
    """Every secret's name and status (``set`` / ``pending``) plus the pending
    requests the owner still has to fill. Needs edit access to the pocket."""
    return await site_secrets.list_secrets(
        workspace_id=ctx.workspace_id or "", user_id=ctx.user_id, pocket_id=pocket_id
    )


@router.put(_BASE + "/{name}", response_model=SiteSecretView)
async def put_site_secret(
    pocket_id: str,
    name: str,
    body: SiteSecretPut,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteSecretView:
    """Set or replace a secret's value (pocket owner only, 8 KB cap). The response
    is the name and status, never the value."""
    return await site_secrets.set_secret(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        name=name,
        value=body.value,
    )


@router.delete(_BASE + "/{name}", status_code=204)
async def delete_site_secret(
    pocket_id: str,
    name: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> Response:
    """Delete a secret or a pending request (pocket owner only)."""
    await site_secrets.delete_secret(
        workspace_id=ctx.workspace_id or "", user_id=ctx.user_id, pocket_id=pocket_id, name=name
    )
    return Response(status_code=204)
