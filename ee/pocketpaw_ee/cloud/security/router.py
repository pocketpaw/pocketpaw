# ee/pocketpaw_ee/cloud/security/router.py — the /api/v1/security/* shield proxy.
# Created: 2026-07-01 (feat/sec-5-security-proxy, SEC-5).
#
# A FastAPI router that maps 1:1 to shield's control API (served on a same-box
# UNIX socket) and fronts it for the dashboard:
#   * GET   /security/decisions        — the agent-decision feed (fwd query params)
#   * GET   /security/stats            — egress / decision counters
#   * GET   /security/config           — the deny/allow egress config
#   * PATCH /security/config           — mutate the egress config (fwd JSON body)
#   * POST  /security/decisions/{id}/resolve — ban/allow a pending decision
#                                       (fwd JSON body; the OWNER user is injected
#                                        as ``actor`` so shield records who acted)
#
# EVERY route is OWNER-gated via ``require_action_any_workspace("security.manage")``
# — shield fronts ban-capable writes AND its read feed exposes who-tried-to-
# egress-what, so a mere admin must not reach any of it (mirrors the belt router's
# use of ``require_action_any_workspace`` but at the OWNER tier). The workspace is
# resolved from the caller's active workspace (these routes carry no {workspace_id}
# path param), matching the belt / discovery routers.
#
# The proxy forwards shield's HTTP status + JSON body straight through (a shield
# 400 on a bad PATCH reaches the caller as a 400). It NEVER logs the shield token.
# When shield is absent — socket unset, missing, or unreachable
# (ConnectError / FileNotFoundError / timeout) — the proxy degrades to a TYPED
# response, never a 500:
#   * GETs   → 200 {"available": false, "reason": "shield_not_deployed"|"unreachable"}
#              so the UI can render an empty state. On success the body is
#              {"available": true, ...shield's JSON...}.
#   * writes → 409 {"detail": "shield_not_deployed"} — you can't act on an absent box.

"""FastAPI router proxying the shield security control plane."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from pocketpaw_ee.cloud._core.deps import (
    current_user_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.security import config as shield_config

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/security",
    tags=["Security"],
    dependencies=[Depends(require_license)],
)

# Errors that mean "shield is not reachable on this box right now". A missing
# socket file raises FileNotFoundError from the transport; a refused / hung
# connection raises httpx.ConnectError / httpx.TimeoutException. Any of these is
# the "unreachable" degrade, distinct from "shield_not_deployed" (socket unset).
_UNREACHABLE_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.TimeoutException,
    httpx.TransportError,
    FileNotFoundError,
    ConnectionError,
    OSError,
)


def shield_client_dep() -> httpx.AsyncClient | None:
    """Provide the httpx client bound to shield's socket, or ``None``.

    Returns ``None`` when the socket path is unset (shield not deployed on this
    box) so the routes take the typed no-shield branch WITHOUT attempting a
    connection. Tests override this dependency to inject a fake client (a real
    ``MockTransport``-backed ``AsyncClient``) so no actual socket is needed.
    """
    if not shield_config.shield_socket_path():
        return None
    return shield_config.build_shield_client()


def _no_shield_read() -> JSONResponse:
    """The typed available:false body for a GET when the socket is unset."""
    return JSONResponse(
        status_code=200,
        content={"available": False, "reason": "shield_not_deployed"},
    )


def _unreachable_read() -> JSONResponse:
    """The typed available:false body for a GET when shield can't be reached."""
    return JSONResponse(
        status_code=200,
        content={"available": False, "reason": "unreachable"},
    )


def _no_shield_write() -> JSONResponse:
    """The typed 409 body for a write when shield is absent/unreachable.

    A write can't act on a box that isn't there — 409 Conflict (the state
    required to perform the action does not exist), not a 200 empty state.
    """
    return JSONResponse(status_code=409, content={"detail": "shield_not_deployed"})


def _passthrough(resp: httpx.Response) -> JSONResponse:
    """Forward shield's status + JSON body straight through to the caller.

    A shield 4xx (e.g. a 400 on a bad PATCH) is forwarded verbatim so the caller
    sees shield's own validation error. A non-JSON body (shield shouldn't emit
    one, but be defensive) is wrapped in a stable envelope rather than crashing.
    """
    try:
        body = resp.json()
    except ValueError:
        body = {"detail": resp.text}
    return JSONResponse(status_code=resp.status_code, content=body)


