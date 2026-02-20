# Memory schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel


class MemoryEntry(BaseModel):
    id: str
    content: str
    timestamp: str
    tags: list[str] = []


class MemorySettingsResponse(BaseModel):
    memory_backend: str = "file"
    memory_use_inference: bool = False
    mem0_llm_provider: str = ""
    mem0_llm_model: str = ""
    mem0_embedder_provider: str = ""
    mem0_embedder_model: str = ""
    mem0_vector_store: str = ""
    mem0_ollama_base_url: str = ""
    mem0_auto_learn: bool = False
