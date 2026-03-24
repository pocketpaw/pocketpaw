import asyncio

import pytest

from pocketpaw.memory.manager import create_memory_store
from pocketpaw.memory.protocol import MemoryEntry, MemoryType


@pytest.mark.asyncio
async def test_vector_backend_lifecycle():
    """
    Tests the full lifecycle of VectorMemory using proper assertions.
    Reflects changes requested by DevRohit06.
    """
    # 1. Setup
    backend = "vector"
    store = create_memory_store(backend=backend)
    
    # Ensure we got the right store class
    assert store.__class__.__name__ == "VectorMemory"

    test_id = "pytest-123"
    test_entry = MemoryEntry(
        id=test_id,
        content="PocketPaw uses ChromaDB for semantic vector search.",
        type=MemoryType.LONG_TERM,
        session_key="test-session",
        tags=["unit-test"]
    )

    # 2. Test Save
    # We don't use try-except; if it fails, pytest will report it correctly.
    saved_id = await store.save(test_entry)
    assert saved_id == test_id

    # 3. Wait for indexing
    # ChromaDB indexing is usually fast but needs a small breather in local tests
    await asyncio.sleep(1)

    # 4. Test Search (Semantic)
    # Searching for a keyword that is not exact but semantically close
    results = await store.search(query="semantic vector", limit=1)
    
    assert len(results) > 0, "Search should return at least one result"
    assert "PocketPaw" in results[0].content
    assert results[0].id == test_id
    assert results[0].type == MemoryType.LONG_TERM

    # 5. Test Get by ID
    retrieved = await store.get(test_id)
    assert retrieved is not None
    assert retrieved.content == test_entry.content

    # 6. Test Delete
    success = await store.delete(test_id)
    assert success is True

    # 7. Verify Deletion
    # After delete, searching or getting should not return the entry
    after_delete = await store.get(test_id)
    assert after_delete is None