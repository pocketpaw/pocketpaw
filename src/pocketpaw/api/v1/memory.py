# Memory router — long-term CRUD, memory settings/stats.
# Created: 2026-02-20

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.common import OkResponse, StatusResponse
from pocketpaw.api.v1.schemas.memory import MemoryEntry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Memory"], dependencies=[Depends(require_scope("memory"))])

_MEMORY_CONFIG_KEYS = {
    "memory_backend": "memory_backend",
    "memory_use_inference": "memory_use_inference",
    "mem0_llm_provider": "mem0_llm_provider",
    "mem0_llm_model": "mem0_llm_model",
    "mem0_embedder_provider": "mem0_embedder_provider",
    "mem0_embedder_model": "mem0_embedder_model",
    "mem0_vector_store": "mem0_vector_store",
    "mem0_ollama_base_url": "mem0_ollama_base_url",
    "mem0_auto_learn": "mem0_auto_learn",
}


@router.get("/memory/long_term")
async def get_long_term_memory(limit: int = Query(50, ge=1, le=500)):
    """Get long-term memories."""
    from pocketpaw.memory import MemoryType, get_memory_manager

    manager = get_memory_manager()
    items = await manager._store.get_by_type(MemoryType.LONG_TERM, limit=limit)
    return [
        MemoryEntry(
            id=item.id,
            content=item.content,
            timestamp=(
                item.created_at.isoformat()
                if hasattr(item.created_at, "isoformat")
                else str(item.created_at)
            ),
            tags=item.tags,
        ).model_dump()
        for item in items
    ]


@router.delete("/memory/long_term/{entry_id}", response_model=OkResponse)
async def delete_long_term_memory(entry_id: str):
    """Delete a long-term memory entry by ID."""
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager()
    deleted = await manager._store.delete(entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return OkResponse()


@router.get("/memory/settings")
async def get_memory_settings():
    """Get current memory backend configuration."""
    from pocketpaw.config import Settings

    settings = Settings.load()
    return {
        "memory_backend": settings.memory_backend,
        "memory_use_inference": settings.memory_use_inference,
        "mem0_llm_provider": settings.mem0_llm_provider,
        "mem0_llm_model": settings.mem0_llm_model,
        "mem0_embedder_provider": settings.mem0_embedder_provider,
        "mem0_embedder_model": settings.mem0_embedder_model,
        "mem0_vector_store": settings.mem0_vector_store,
        "mem0_ollama_base_url": settings.mem0_ollama_base_url,
        "mem0_auto_learn": settings.mem0_auto_learn,
    }


@router.post("/memory/settings", response_model=StatusResponse)
async def save_memory_settings(request: Request):
    """Save memory backend configuration."""
    from pocketpaw.config import Settings, get_settings
    from pocketpaw.memory import get_memory_manager

    data = await request.json()
    settings = Settings.load()

    for key, value in data.items():
        settings_field = _MEMORY_CONFIG_KEYS.get(key)
        if settings_field:
            setattr(settings, settings_field, value)

    settings.save()
    get_settings.cache_clear()
    get_memory_manager(force_reload=True)
    return StatusResponse()


@router.get("/memory/stats")
async def get_memory_stats():
    """Get memory backend statistics."""
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "get_memory_stats"):
        return await store.get_memory_stats()

    return {"backend": "file", "total_memories": "N/A (use mem0 for stats)"}
