import asyncio

from pocketpaw.memory.manager import create_memory_store
from pocketpaw.memory.protocol import MemoryEntry, MemoryType


async def test_vector_backend():
    print("🚀 Starting Vector Memory Test...")
    
    # Force the backend to vector for this test
    backend = "vector"
    
    try:
        # 2. Initialize the Store via your new Manager logic
        store = create_memory_store(backend=backend)
        print(f"✅ Store initialized: {type(store).__name__}")
        
        # 3. Create a fake memory entry
        test_entry = MemoryEntry(
            id="test-123",
            content="PocketPaw is a powerful AI assistant with vector memory.",
            type=MemoryType.LONG_TERM,
            session_key="test-session"
        )
        
        # 4. Test SAVING
        print("💾 Saving memory...")
        await store.save(test_entry)
        print("✅ Save successful!")
        
        print("⏳ Waiting for ChromaDB to index (2 seconds)...")
        await asyncio.sleep(2)
        
        # 5. Test SEARCHING (Semantic)
        print("🔍 Searching for 'AI assistant'...")
        results = await store.search("AI assistant", limit=1)
        
        if results and "PocketPaw" in results[0].content:
            print(f"🎉 SUCCESS! Found memory: {results[0].content}")
        else:
            if not results:
                print("❌ Search failed: No results returned.")
            else:
                print(f"❌ Search failed: Expected 'PocketPaw' but got '{results[0].content}'")
            
        # 6. Test DELETING
        print("🗑️ Deleting memory...")
        success = await store.delete("test-123")
        if success:
            print("✅ Delete successful!")
            
    except Exception as e:
        print(f"💥 TEST FAILED with error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_vector_backend())
