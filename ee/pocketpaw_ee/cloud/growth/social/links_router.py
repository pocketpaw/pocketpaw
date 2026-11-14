# ee/pocketpaw_ee/cloud/growth/social/links_router.py — the public redirect for
# Growth › Social tracked links, ``GET /api/v1/go/{code}``. Mounted on its own,
# beside the growth webhooks, because the people clicking a link in an X reply
# hold no session: no license gate, no RBAC, no RequestContext.
#
# What keeps it safe: ``service.record_click`` refuses a code that does not
# match ``links.CODE_RE`` before anything reads the database; the 302 goes only
# to the URL stored with the code (the request's query string is never passed
# on); an unknown or malformed code is a bare 404 with no body. The handler
# hands the service the User-Agent only to skip preview-bot fetches; nothing
# about the requester is stored. Rate limiting is the proxy's job (Traefik on
# Coolify), like every other public route.

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from pocketpaw_ee.cloud.growth.social import service as social_service

router = APIRouter(prefix="/go", tags=["Growth"])


@router.get("/{code}", include_in_schema=False)
async def follow_link(code: str, request: Request) -> Response:
    target = await social_service.record_click(code, request.headers.get("user-agent"))
    if target is None:
        return Response(status_code=404)
    return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})
