"""Tests for memory isolation — sender-scoped memory and identity injection."""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.memory.file_store import FileMemoryStore
from pocketclaw.memory.manager import MemoryManager
from pocketclaw.memory.protocol import MemoryEntry, MemoryType

# ---------------------------------------------------------------------------
# _resolve_user_id
# ---------------------------------------------------------------------------


class TestResolveUserId:
    """Tests for MemoryManager._resolve_user_id()."""

    def _make_manager(self) -> MemoryManager:
        store = MagicMock(spec=["save", "get_by_type", "get_session", "search"])
        return MemoryManager(store=store)

    def test_no_sender_returns_default(self):
        mgr = self._make_manager()
        assert mgr._resolve_user_id(None) == "default"
        assert mgr._resolve_user_id("") == "default"

    @patch("pocketclaw.config.get_settings")
    def test_no_owner_id_returns_default(self, mock_settings):
        mock_settings.return_value = MagicMock(owner_id="")
        mgr = self._make_manager()
        assert mgr._resolve_user_id("some_sender") == "default"

    @patch("pocketclaw.config.get_settings")
    def test_sender_is_owner_returns_default(self, mock_settings):
        mock_settings.return_value = MagicMock(owner_id="owner123")
        mgr = self._make_manager()
        assert mgr._resolve_user_id("owner123") == "default"

    @patch("pocketclaw.config.get_settings")
    def test_non_owner_returns_hash(self, mock_settings):
        mock_settings.return_value = MagicMock(owner_id="owner123")
        mgr = self._make_manager()
        result = mgr._resolve_user_id("stranger456")
        expected = hashlib.sha256(b"stranger456").hexdigest()[:16]
        assert result == expected

    @patch("pocketclaw.config.get_settings")
    def test_hash_is_deterministic(self, mock_settings):
        mock_settings.return_value = MagicMock(owner_id="owner123")
        mgr = self._make_manager()
        assert mgr._resolve_user_id("bob") == mgr._resolve_user_id("bob")

    @patch("pocketclaw.config.get_settings")
    def test_different_senders_different_hashes(self, mock_settings):
        mock_settings.return_value = MagicMock(owner_id="owner123")
        mgr = self._make_manager()
        assert mgr._resolve_user_id("alice") != mgr._resolve_user_id("bob")


# ---------------------------------------------------------------------------
# File store per-user routing
# ---------------------------------------------------------------------------


