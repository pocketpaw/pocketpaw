"""SSO router — admin config CRUD + the public OIDC login/callback dance.

The login + callback routes are unauthenticated (the user has no
session yet); the workspace-scoped config routes go through the
standard ``require_action("workspace.update")`` admin guard.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse

from pocketpaw_ee.cloud._core.deps import require_action
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
from pocketpaw_ee.cloud.auth._login_helpers import mint_and_record
from pocketpaw_ee.cloud.auth.core import cookie_backend
from pocketpaw_ee.cloud.auth.sso import service as sso_service
from pocketpaw_ee.cloud.auth.sso.dto import (
    SsoConfigOut,
    SsoConfigUpsertRequest,
    SsoTestResponse,
)
from pocketpaw_ee.cloud.models.workspace import SsoConfig

logger = logging.getLogger(__name__)

router = APIRouter(tags=["SSO"])


def _to_out(cfg: SsoConfig) -> SsoConfigOut:
    return SsoConfigOut(
        provider=cfg.provider,
        issuer=cfg.issuer,
        client_id=cfg.client_id,
        allowed_domains=list(cfg.allowed_domains or []),
        enforced=cfg.enforced,
    )


# ---------------------------------------------------------------------------
# Workspace-scoped admin config
# ---------------------------------------------------------------------------


@router.post("/workspaces/{workspace_id}/sso", response_model=SsoConfigOut)
async def upsert_sso(
    workspace_id: str,
    body: SsoConfigUpsertRequest,
    _admin=Depends(require_action("workspace.update")),
) -> SsoConfigOut:
    cfg = await sso_service.upsert_sso_config(
        workspace_id,
        provider=body.provider,
        issuer=body.issuer,
        client_id=body.client_id,
        client_secret_plain=body.client_secret,
        allowed_domains=body.allowed_domains,
        enforced=body.enforced,
    )
    return _to_out(cfg)


@router.get("/workspaces/{workspace_id}/sso", response_model=SsoConfigOut)
async def get_sso(
    workspace_id: str,
    _admin=Depends(require_action("workspace.update")),
) -> SsoConfigOut:
    cfg = await sso_service.get_sso_config(workspace_id)
    if cfg is None:
        raise NotFound("sso_config", workspace_id)
    return _to_out(cfg)


@router.delete("/workspaces/{workspace_id}/sso", status_code=204)
async def delete_sso(
    workspace_id: str,
    _admin=Depends(require_action("workspace.update")),
) -> None:
    await sso_service.delete_sso_config(workspace_id)


@router.post("/workspaces/{workspace_id}/sso/test", response_model=SsoTestResponse)
async def test_sso(
    workspace_id: str,
    _admin=Depends(require_action("workspace.update")),
) -> SsoTestResponse:
    result = await sso_service.test_connection(workspace_id)
    return SsoTestResponse(**result)


# ---------------------------------------------------------------------------
# Public OIDC dance — no auth required
# ---------------------------------------------------------------------------


def _error_redirect(reason: str) -> RedirectResponse:
    return RedirectResponse(url=f"/auth/error?reason={quote(reason)}", status_code=302)


@router.get("/auth/sso/{workspace_slug}/login")
async def sso_login(workspace_slug: str) -> RedirectResponse:
    try:
        url = await sso_service.begin_login(workspace_slug)
    except CloudError as exc:
        return _error_redirect(exc.code)
    except Exception:  # noqa: BLE001 — discovery / network failure
        logger.exception("sso.begin_login failed for slug=%s", workspace_slug)
        return _error_redirect("sso.begin_failed")
    return RedirectResponse(url=url, status_code=302)


@router.get("/auth/sso/callback")
async def sso_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        return _error_redirect(error)
    if not code or not state:
        return _error_redirect("sso.missing_code_or_state")
    try:
        user = await sso_service.complete_login(code, state)
    except CloudError as exc:
        return _error_redirect(exc.code)
    except Exception:  # noqa: BLE001
        logger.exception("sso.complete_login failed")
        return _error_redirect("sso.callback_failed")

    response = await mint_and_record(cookie_backend, user, request)
    # Redirect to frontend root; the response already carries the auth cookie.
    redirect = RedirectResponse(url="/", status_code=302)
    for key, value in response.headers.items():
        if key.lower() == "set-cookie":
            redirect.headers.append("set-cookie", value)
    return redirect
