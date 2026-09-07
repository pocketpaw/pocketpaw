# ee/pocketpaw_ee/cloud/byok/router.py — set / inspect / remove the workspace's
# own provider key.
#
# Created 2026-08-28 (feat/other-hand-byok).
#
# Three routes, and a hard rule: NOTHING here returns a key. Every response is a
# ``ByokStatus`` built from display-only columns. There is no GET that reveals
# the credential and no echo on save — once set, a key is write-only from the
# API's point of view, and the only code that can read it back is the turn path.
#
# Workspace-scoped, not user-scoped, matching where the credential is spent: a
# turn runs in a workspace. On the Otherhand kiosk face each account gets its own
# auto-provisioned workspace, so workspace == user there anyway.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.byok import service as byok_service
from pocketpaw_ee.cloud.byok.dto import ByokSetRequest, ByokStatus
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

router = APIRouter(prefix="/byok", tags=["BYOK"])


@router.get("/key", response_model=ByokStatus)
async def get_byok_status(
    workspace_id: str = Depends(current_workspace_id),
) -> ByokStatus:
    """Whether a key is configured, and enough to tell WHICH one. Never the key."""
    return await byok_service.get_status(workspace_id)


@router.put("/key", response_model=ByokStatus)
async def set_byok_key(
    body: ByokSetRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> ByokStatus:
    """Validate the key against the provider, then store it encrypted.

    PUT rather than POST because setting a key twice is the same as setting it
    once — there is at most one per workspace, and replacing is the normal case.
    A key that the provider rejects is never written.
    """
    return await byok_service.set_key(
        workspace_id,
        body.api_key,
        provider=body.provider,
        user_id=user_id,
    )


@router.delete("/key", response_model=ByokStatus)
async def delete_byok_key(
    workspace_id: str = Depends(current_workspace_id),
) -> ByokStatus:
    """Remove the key. Idempotent — removing an absent key succeeds."""
    return await byok_service.delete_key(workspace_id)
