from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VectorStoreProtocol(Protocol):
    """Protocol defining the interface for vector database adapters."""

    async def add(self, doc_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        """
        Add or update a document in the vector store.

        Args:
            doc_id: Unique identifier for the document.
            text: The text content to embed and store.
            metadata: Optional dictionary of associated metadata (source, timestamp, etc.).
        """
        ...

    async def search(self, query: str, limit: int = 5) -> list[str]:
        """Search for the most relevant documents based on a query string.

        Returns a list of document strings.
        """
        ...

    async def delete(self, doc_id: str) -> None:
        """Delete a document from the vector store by its ID."""
        ...

    async def get_by_id(self, doc_id: str) -> str | None:
        """Retrieve a specific document by its ID."""
        ...
