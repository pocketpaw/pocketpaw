import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Use TYPE_CHECKING to avoid circular imports with Settings
if TYPE_CHECKING:
    from pocketpaw.config import Settings


# Note: We no longer inherit from VectorStoreProtocol here.
# The @runtime_checkable on the protocol handles the check automatically.
class ChromaAdapter:
    def __init__(self, path: str | Path | None = None, collection_name: str = "pocketpaw_memory"):
        try:
            import chromadb
        except ImportError:
            raise ImportError(
                "chromadb is required for vector backend. Install with: pip install chromadb"
            )

        # 1. Define the project convention path as a Path object
        default_path = Path.home() / ".pocketpaw" / "chroma_db"

        # 2. Determine the path and create the directory while it's still a Path object
        target_path = Path(path) if path is not None else default_path

        # This creates the .pocketpaw folder if it's missing
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # 3. Convert to string only when passing to the chromadb client
        self.client = chromadb.PersistentClient(path=str(target_path))

        self.collection = self.client.get_or_create_collection(name=collection_name)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "ChromaAdapter":
        """
        Factory method to create an adapter instance using the
        vectordb_path defined in the project settings.
        """
        return cls(path=settings.vectordb_path)

    async def add(self, doc_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        """Adds or updates a document using upsert."""
        await asyncio.to_thread(
            self.collection.upsert,
            documents=[text],
            ids=[doc_id],
            metadatas=[metadata] if metadata else None,
        )

    async def search(self, query: str, limit: int = 5) -> list[str]:
        results = await asyncio.to_thread(
            self.collection.query,
            query_texts=[query],
            n_results=limit,
        )

        # Safe check for search results
        if results and results.get("documents") and len(results["documents"]) > 0:
            return results["documents"][0]
        return []

    async def delete(self, doc_id: str) -> None:
        """Deletes a document by its ID."""
        await asyncio.to_thread(
            self.collection.delete,
            ids=[doc_id],
        )

    async def get_by_id(self, doc_id: str) -> str | None:
        """FIX: get_by_id crash prevention logic."""
        results = await asyncio.to_thread(
            self.collection.get,
            ids=[doc_id],
        )

        # Safely checks if documents key exists, has items, and the first item isn't None
        docs = results.get("documents")
        if docs and len(docs) > 0 and docs[0] is not None:
            return docs[0]

        return None