def _available_read(resp: httpx.Response) -> JSONResponse:
    """Wrap a successful shield GET as {"available": true, ...shield JSON...}.

    On a shield error status the body is forwarded verbatim (not wrapped) so the
    caller still sees shield's error shape rather than a spurious available:true.
    """
    if resp.status_code >= 400:
        return _passthrough(resp)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": resp.text}
    content: dict[str, Any] = {"available": True}
    if isinstance(payload, dict):
        content.update(payload)
    else:
        content["data"] = payload
    return JSONResponse(status_code=resp.status_code, content=content)


async def _proxy_get(
    client: httpx.AsyncClient | None,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> JSONResponse:
    """Forward a GET to shield, degrading to a typed read on absence/failure."""
    if client is None:
        return _no_shield_read()
    try:
        async with client:
            resp = await client.get(path, params=params)
    except _UNREACHABLE_ERRORS:
        # Log the reason (never the token / Authorization header) so operators
        # can see shield is down without leaking the credential.
        logger.warning("shield unreachable on GET %s — degrading to available:false", path)
        return _unreachable_read()
    return _available_read(resp)


async def _proxy_write(
    client: httpx.AsyncClient | None,
    method: str,
    path: str,
    *,
    json_body: Any,
) -> JSONResponse:
    """Forward a PATCH/POST to shield, degrading to a typed 409 on absence."""
    if client is None:
        return _no_shield_write()
    try:
        async with client:
            resp = await client.request(method, path, json=json_body)
    except _UNREACHABLE_ERRORS:
        logger.warning("shield unreachable on %s %s — returning 409", method, path)
        return _no_shield_write()
    return _passthrough(resp)


# ---------------------------------------------------------------------------
# Reads — decisions / stats / config
# ---------------------------------------------------------------------------


@router.get("/decisions")
async def get_decisions(
    request: Request,
    _user: Any = Depends(require_action_any_workspace("security.manage")),
    client: httpx.AsyncClient | None = Depends(shield_client_dep),
) -> JSONResponse:
    """Proxy shield's agent-decision feed (OWNER only).

    Query params are forwarded verbatim so shield owns the filter/paginate
    contract (e.g. ``?status=pending&limit=50``). On success the body is
    ``{"available": true, ...shield JSON...}``; when shield is absent it is
    ``{"available": false, "reason": ...}`` at 200 so the UI renders an empty
    state instead of erroring.
    """
    return await _proxy_get(client, "/decisions", params=dict(request.query_params))


@router.get("/stats")
async def get_stats(
    _user: Any = Depends(require_action_any_workspace("security.manage")),
    client: httpx.AsyncClient | None = Depends(shield_client_dep),
) -> JSONResponse:
    """Proxy shield's egress / decision counters (OWNER only)."""
    return await _proxy_get(client, "/stats")


@router.get("/config")
async def get_config(
    _user: Any = Depends(require_action_any_workspace("security.manage")),
    client: httpx.AsyncClient | None = Depends(shield_client_dep),
) -> JSONResponse:
    """Proxy shield's egress deny/allow config (OWNER only)."""
    return await _proxy_get(client, "/config")


# ---------------------------------------------------------------------------
# Writes — config PATCH, decision resolve
# ---------------------------------------------------------------------------


@router.patch("/config")
async def patch_config(
    body: dict[str, Any],
    _user: Any = Depends(require_action_any_workspace("security.manage")),
    client: httpx.AsyncClient | None = Depends(shield_client_dep),
) -> JSONResponse:
    """Proxy a config mutation to shield (OWNER only).

    The JSON body is forwarded verbatim; shield validates it and a bad PATCH
    (shield 400) is forwarded through as a 400. When shield is absent the write
    returns 409 ``shield_not_deployed`` — you cannot mutate the config on a box
    that has no shield.
    """
    return await _proxy_write(client, "PATCH", "/config", json_body=body)


@router.post("/decisions/{decision_id}/resolve")
async def resolve_decision(
    decision_id: str,
    body: dict[str, Any] | None = None,
    user_id: str = Depends(current_user_id),
    _user: Any = Depends(require_action_any_workspace("security.manage")),
    client: httpx.AsyncClient | None = Depends(shield_client_dep),
) -> JSONResponse:
    """Resolve (ban/allow) a pending shield decision (OWNER only).

    The OWNER user id is injected as the ``actor`` field on the forwarded body
    so shield records WHO resolved the decision — the caller cannot spoof a
    different actor (any client-supplied ``actor`` is overwritten). When shield
    is absent the write returns 409 ``shield_not_deployed``.
    """
    payload: dict[str, Any] = dict(body or {})
    # Server-authoritative actor — overwrite any client-supplied value so the
    # audit trail on shield's side always names the authenticated OWNER.
    payload["actor"] = user_id
    return await _proxy_write(
        client, "POST", f"/decisions/{decision_id}/resolve", json_body=payload
    )
