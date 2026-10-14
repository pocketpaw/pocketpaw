# ee/pocketpaw_ee/integrations/router.py: the Sites integrations HTTP surface.
#
# ``router`` (owner routes: session + workspace RBAC + the ``sites`` plan feature):
#   GET    /integrations/providers                       ProviderView[]
#   GET    /integrations/connections                     ConnectionView[]
#   POST   /integrations/{provider}/connect              {pocket_id?} -> {authorize_url}
#   POST   /integrations/connections/{id}/resume         {authorize_url} (creator or admin)
#   DELETE /integrations/connections/{id}                revoke (creator or admin)
#   GET    /sites/by-pocket/{pocket_id}/integrations     SiteIntegrationView[]
#   PUT    /sites/by-pocket/{pocket_id}/integrations/{provider}
#   DELETE /sites/by-pocket/{pocket_id}/integrations/{provider}   204, grant survives
#   GET    /sites/by-pocket/{pocket_id}/integrations/newsletter/export.csv  (owner)
#   GET    /sites/by-pocket/{pocket_id}/orders?limit&cursor
#
# ``public_router`` (no session by design; each names its own credential):
#   GET  /integrations/{provider}/start      one-time ticket -> browser cookie -> provider
#                                            (refused when the browser's API session
#                                            is someone else's)
#   GET  /integrations/{provider}/callback   single-use state + the matching cookie
#   POST /sites/{site_id}/x/{provider}/{action}   site key or PAW_GATEWAY_TOKEN
#   POST /integrations/webhooks/{provider}   provider signature
#   GET  /integrations/social/{provider}/authorize   visitor sign-in consent page
#                                            (site binding + own-host return_to)
#   POST /integrations/social/{provider}/authorize   Continue / Cancel from that page
#                                            (single-use token + SameSite=Strict cookie)
#   GET  /integrations/social/{provider}/callback    single-use broker state
#   POST /integrations/social/redeem          PAW_GATEWAY_TOKEN + ticket + nonce
#                                            (all three: social_broker.py)
#
# Wire shapes are pinned by the Wave 0 contract and its Wave 1 addendum; see dto.py. Mounted in
# ``cloud.mount_cloud``; both routers are in tests/cloud/auth/test_route_auth_audit.py.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import (
    action_allowed_any_workspace,
    require_action_any_workspace,
    require_plan_feature,
)
from pocketpaw_ee.integrations import bindings, gateway, oauth, social_broker, vault, webhooks
from pocketpaw_ee.integrations.dto import (
    ConnectBody,
    ConnectionView,
    ConnectResponse,
    OrdersPage,
    ProviderView,
    SiteIntegrationPut,
    SiteIntegrationView,
)

router = APIRouter(tags=["Integrations"], dependencies=[Depends(require_plan_feature("sites"))])
public_router = APIRouter(tags=["Integrations"])

_SITE = "/sites/by-pocket/{pocket_id}"


