# share_router.py — FL-12b "Public share links".
#
# Two routers with DELIBERATELY different auth boundaries:
#
#   share_router  (prefix /files) — OWNER surface, license + membership gated:
#       POST   /files/{file_id}/share          create a share link
#       DELETE /files/{file_id}/share/{token}   revoke a share link
#     Both require a workspace principal AND owner-or-admin write access on the
#     file (reuses EEUploadService._assert_can_write), so create/revoke are
#     workspace + owner scoped.
#
#   public_share_router  (prefix /share) — PUBLIC, NO auth, NO license gate:
#       GET    /share/{token}                    redirect to a download URL
#     Authorization here is the unguessable token ITSELF. The route looks the
#     token up, checks active (not revoked, not expired) -> 410/404 otherwise,
#     then mints the download URL by calling EEUploadService.presigned_get with
#     the LINK's stored owner_id + workspace_id as the read identity. That path
#     forces Content-Disposition: attachment for any non-inline mime (ART-4), so
#     an agent-authored .html/.svg/.js downloads instead of rendering active
#     content on the storage origin. We do NOT build a separate presign — the
#     forced-attachment protection is inherited, not bypassed.
#
# The public route leaks nothing about the workspace/owner: a bad token is a
# flat 404, an inactive one a flat 410, and success is an opaque redirect to a
# short-TTL storage URL. Sharing is independent of FileUpload.hide_from_ai.
#
# Feature-flagged behind POCKETPAW_SHARE_LINKS_ENABLED (default OFF for a
# cautious rollout): when disabled, BOTH the create route and the public GET
# return 404 so the surface is fully dark.

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse

from pocketpaw.uploads.errors import NotFound
from pocketpaw.uploads.signing import DEFAULT_TTL_SECONDS
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud.shared.time import iso_utc
from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
from pocketpaw_ee.cloud.uploads.router import _SVC, _is_workspace_admin
from pocketpaw_ee.cloud.uploads.share_models import SHARE_LINK_TTL_DAYS
from pocketpaw_ee.cloud.uploads.share_store import ShareLinkStore

_META = MongoFileStore()
_LINKS = ShareLinkStore()


def _share_links_enabled() -> bool:
    """Feature flag — default OFF for a cautious rollout."""
    return os.environ.get("POCKETPAW_SHARE_LINKS_ENABLED", "false").strip().lower() == "true"


# Owner-facing surface: license-gated, workspace + membership scoped.
share_router = APIRouter(
    prefix="/files",
    tags=["Uploads", "ShareLinks"],
    dependencies=[Depends(require_license)],
)

# Public surface: intentionally NO auth and NO license dependency. The token is
# the authorization. Kept on its own router so no ambient gate leaks in.
public_share_router = APIRouter(
    prefix="/share",
    tags=["ShareLinks"],
)


def _token_url(token: str) -> str:
    """Relative public URL a recipient opens. Callers/FE may prefix an origin."""
    return f"/api/v1/share/{token}"


@share_router.post(
    "/{file_id}/share",
    dependencies=[Depends(require_action_any_workspace("uploads.write"))],
)
async def create_share_link(
    file_id: str,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Create a public share link for ``file_id`` (owner / workspace-admin only).

    Independent of the file's ``hide_from_ai`` flag — a file hidden from AI can
    still be shared. Returns the token URL and its expiry.
    """
    if not _share_links_enabled():
        raise HTTPException(status_code=404, detail="not found")

    rec = await _META.get_scoped(file_id, workspace=workspace)
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")

    # Owner OR workspace admin only — same write ACL the PATCH/DELETE routes use.
    try:
        await _SVC._assert_can_write(rec, user_id, workspace)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail="files.forbidden") from e

    link = await _LINKS.create(file_id=file_id, workspace=workspace, owner_id=user_id)
    return {
        "token": link.token,
        "url": _token_url(link.token),
        "expires_at": iso_utc(link.expires_at),
        "ttl_days": SHARE_LINK_TTL_DAYS,
        "revoked": link.revoked,
    }


@share_router.delete("/{file_id}/share/{token}", status_code=204)
async def revoke_share_link(
    file_id: str,
    token: str,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
):
    """Revoke a share link (owner / workspace-admin only, workspace + file scoped)."""
    rec = await _META.get_scoped(file_id, workspace=workspace)
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")

    # Owner OR workspace admin only.
    if rec.owner_id != user_id:
        try:
            is_admin = await _is_workspace_admin(user_id, workspace)
        except Exception:
            is_admin = False
        if not is_admin:
            raise HTTPException(status_code=403, detail="files.forbidden")

    link = await _LINKS.revoke(token, file_id=file_id, workspace=workspace)
    if link is None:
        raise HTTPException(status_code=404, detail="not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@public_share_router.get("/{token}")
async def resolve_share_link(token: str):
    """PUBLIC — resolve a share token to a download redirect. No auth.

    Authorization is the token. Invalid/unknown -> 404; revoked or expired ->
    410; the underlying file gone -> 404. On success, mints a short-TTL
    presigned download URL via ``presigned_get`` (which forces attachment for
    non-inline mimes) and 302-redirects to it. We NEVER expose workspace/owner
    internals and NEVER require auth beyond the token.
    """
    if not _share_links_enabled():
        raise HTTPException(status_code=404, detail="not found")

    link = await _LINKS.get_by_token(token)
    if link is None:
        # Unknown token — flat 404, no distinction from a typo.
        raise HTTPException(status_code=404, detail="not found")

    if not link.is_active():
        # Revoked or expired — gone.
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="link expired")

    # Mint the download as the LINK's owner, in the LINK's workspace. This
    # replays the owner's read identity into presigned_get, which:
    #   (a) re-checks the file still exists in that workspace (NotFound -> 404),
    #   (b) forces Content-Disposition: attachment for non-inline mimes.
    # The token is single-file scoped, so this only ever resolves link.file_id.
    try:
        _rec, presigned = await _SVC.presigned_get(
            link.file_id,
            link.owner_id,
            link.workspace_id,
            DEFAULT_TTL_SECONDS,
        )
    except NotFound as e:
        # File deleted after the link was minted — treat as gone.
        raise HTTPException(status_code=404, detail="not found") from e

    if not presigned:
        # No presigning adapter (local dev) — the authenticated cloud download
        # route requires a JWT, which a public recipient does not have. Fail
        # closed rather than hand back an unusable/authed URL.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="share links require a presigning storage backend",
        )

    # 302 to the short-TTL storage URL. The disposition (attachment for
    # non-inline mimes) is baked into the presigned URL by presigned_get.
    return RedirectResponse(url=presigned, status_code=status.HTTP_302_FOUND)