class TestFileStoreUserScoping:
    """Tests for FileMemoryStore per-user MEMORY.md routing."""

    def test_get_user_memory_file_default(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)
        result = store._get_user_memory_file("default")
        assert result == store.long_term_file

    def test_get_user_memory_file_non_default(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)
        result = store._get_user_memory_file("abc123")
        assert result == tmp_path / "users" / "abc123" / "MEMORY.md"
        assert result.parent.exists()  # dir auto-created

    async def test_save_long_term_default_user(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)
        entry = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content="Owner fact",
            tags=[],
            metadata={"header": "Test"},
        )
        await store.save(entry)
        assert store.long_term_file.exists()
        assert "Owner fact" in store.long_term_file.read_text()

    async def test_save_long_term_scoped_user(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)
        entry = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content="Stranger fact",
            tags=[],
            metadata={"header": "Test", "user_id": "abc123"},
        )
        await store.save(entry)
        user_file = tmp_path / "users" / "abc123" / "MEMORY.md"
        assert user_file.exists()
        assert "Stranger fact" in user_file.read_text()
        # Root MEMORY.md should NOT contain it
        if store.long_term_file.exists():
            assert "Stranger fact" not in store.long_term_file.read_text()

    async def test_get_by_type_scoped(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)
        # Save owner memory
        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Owner data",
                tags=[],
                metadata={"header": "A"},
            )
        )
        # Save scoped memory
        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Scoped data",
                tags=[],
                metadata={"header": "B", "user_id": "xyz"},
            )
        )
        # Unscoped retrieval returns both
        all_lt = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(all_lt) == 2

        # Scoped retrieval returns only matching
        owner_lt = await store.get_by_type(MemoryType.LONG_TERM, user_id="default")
        assert len(owner_lt) == 1
        assert owner_lt[0].content == "Owner data"

        scoped_lt = await store.get_by_type(MemoryType.LONG_TERM, user_id="xyz")
        assert len(scoped_lt) == 1
        assert scoped_lt[0].content == "Scoped data"

    async def test_daily_notes_stay_global(self, tmp_path):
        """Daily notes should NOT be scoped to user_id."""
        store = FileMemoryStore(base_path=tmp_path)
        entry = MemoryEntry(
            id="",
            type=MemoryType.DAILY,
            content="Daily note",
            tags=[],
            metadata={"header": "10:00"},
        )
        await store.save(entry)
        daily = await store.get_by_type(MemoryType.DAILY)
        assert len(daily) == 1

    async def test_load_index_includes_user_files(self, tmp_path):
        """Verify _load_index picks up per-user MEMORY.md files."""
        # Create a user memory file manually
        user_dir = tmp_path / "users" / "test_user"
        user_dir.mkdir(parents=True)
        (user_dir / "MEMORY.md").write_text("## Fact\n\nUser prefers Python.", encoding="utf-8")
        store = FileMemoryStore(base_path=tmp_path)
        # Should have loaded the user memory
        lt = await store.get_by_type(MemoryType.LONG_TERM)
        assert any("Python" in e.content for e in lt)

    async def test_parsed_user_id_from_path(self, tmp_path):
        """User entries parsed from users/{id}/MEMORY.md get user_id in metadata."""
        user_dir = tmp_path / "users" / "abc999"
        user_dir.mkdir(parents=True)
        (user_dir / "MEMORY.md").write_text("## Pref\n\nLikes dark mode.", encoding="utf-8")
        store = FileMemoryStore(base_path=tmp_path)
        lt = await store.get_by_type(MemoryType.LONG_TERM, user_id="abc999")
        assert len(lt) == 1
        assert lt[0].metadata.get("user_id") == "abc999"


# ---------------------------------------------------------------------------
# MemoryManager integration
# ---------------------------------------------------------------------------


class TestMemoryManagerScoping:
    """Integration tests for MemoryManager with sender_id."""

    @patch("pocketclaw.config.get_settings")
    async def test_remember_sets_user_id(self, mock_settings, tmp_path):
        mock_settings.return_value = MagicMock(owner_id="owner1")
        store = FileMemoryStore(base_path=tmp_path)
        mgr = MemoryManager(store=store)
        await mgr.remember("test fact", sender_id="stranger")
        lt = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(lt) == 1
        assert lt[0].metadata.get("user_id") != "default"

    @patch("pocketclaw.config.get_settings")
    async def test_remember_owner_uses_default(self, mock_settings, tmp_path):
        mock_settings.return_value = MagicMock(owner_id="owner1")
        store = FileMemoryStore(base_path=tmp_path)
        mgr = MemoryManager(store=store)
        await mgr.remember("owner fact", sender_id="owner1")
        lt = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(lt) == 1
        assert lt[0].metadata.get("user_id") == "default"

    @patch("pocketclaw.config.get_settings")
    async def test_get_context_scoped(self, mock_settings, tmp_path):
        mock_settings.return_value = MagicMock(owner_id="owner1")
        store = FileMemoryStore(base_path=tmp_path)
        mgr = MemoryManager(store=store)
        await mgr.remember("Owner secret", sender_id="owner1")
        await mgr.remember("Stranger info", sender_id="stranger")

        # Owner context should have owner secret, not stranger info
        ctx = await mgr.get_context_for_agent(sender_id="owner1")
        assert "Owner secret" in ctx
        assert "Stranger info" not in ctx

        # Stranger context should have stranger info, not owner secret
        ctx2 = await mgr.get_context_for_agent(sender_id="stranger")
        assert "Stranger info" in ctx2
        assert "Owner secret" not in ctx2

    async def test_backward_compat_no_sender(self, tmp_path):
        """No sender_id + no owner_id → everything is 'default'."""
        store = FileMemoryStore(base_path=tmp_path)
        mgr = MemoryManager(store=store)
        await mgr.remember("Global fact")
        ctx = await mgr.get_context_for_agent()
        assert "Global fact" in ctx


