# OAuth2 router — authorize, token, revoke.
# Created: 2026-02-20

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from pocketpaw.api.v1.schemas.oauth2 import RevokeRequest, TokenRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["OAuth2"])

_CONSENT_HTML = """<!DOCTYPE html>
<html><head><title>PocketPaw Authorization</title>
<style>
body {{ font-family: system-ui; max-width: 480px; margin: 40px auto; padding: 20px; }}
.btn {{ padding: 10px 24px; border: none; border-radius: 6px; cursor: pointer; font-size: 16px; }}
.allow {{ background: #2563eb; color: white; }} .allow:hover {{ background: #1d4ed8; }}
.deny {{ background: #e5e7eb; color: #374151; margin-left: 12px; }}
h2 {{ margin-bottom: 8px; }}
.scopes {{ background: #f3f4f6; padding: 12px; border-radius: 8px; margin: 16px 0; }}
.scope {{ display: inline-block; background: #dbeafe; padding: 4px 8px;
  border-radius: 4px; margin: 2px; font-size: 14px; }}
</style></head><body>
<h2>Authorize {client_name}</h2>
<p>This app wants to access your PocketPaw instance.</p>
<div class="scopes"><strong>Requested permissions:</strong><br>{scope_badges}</div>
<form method="POST" action="/api/v1/oauth/authorize/consent">
<input type="hidden" name="client_id" value="{client_id}">
<input type="hidden" name="redirect_uri" value="{redirect_uri}">
<input type="hidden" name="scope" value="{scope}">
<input type="hidden" name="code_challenge" value="{code_challenge}">
<input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
<input type="hidden" name="state" value="{state}">
<button type="submit" name="action" value="allow" class="btn allow">Allow</button>
<button type="submit" name="action" value="deny" class="btn deny">Deny</button>
</form></body></html>"""


@router.get("/oauth/authorize")
async def authorize(
    request: Request,
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query("chat sessions"),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
    state: str = Query(""),
):
    """Show OAuth2 consent screen for PKCE flow."""
    from pocketpaw.security.rate_limiter import auth_limiter

    client_ip = request.client.host if request.client else "unknown"
    if not auth_limiter.allow(client_ip):
        return JSONResponse(status_code=429, content={"detail": "Too many requests"})

    from pocketpaw.api.oauth2.server import get_oauth_server

    server = get_oauth_server()
    client = server.storage.get_client(client_id)
    if client is None or redirect_uri not in client.redirect_uris:
        raise HTTPException(status_code=400, detail="Invalid authorization request")

    scope_badges = " ".join(f'<span class="scope">{s}</span>' for s in scope.split())

    html = _CONSENT_HTML.format(
        client_name=client.client_name,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        scope_badges=scope_badges,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        state=state,
    )
    return HTMLResponse(html)


@router.post("/oauth/authorize/consent")
async def authorize_consent(request: Request):
    """Process consent form submission."""
    from pocketpaw.security.rate_limiter import auth_limiter

    client_ip = request.client.host if request.client else "unknown"
    if not auth_limiter.allow(client_ip):
        return JSONResponse(status_code=429, content={"detail": "Too many requests"})

    from pocketpaw.api.oauth2.server import get_oauth_server

    form = await request.form()
    action = form.get("action", "deny")

    client_id = str(form.get("client_id", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    scope = str(form.get("scope", ""))
    code_challenge = str(form.get("code_challenge", ""))
    code_challenge_method = str(form.get("code_challenge_method", "S256"))
    state = str(form.get("state", ""))

    if action != "allow":
        params = {"error": "access_denied"}
        if state:
            params["state"] = state
        return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)

    server = get_oauth_server()
    code, error = server.authorize(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )

    if error:
        params = {"error": error}
        if state:
            params["state"] = state
        return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)

    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)


@router.post("/oauth/token")
async def token_exchange(body: TokenRequest):
    """Exchange authorization code or refresh token for access token."""
    from pocketpaw.api.oauth2.server import get_oauth_server

    server = get_oauth_server()

    if body.grant_type == "authorization_code":
        if not body.code or not body.code_verifier or not body.client_id:
            raise HTTPException(
                status_code=400,
                detail="code, code_verifier, and client_id are required",
            )
        result, error = server.exchange(
            code=body.code,
            client_id=body.client_id,
            code_verifier=body.code_verifier,
            redirect_uri=body.redirect_uri or "",
        )
    elif body.grant_type == "refresh_token":
        if not body.refresh_token:
            raise HTTPException(status_code=400, detail="refresh_token is required")
        result, error = server.refresh(body.refresh_token)
    else:
        raise HTTPException(status_code=400, detail="Unsupported grant_type")

    if error:
        raise HTTPException(status_code=400, detail=error)

    return result


@router.post("/oauth/revoke")
async def revoke_token(body: RevokeRequest):
    """Revoke an access or refresh token."""
    from pocketpaw.api.oauth2.server import get_oauth_server

    server = get_oauth_server()
    revoked = server.revoke(body.token)
    return {"revoked": revoked}
