# API keys router — CRUD endpoints for long-lived API keys.
# Created: 2026-02-20

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.api_keys import (
    APIKeyCreatedResponse,
    APIKeyInfo,
    CreateKeyRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["API Keys"], dependencies=[Depends(require_scope("admin"))])


@router.post("/auth/api-keys", response_model=APIKeyCreatedResponse)
async def create_api_key(body: CreateKeyRequest):
    """Create a new API key. The plaintext key is returned only once."""
    from pocketpaw.api.api_keys import get_api_key_manager

    manager = get_api_key_manager()
    try:
        record, plaintext = manager.create(
            name=body.name,
            scopes=body.scopes,
            expires_at=body.expires_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return APIKeyCreatedResponse(
        key=plaintext,
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        scopes=record.scopes,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )


@router.get("/auth/api-keys", response_model=list[APIKeyInfo])
async def list_api_keys():
    """List all API keys (no secrets exposed)."""
    from pocketpaw.api.api_keys import get_api_key_manager

    manager = get_api_key_manager()
    keys = manager.list_keys()
    return [
        APIKeyInfo(
            id=k.id,
            name=k.name,
            prefix=k.prefix,
            scopes=k.scopes,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            expires_at=k.expires_at,
            revoked=k.revoked,
        )
        for k in keys
    ]


@router.delete("/auth/api-keys/{key_id}")
async def revoke_api_key(key_id: str):
    """Revoke an API key."""
    from pocketpaw.api.api_keys import get_api_key_manager

    manager = get_api_key_manager()
    revoked = manager.revoke(key_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="API key not found or already revoked")
    return {"status": "ok"}


@router.post("/auth/api-keys/{key_id}/rotate", response_model=APIKeyCreatedResponse)
async def rotate_api_key(key_id: str):
    """Rotate an API key: revoke old + create new with same scopes."""
    from pocketpaw.api.api_keys import get_api_key_manager

    manager = get_api_key_manager()
    result = manager.rotate(key_id)
    if result is None:
        raise HTTPException(status_code=404, detail="API key not found or already revoked")

    record, plaintext = result
    return APIKeyCreatedResponse(
        key=plaintext,
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        scopes=record.scopes,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )
