import pytest
import asyncio
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from pocketclaw.memory.protocol import MemoryType, MemoryEntry
from pocketclaw.memory.file_store import FileMemoryStore
from pocketclaw.memory.manager import MemoryManager, create_memory_store

@pytest.fixture
def temp_memory_path(tmp_path):
    return tmp_path

@pytest.fixture
def file_store(temp_memory_path):
    return FileMemoryStore(base_path=temp_memory_path)

@pytest.fixture
def memory_manager(temp_memory_path):
    return MemoryManager(base_path=temp_memory_path)

@pytest.mark.asyncio
async def test_concurrent_session_writes(file_store):
    """Test concurrent writes to the same session."""
    session_key = "concurrent_session"
    
    async def write_entry(i):
        entry = MemoryEntry(
            id=f"id-{i}",
            type=MemoryType.SESSION,
            content=f"Message {i}",
            role="user",
            session_key=session_key,
        )
        await file_store.save(entry)

    # Run 50 concurrent writes
    await asyncio.gather(*[write_entry(i) for i in range(50)])

    # Verify all writes persisted (checking count)
    history = await file_store.get_session(session_key)
    # Note: File writes in python 'a' mode are generally atomic for small writes or at least won't corrupt, 
    # but race conditions in read-modify-write (which _save_session_entry does) MIGHT cause data loss if not locked.
    # _save_session_entry does: read -> decode -> append -> write.
    # This IS NOT safe without a lock! 
    # If the test fails, it reveals a bug in implementation (which is the point of the test).
    # Since I didn't add a lock to FileMemoryStore, this test MIGHT fail.
    # The user asked for "Concurrent read/write to the same session" tests.
    
    # Asserting exact count might be flaky if implementation is not thread-safe.
    # But usually `asyncio` is single-threaded, so `await file_store.save` yields.
    # If `save` has `await` points (filesystem I/O), another task runs.
    # `pathlib.read_text` and `write_text` are synchronous blocking calls in standard library (mostly).
    # IF they are blocking, there is no context switch during `read...write`.
    # So it should be safe in standard asyncio unless running in a threadpool.
    # Let's see if `_save_session_entry` is async. IT IS `async def`, but calls synchronous file I/O.
    # So it blocks the loop. Meaning it effectively runs serially.
    # So the test should pass.
    assert len(history) == 50

@pytest.mark.asyncio
async def test_session_compaction(file_store):
    """Test that session history is compacted/truncated."""
    session_key = "compaction_test"
    
    # Write 110 entries
    for i in range(110):
        entry = MemoryEntry(
            id=f"id-{i}",
            type=MemoryType.SESSION,
            content=f"Message {i}",
            role="user",
            session_key=session_key,
        )
        await file_store.save(entry)

    history = await file_store.get_session(session_key)
    assert len(history) == 100
    assert history[0].content == "Message 10" # 0-109 saved, keep last 100 -> 10 to 109.
    assert history[-1].content == "Message 109"

@pytest.mark.asyncio
async def test_unicode_persistence(file_store):
    """Test storing special characters and unicode."""
    session_key = "unicode_test"
    content = "Hello, world! 🌍👋 こんにちは (Kon'nichiwa) Ümlaut"
    
    entry = MemoryEntry(
        id="unicode-id",
        type=MemoryType.SESSION,
        content=content,
        role="user",
        session_key=session_key,
    )
    await file_store.save(entry)
    
    history = await file_store.get_session(session_key)
    assert len(history) == 1
    assert history[0].content == content

@pytest.mark.asyncio
async def test_empty_session_handling(file_store):
    """Test handling of empty or non-existent sessions."""
    # Get non-existent
    history = await file_store.get_session("non_existent")
    assert history == []
    
    # Clear non-existent
    count = await file_store.clear_session("non_existent")
    assert count == 0
    
    # List empty
    sessions = await file_store.list_sessions()
    assert "non_existent" not in sessions

@pytest.mark.asyncio
async def test_session_listing_deletion(memory_manager):
    """Test listing and deleting sessions via Manager."""
    session_key = "list_test_session"
    
    # 1. Create session
    await memory_manager.add_to_session(session_key, "user", "Hello")
    
    # 2. List sessions
    sessions = await memory_manager.list_sessions()
    assert session_key in sessions
    
    # 3. Delete session
    await memory_manager.clear_session(session_key)
    
    # 4. List again
    sessions = await memory_manager.list_sessions()
    assert session_key not in sessions

@pytest.mark.asyncio
async def test_search_no_results(memory_manager):
    """Test search returning no results."""
    results = await memory_manager.search("definitely_not_in_memory_xyz")
    assert results == []

@pytest.mark.asyncio
async def test_auto_learn_triggers_config():
    """Test that use_inference config is correctly passed to factory."""
    # We can't easily mock imports inside the function, but we can verify
    # that MemoryManager stores the config or passes it.
    # Since checking internal state is fragile, let's just create a Manager
    # with specific settings and verify checking "mem0" backend logic (mocked).
    
    # Patch where it is defined since it is imported inside the function
    with patch("pocketclaw.memory.mem0_store.Mem0MemoryStore") as mock_mem0:
        # Mock importlib to pretend mem0 is installed
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = True # mem0 is found
            
            # Create manager asking for mem0 and inference
            mgr = MemoryManager(backend="mem0", use_inference=True)
            mock_mem0.assert_called_with(
                user_id="default",
                data_path=None,
                use_inference=True
            )
            
            # Create manager asking for mem0 and NO inference
            mgr2 = MemoryManager(backend="mem0", use_inference=False)
            mock_mem0.assert_called_with(
                user_id="default",
                data_path=None,
                use_inference=False
            )