@router.get("/integrations/providers", response_model=list[ProviderView])
async def list_providers(
    _ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> list[ProviderView]:
    return bindings.list_provider_views()


@router.get("/integrations/connections", response_model=list[ConnectionView])
async def list_connections(
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> list[ConnectionView]:
    rows = await vault.list_connections(ctx.workspace_id or "")
    return [vault.to_view(r, await bindings.site_count(str(r.id))) for r in rows]


@router.post("/integrations/{provider}/connect", response_model=ConnectResponse)
async def connect(
    provider: str,
    body: ConnectBody | None = None,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> ConnectResponse:
    url = await oauth.start_connect(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        provider_id=provider,
        pocket_id=(body.pocket_id if body else None) or None,
    )
    return ConnectResponse(authorize_url=url)


@router.post("/integrations/connections/{connection_id}/resume", response_model=ConnectResponse)
async def resume_connection(
    connection_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
    is_admin: bool = Depends(action_allowed_any_workspace("workspace.update")),
) -> ConnectResponse:
    """Finish an unfinished provider setup, or reconnect a needs_reconnect grant."""
    url = await oauth.start_resume(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        connection_id=connection_id,
        is_admin=is_admin,
    )
    return ConnectResponse(authorize_url=url)


@router.delete("/integrations/connections/{connection_id}", status_code=204)
async def revoke_connection(
    connection_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
    is_admin: bool = Depends(action_allowed_any_workspace("workspace.update")),
) -> Response:
    await bindings.revoke_connection(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        connection_id=connection_id,
        is_admin=is_admin,
    )
    return Response(status_code=204)


@router.get(_SITE + "/integrations", response_model=list[SiteIntegrationView])
async def list_site_integrations(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> list[SiteIntegrationView]:
    return await bindings.list_for_pocket(
        workspace_id=ctx.workspace_id or "", user_id=ctx.user_id, pocket_id=pocket_id
    )


@router.get(_SITE + "/integrations/newsletter/export.csv")
async def export_newsletter(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> Response:
    from pocketpaw_ee.integrations.providers.newsletter import export_csv

    await bindings.require_owner(ctx.workspace_id or "", ctx.user_id, pocket_id)
    body = await export_csv(ctx.workspace_id or "", pocket_id)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="newsletter-subscribers.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.put(_SITE + "/integrations/{provider}", response_model=SiteIntegrationView)
async def put_site_integration(
    pocket_id: str,
    provider: str,
    body: SiteIntegrationPut,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
    is_admin: bool = Depends(action_allowed_any_workspace("workspace.update")),
) -> SiteIntegrationView:
    return await bindings.upsert(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        provider_id=provider,
        body=body,
        is_admin=is_admin,
    )


@router.delete(_SITE + "/integrations/{provider}", status_code=204)
async def delete_site_integration(
    pocket_id: str,
    provider: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> Response:
    await bindings.unlink(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        provider_id=provider,
    )
    return Response(status_code=204)


@router.get(_SITE + "/orders", response_model=OrdersPage)
async def list_orders(
    pocket_id: str,
    limit: int = Query(50, ge=1, le=webhooks.MAX_ORDERS_PAGE),
    cursor: str | None = None,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
    is_admin: bool = Depends(action_allowed_any_workspace("workspace.update")),
) -> OrdersPage:
    """Editors see customer emails masked; the pocket owner and admins see them whole
    (the same PII rule as the owner-only newsletter CSV)."""
    return await webhooks.list_orders(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        limit=limit,
        cursor=cursor,
        is_admin=is_admin,
    )


# ------------------------------------------------------------------ public


def _page(message: dict) -> HTMLResponse:
    page, nonce = oauth.callback_html(message)
    return HTMLResponse(
        page,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; base-uri 'none'; "
                "frame-ancestors 'none'"
            ),
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@public_router.get("/integrations/{provider}/start")
async def oauth_start(provider: str, request: Request, t: str = "") -> Response:
    """Redeem the one-time ticket, bind this browser with a cookie, go to the provider."""
    try:
        url, state_hash = await oauth.redeem_ticket(
            provider, t, request.cookies.get(oauth.SESSION_COOKIE)
        )
    except oauth.StartRefused as refused:
        return _page(
            {
                "type": oauth.MESSAGE_TYPE,
                "provider": provider,
                "connection_id": None,
                "status": "error",
                "error": refused.code,
            }
        )
    resp = RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
    resp.set_cookie(
        oauth.cookie_name(provider),
        state_hash,
        max_age=oauth.cookie_max_age(provider),
        path=oauth.COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resp


@public_router.get("/integrations/{provider}/callback", response_class=HTMLResponse)
async def oauth_callback(
    provider: str, request: Request, code: str = "", state: str = ""
) -> HTMLResponse:
    """Provider redirect. Needs the single-use state AND this browser's start cookie."""
    cookie = request.cookies.get(oauth.cookie_name(provider))
    message = await oauth.finish_callback(provider, code, state, cookie)
    resp = _page(message)
    resp.delete_cookie(
        oauth.cookie_name(provider),
        path=oauth.COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resp


@public_router.post("/sites/{site_id}/x/{provider}/{action}")
async def gateway_call(site_id: str, provider: str, action: str, request: Request) -> JSONResponse:
    """The site gateway. See gateway.py for the gate order."""
    return await gateway.call(request, site_id, provider, action)


@public_router.post("/integrations/webhooks/{provider}")
async def provider_webhook(provider: str, request: Request) -> JSONResponse:
    """Provider-signed webhook ingress. See webhooks.py."""
    return await webhooks.ingest(provider, request)


@public_router.get("/integrations/social/{provider}/authorize")
async def social_authorize(
    provider: str,
    request: Request,
    site_id: str = "",
    return_to: str = "",
    nonce: str = "",
    next: str = "/",  # noqa: A002 - the wire name
) -> Response:
    """Visitor sign-in for a project site: checks the binding and return_to, then
    shows a page naming the site; nothing goes to the provider before Continue."""
    return await social_broker.authorize(request, provider, site_id, return_to, nonce, next)


@public_router.post("/integrations/social/{provider}/authorize")
async def social_authorize_decide(provider: str, request: Request) -> Response:
    """Continue (-> provider) or Cancel (-> the site) from the consent page."""
    return await social_broker.decide(request, provider)


@public_router.get("/integrations/social/{provider}/callback")
async def social_callback(
    provider: str, request: Request, code: str = "", state: str = ""
) -> Response:
    """Provider redirect: single-use state -> ticket -> back to the site's callback."""
    return await social_broker.callback(request, provider, code, state)


@public_router.post("/integrations/social/redeem")
async def social_redeem(request: Request) -> JSONResponse:
    """The site Worker trades a ticket (+ its nonce) for the visitor's profile."""
    return await social_broker.redeem(request)
