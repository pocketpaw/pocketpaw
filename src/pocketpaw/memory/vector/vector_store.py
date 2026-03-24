from pocketpaw.memory.protocol import MemoryEntry, MemoryStoreProtocol, MemoryType
from pocketpaw.vectordb.chroma_adapter import ChromaAdapter


class VectorMemory(MemoryStoreProtocol):
    """
    Implementation of MemoryStoreProtocol that uses the ChromaDB adapter.
    Enables semantic long-term memory storage and retrieval.
    """

    def __init__(self, adapter: ChromaAdapter):
        self.adapter = adapter

    async def save(self, entry: MemoryEntry) -> str:
        """Saves a memory entry and returns its unique ID."""
        metadata = {
            "memory_type": entry.type.value,
            "session_key": entry.session_key or "default",
            "tags": entry.tags,
            "role": entry.role or "",
        }
        
        # Include any additional fields from the entry's metadata
        if entry.metadata:
            metadata.update(entry.metadata)

        await self.adapter.add(doc_id=entry.id, text=entry.content, metadata=metadata)
        return entry.id

    async def get(self, entry_id: str) -> MemoryEntry | None:
        """Retrieves a specific memory entry by its ID."""
        result = await self.adapter.get_by_id(entry_id)
        if not result:
            return None
            
        metadata = result.get("metadata", {})
        m_type_str = metadata.get("memory_type", "long_term")
        
        return MemoryEntry(
            id=result["id"],
            content=result["text"],
            type=MemoryType(m_type_str)
            if m_type_str in [t.value for t in MemoryType]
            else MemoryType.LONG_TERM,
            metadata=metadata,
            tags=metadata.get("tags", []),
            role=metadata.get("role"),
            session_key=metadata.get("session_key")
        )

    async def delete(self, entry_id: str) -> bool:
        """Deletes an entry from the vector database."""
        return await self.adapter.delete(entry_id)

    async def search(
        self, 
        query: str | None = None, 
        memory_type: MemoryType | None = None, 
        tags: list[str] | None = None, 
        limit: int = 10
    ) -> list[MemoryEntry]:
        """Searches memories by semantic query, type, or tags."""
        if not query:
            # Fallback to fetching all entries if no search query is provided
            results = await self.adapter.get_all(limit=limit)
        else:
            results = await self.adapter.search(query, limit=limit)
        
        memory_entries = []
        for result in results:
            content = result.get("text", "")
            doc_id = result.get("id", "unknown")
            metadata = result.get("metadata", {})
            # FIX: Check if result is a string (only text) or a dict (full data)
            if isinstance(result, str):
                # If it's just a string, we provide defaults for other fields
                content = result
                metadata = {}
                doc_id = "unknown"
            else:
                # If it's a dict, extract values safely
                content = result.get("text", result.get("content", ""))
                metadata = result.get("metadata", {})
                doc_id = result.get("id", "unknown")

            # Apply manual filtering for memory_type
            if memory_type and metadata.get("memory_type") != memory_type.value:
                continue

            # Apply manual filtering for tags
            if tags:
                stored_tags = metadata.get("tags", [])
                if not any(tag in stored_tags for tag in tags):
                    continue
            
            m_type_str = metadata.get("memory_type", "long_term")
            memory_entries.append(
                MemoryEntry(
                    id=doc_id,
                    content=content,
                    type=MemoryType(m_type_str)
                    if m_type_str in [t.value for t in MemoryType]
                    else MemoryType.LONG_TERM,
                    metadata=metadata,
                    tags=metadata.get("tags", []),
                    role=metadata.get("role"),
                    session_key=metadata.get("session_key")
                )
            )
        return memory_entries[:limit]

    async def get_by_type(
        self, 
        memory_type: MemoryType, 
        limit: int = 100,
        user_id: str | None = None
    ) -> list[MemoryEntry]:
        """Retrieves all memories belonging to a specific MemoryType."""
        # Reuse search logic with a type filter but without a specific query
        return await self.search(memory_type=memory_type, limit=limit)

    async def get_session(self, session_key: str, user_id: str | None = None) -> list[MemoryEntry]:
        """Retrieves conversation history for a specific session."""
        # Fetch entries from ChromaAdapter filtered by session_key metadata
        results = await self.adapter.get_by_metadata({"session_key": session_key})
        
        entries = []
        for result in results:
            metadata = result.get("metadata", {})
            entries.append(
                MemoryEntry(
                    id=result.get("id"),
                    content=result.get("text", ""),
                    type=MemoryType.SESSION,
                    metadata=metadata,
                    session_key=session_key,
                    role=metadata.get("role")
                )
            )
        return entries

    async def clear_session(self, session_key: str) -> int:
        """Deletes all history for a session and returns the count of deleted entries."""
        entries = await self.get_session(session_key)
        count = 0
        for entry in entries:
            if entry.id:
                await self.delete(entry.id)
                count += 1
        return count