# ---------------------------------------------------------------------------
# Context Builder identity injection
# ---------------------------------------------------------------------------


class TestContextBuilderIdentity:
    """Tests for sender identity block in system prompt."""

    @patch("pocketclaw.config.get_settings")
    async def test_owner_identity_block(self, mock_settings):
        from pocketclaw.bootstrap.context_builder import AgentContextBuilder

        mock_settings.return_value = MagicMock(owner_id="owner1")
        memory = MagicMock()
        memory.get_semantic_context = AsyncMock(return_value="")
        memory.get_context_for_agent = AsyncMock(return_value="")
        bootstrap = MagicMock()
        ctx_obj = MagicMock()
        ctx_obj.to_system_prompt.return_value = "base prompt"
        bootstrap.get_context = AsyncMock(return_value=ctx_obj)

        builder = AgentContextBuilder(bootstrap_provider=bootstrap, memory_manager=memory)
        prompt = await builder.build_system_prompt(user_query="hi", sender_id="owner1")
        assert "role: owner" in prompt
        assert "This is your owner" in prompt

    @patch("pocketclaw.config.get_settings")
    async def test_external_user_identity_block(self, mock_settings):
        from pocketclaw.bootstrap.context_builder import AgentContextBuilder

        mock_settings.return_value = MagicMock(owner_id="owner1")
        memory = MagicMock()
        memory.get_semantic_context = AsyncMock(return_value="")
        memory.get_context_for_agent = AsyncMock(return_value="")
        bootstrap = MagicMock()
        ctx_obj = MagicMock()
        ctx_obj.to_system_prompt.return_value = "base prompt"
        bootstrap.get_context = AsyncMock(return_value=ctx_obj)

        builder = AgentContextBuilder(bootstrap_provider=bootstrap, memory_manager=memory)
        prompt = await builder.build_system_prompt(user_query="hi", sender_id="stranger")
        assert "role: external user" in prompt
        assert "NOT your owner" in prompt

    async def test_no_owner_id_no_identity_block(self):
        from pocketclaw.bootstrap.context_builder import AgentContextBuilder

        memory = MagicMock()
        memory.get_semantic_context = AsyncMock(return_value="")
        memory.get_context_for_agent = AsyncMock(return_value="")
        bootstrap = MagicMock()
        ctx_obj = MagicMock()
        ctx_obj.to_system_prompt.return_value = "base prompt"
        bootstrap.get_context = AsyncMock(return_value=ctx_obj)

        builder = AgentContextBuilder(bootstrap_provider=bootstrap, memory_manager=memory)
        prompt = await builder.build_system_prompt(user_query="hi")
        assert "sender_id=" not in prompt

    @patch("pocketclaw.config.get_settings")
    async def test_no_owner_configured_no_block(self, mock_settings):
        from pocketclaw.bootstrap.context_builder import AgentContextBuilder

        mock_settings.return_value = MagicMock(owner_id="")
        memory = MagicMock()
        memory.get_semantic_context = AsyncMock(return_value="")
        bootstrap = MagicMock()
        ctx_obj = MagicMock()
        ctx_obj.to_system_prompt.return_value = "base prompt"
        bootstrap.get_context = AsyncMock(return_value=ctx_obj)

        builder = AgentContextBuilder(bootstrap_provider=bootstrap, memory_manager=memory)
        prompt = await builder.build_system_prompt(user_query="hi", sender_id="someone")
        # No identity block when owner_id is empty
        assert "role: owner" not in prompt
        assert "role: external user" not in prompt
