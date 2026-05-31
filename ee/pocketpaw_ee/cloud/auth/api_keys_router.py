"""HTTP router for workspace API keys.

JWT-only on every endpoint — API keys can't mint, list, or revoke other
API keys. Mounted into the auth router so wave-3 changes stay contiguous.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.auth import api_keys as api_keys_service
from pocketpaw_ee.cloud.auth.api_keys_dto import (
    APIKeyOut,
    CreateAPIKeyRequest,
    CreatedAPIKeyResponse,
)
from pocketpaw_ee.cloud.auth.core import current_active_user
from pocketpaw_ee.cloud.auth.scopes import validate_scopes
from pocketpaw_ee.cloud.models.api_key import APIKey

router = APIRouter(tags=["Auth"])


def _doc_to_out(doc: APIKey) -> APIKeyOut:
    return APIKeyOut(
        id=str(doc.id),
        name=doc.name,
        prefix=doc.prefix,
        scopes=list(doc.scopes),
        ownerUserId=doc.owner_user_id,
        expiresAt=doc.expires_at.isoformat() if doc.expires_at else None,
        lastUsedAt=doc.last_used_at.isoformat() if doc.last_used_at else None,
        createdAt=doc.created_at.isoformat(),
        revoked=doc.revoked,
    )


async def _require_member(workspace_id: str, user_id: str) -> None:
    from pocketpaw_ee.cloud.workspace.service import _get_member_role

    role = await _get_member_role(workspace_id, user_id)
    if role is None:
        raise HTTPException(status_code=404, detail="workspace_not_found")


@router.post(
    "/workspaces/{workspace_id}/api-keys",
    response_model=CreatedAPIKeyResponse,
)
async def create_workspace_api_key(
    workspace_id: str,
    body: CreateAPIKeyRequest,
    user: Any = Depends(current_active_user),
) -> CreatedAPIKeyResponse:
    await _require_member(workspace_id, str(user.id))

    try:
        scopes = validate_scopes(body.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    doc, full_key = await api_keys_service.create_api_key(
        workspace_id=workspace_id,
        owner_user_id=str(user.id),
        name=body.name,
        scopes=scopes,
        expires_at=api_keys_service._expires_in_days(body.expires_in_days),
    )

    await audit_service.record(
        workspace_id,
        str(user.id),
        "api_key.create",
        target_type="api_key",
        target_id=str(doc.id),
        metadata={"name": doc.name, "scopes": scopes, "prefix": doc.prefix},
    )

    out = _doc_to_out(doc)
    return CreatedAPIKeyResponse(**out.model_dump(by_alias=False), fullKey=full_key)


@router.get(
    "/workspaces/{workspace_id}/api-keys",
    response_model=list[APIKeyOut],
)
async def list_workspace_api_keys(
    workspace_id: str,
    user: Any = Depends(current_active_user),
) -> list[APIKeyOut]:
    await _require_member(workspace_id, str(user.id))
    rows = await api_keys_service.list_api_keys(workspace_id)
    return [_doc_to_out(r) for r in rows]


@router.delete("/workspaces/{workspace_id}/api-keys/{key_id}")
async def revoke_workspace_api_key(
    workspace_id: str,
    key_id: str,
    user: Any = Depends(current_active_user),
) -> dict:
    await _require_member(workspace_id, str(user.id))
    doc = await api_keys_service.revoke_api_key(key_id, workspace_id)
    await audit_service.record(
        workspace_id,
        str(user.id),
        "api_key.revoke",
        target_type="api_key",
        target_id=str(doc.id),
        metadata={"name": doc.name, "prefix": doc.prefix},
    )
    return {"ok": True}


__all__ = ["router"]
