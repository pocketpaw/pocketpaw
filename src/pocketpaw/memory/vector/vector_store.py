
from pocketpaw.config import Settings
from pocketpaw.memory.protocol import MemoryEntry, MemoryType
from pocketpaw.vectordb.chroma_adapter import ChromaAdapter


class VectorMemory:
    """
    Implementation of MemoryStoreProtocol that uses the ChromaDB adapter.
    """

    def __init__(self, settings: Settings):
        # Initializes the adapter using the path from your merged config
        self.adapter = ChromaAdapter.from_settings(settings)

    async def save(self, entry: MemoryEntry) -> str:
        # Convert Pydantic object to database-friendly metadata
        metadata = {
            "memory_type": str(entry.type),  # Use .type to match MemoryEntry model
            "session_key": entry.session_key or "default",
        }
        await self.adapter.add(doc_id=entry.id, text=entry.content, metadata=metadata)
        return entry.id

    async def search(self, query: str, limit: int = 10) -> list[MemoryEntry]:
        # ChromaAdapter returns a list of strings (documents)
        results = await self.adapter.search(query, limit=limit)

        memory_entries = []
        for doc_text in results:
            # Kyunki adapter sirf text de raha hai, hum temporary ID
            # aur default type use karenge
            memory_entries.append(
                MemoryEntry(
                    id="search-result",  # Adapter ID nahi bhej raha, isliye dummy ID
                    content=doc_text,
                    type=MemoryType.LONG_TERM,
                )
            )
        return memory_entries

    async def get(self, entry_id: str) -> MemoryEntry | None:
        result = await self.adapter.get_by_id(entry_id)
        if not result:
            return None
        return MemoryEntry(
            id=result["id"],
            content=result["text"],
            type=MemoryType(result.get("metadata", {}).get("memory_type", "fact")),
        )

    async def delete(self, entry_id: str) -> bool:
        return await self.adapter.delete(entry_id)
