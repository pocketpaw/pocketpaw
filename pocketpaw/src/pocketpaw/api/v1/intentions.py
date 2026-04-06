# Intentions router — CRUD + toggle + run.
# Created: 2026-02-20

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from pocketpaw.api.v1.schemas.intentions import (
    CreateIntentionRequest,
    DeleteIntentionResponse,
    IntentionInfo,
    IntentionListResponse,
    IntentionResponse,
    RunIntentionResponse,
    UpdateIntentionRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Intentions"])


def _to_info(d: dict) -> IntentionInfo:
    """Convert a raw intention dict to an IntentionInfo model."""
    return IntentionInfo(
        id=d.get("id", ""),
        name=d.get("name", ""),
        prompt=d.get("prompt", ""),
        trigger=d.get("trigger", {}),
        context_sources=d.get("context_sources", []),
        enabled=d.get("enabled", True),
        created_at=d.get("created_at", ""),
        last_run=d.get("last_run"),
        next_run=d.get("next_run"),
    )


@router.get("/intentions", response_model=IntentionListResponse)
async def list_intentions():
    """Get all intentions."""
    from pocketpaw.daemon.proactive import get_daemon

    daemon = get_daemon()
    raw = daemon.get_intentions()
    return IntentionListResponse(intentions=[_to_info(i) for i in raw])


@router.post("/intentions", response_model=IntentionResponse)
async def create_intention(body: CreateIntentionRequest):
    """Create a new intention."""
    from pocketpaw.daemon.proactive import get_daemon

    daemon = get_daemon()
    try:
        result = daemon.create_intention(
            name=body.name,
            prompt=body.prompt,
            trigger=body.trigger,
            context_sources=body.context_sources,
            enabled=body.enabled,
        )
        return IntentionResponse(intention=_to_info(result))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create intention: {exc}")


@router.patch("/intentions/{intention_id}", response_model=IntentionResponse)
async def update_intention(intention_id: str, body: UpdateIntentionRequest):
    """Partially update an intention."""
    from pocketpaw.daemon.proactive import get_daemon

    daemon = get_daemon()
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    result = daemon.update_intention(intention_id, updates)
    if result is None:
        raise HTTPException(status_code=404, detail="Intention not found")

    return IntentionResponse(intention=_to_info(result))


@router.delete("/intentions/{intention_id}", response_model=DeleteIntentionResponse)
async def delete_intention(intention_id: str):
    """Delete an intention by ID."""
    from pocketpaw.daemon.proactive import get_daemon

    daemon = get_daemon()
    deleted = daemon.delete_intention(intention_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Intention not found")

    return DeleteIntentionResponse(id=intention_id)


@router.post("/intentions/{intention_id}/toggle", response_model=IntentionResponse)
async def toggle_intention(intention_id: str):
    """Toggle the enabled state of an intention."""
    from pocketpaw.daemon.proactive import get_daemon

    daemon = get_daemon()
    result = daemon.toggle_intention(intention_id)

    if result is None:
        raise HTTPException(status_code=404, detail="Intention not found")

    return IntentionResponse(intention=_to_info(result))


@router.post("/intentions/{intention_id}/run", response_model=RunIntentionResponse)
async def run_intention(intention_id: str):
    """Manually trigger an intention to run now."""
    from pocketpaw.daemon.proactive import get_daemon

    daemon = get_daemon()
    # Verify the intention exists
    intentions = daemon.get_intentions()
    intention = next((i for i in intentions if i.get("id") == intention_id), None)

    if intention is None:
        raise HTTPException(status_code=404, detail="Intention not found")

    # Fire and forget — intention runs in background
    asyncio.create_task(daemon.run_intention_now(intention_id))

    return RunIntentionResponse(
        id=intention_id,
        status="running",
        message=f"Running intention: {intention.get('name', '')}",
    )
