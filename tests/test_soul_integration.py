"""Integration tests: soul-protocol + PocketPaw wiring."""

import pytest


def _has_soul_protocol() -> bool:
    try:
        import soul_protocol  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(not _has_soul_protocol(), reason="soul-protocol not installed")


@pytest.fixture(autouse=True)
def _reset_soul():
    from pocketpaw.soul._manager import _reset_manager

    _reset_manager()
    yield
    _reset_manager()


class TestSoulIntegration:
    async def test_bootstrap_provider_generates_prompt(self):
        from soul_protocol import Soul

        from pocketpaw.soul import SoulBootstrapProvider

        soul = await Soul.birth(
            name="IntegTest",
            archetype="Test Agent",
            persona="I am a test agent.",
        )
        provider = SoulBootstrapProvider(soul)
        ctx = await provider.get_context()

        assert ctx.name == "IntegTest"
        assert len(ctx.identity) > 0

    async def test_bridge_observe_and_recall(self):
        from soul_protocol import Soul

        from pocketpaw.soul import SoulBridge

        soul = await Soul.birth(name="BridgeTest", persona="Test.")
        bridge = SoulBridge(soul)

        await bridge.observe("What is Python?", "Python is a programming language.")
        results = await bridge.recall("Python")
        assert isinstance(results, list)

    async def test_manager_full_lifecycle(self, tmp_path):
        from pocketpaw.config import Settings
        from pocketpaw.soul import SoulManager
        from pocketpaw.soul._manager import _reset_manager

        _reset_manager()
        settings = Settings(
            soul_enabled=True,
            soul_name="LifecycleTest",
            soul_archetype="The Tester",
            soul_path=str(tmp_path / "lifecycle.soul"),
            soul_auto_save_interval=0,
        )

        mgr = SoulManager(settings)
        await mgr.initialize()
        assert mgr.soul.name == "LifecycleTest"

        await mgr.observe("test input", "test output")
        await mgr.save()
        assert (tmp_path / "lifecycle.soul").exists()

        # Re-awaken
        _reset_manager()
        mgr2 = SoulManager(settings)
        await mgr2.initialize()
        assert mgr2.soul.name == "LifecycleTest"
        _reset_manager()

    async def test_soul_tools_injected_into_tool_bridge(self, tmp_path):
        """When soul is active, tool_bridge discovers all soul tools."""
        from pocketpaw.config import Settings
        from pocketpaw.soul import SoulManager
        from pocketpaw.soul._manager import _reset_manager

        _reset_manager()
        settings = Settings(
            soul_enabled=True,
            soul_name="ToolTest",
            soul_path=str(tmp_path / "tools.soul"),
            soul_auto_save_interval=0,
        )
        mgr = SoulManager(settings)
        await mgr.initialize()

        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = _instantiate_all_tools(backend="openai_agents")
        tool_names = {t.name for t in tools}
        assert "soul_remember" in tool_names
        assert "soul_recall" in tool_names
        assert "soul_edit_core" in tool_names
        assert "soul_status" in tool_names
        assert "soul_evaluate" in tool_names
        assert "soul_reload" in tool_names

        _reset_manager()

    async def test_corrupt_file_recovery_end_to_end(self, tmp_path):
        """Corrupt .soul file triggers backup + fresh birth."""
        from pocketpaw.config import Settings
        from pocketpaw.soul import SoulManager
        from pocketpaw.soul._manager import _reset_manager

        _reset_manager()
        soul_file = tmp_path / "corrupt.soul"
        soul_file.write_bytes(b"CORRUPT DATA HERE")

        settings = Settings(
            soul_enabled=True,
            soul_name="RecoveryTest",
            soul_path=str(soul_file),
            soul_auto_save_interval=0,
        )
        mgr = SoulManager(settings)
        await mgr.initialize()

        # Should have recovered
        assert mgr.soul is not None
        assert mgr.soul.name == "RecoveryTest"

        # Corrupt file backed up
        assert (tmp_path / "corrupt.soul.corrupt").exists()

        _reset_manager()

    async def test_bootstrap_recall_requests_procedural_and_semantic(self):
        """SVL-4: the bootstrap auto-recall must request BOTH SEMANTIC and
        PROCEDURAL memory types, and surface whatever recall returns into the
        knowledge context.

        Minted correction-rules (CorrectionSoulBridge writes them as
        ``type="procedural"`` → ``MemoryType.PROCEDURAL``) and session-learned
        how-tos were previously excluded by a ``types=[SEMANTIC]``-only filter,
        so they could never reach the bootstrap system prompt.

        This test drives the seam deterministically by stubbing ``soul.recall``:
        it asserts (1) PROCEDURAL is now in the requested ``types`` set, and
        (2) a returned PROCEDURAL entry's content surfaces in ``ctx.knowledge``
        alongside a SEMANTIC one (no regression). It does NOT depend on the
        underlying store's relevance scoring — see
        ``test_bootstrap_empty_query_recall_is_inert`` for the separate,
        pre-existing limitation that the empty query returns nothing from the
        real BM25 store.
        """
        from unittest.mock import AsyncMock

        from soul_protocol import MemoryEntry, MemoryType, Soul

        from pocketpaw.soul import SoulBootstrapProvider

        soul = await Soul.birth(
            name="RecallFilterTest",
            archetype="Test Agent",
            persona="I am a test agent.",
        )

        semantic_entry = MemoryEntry(
            type=MemoryType.SEMANTIC,
            content="The user's company is named Acme Corp.",
            importance=8,
        )
        procedural_entry = MemoryEntry(
            type=MemoryType.PROCEDURAL,
            content="Always greet the user by their first name.",
            importance=7,
        )

        recall_mock = AsyncMock(return_value=[semantic_entry, procedural_entry])
        soul.recall = recall_mock  # type: ignore[method-assign]

        provider = SoulBootstrapProvider(soul)
        ctx = await provider.get_context()

        # (1) The auto-recall must request PROCEDURAL alongside SEMANTIC.
        recall_mock.assert_awaited()
        requested_types = recall_mock.await_args.kwargs["types"]
        assert MemoryType.PROCEDURAL in requested_types, (
            "bootstrap recall no longer requests PROCEDURAL memories — "
            "minted correction-rules would be structurally excluded again"
        )
        assert MemoryType.SEMANTIC in requested_types, (
            "bootstrap recall dropped SEMANTIC memories — regression"
        )

        # (2) Both surface in the knowledge context.
        knowledge_blob = "\n".join(ctx.knowledge)
        assert "Always greet the user by their first name." in knowledge_blob, (
            "PROCEDURAL memory did not surface in bootstrap context"
        )
        assert "The user's company is named Acme Corp." in knowledge_blob, (
            "SEMANTIC memory did not surface in bootstrap context"
        )

    async def test_bootstrap_empty_query_recall_is_inert(self):
        """Documents a pre-existing limitation discovered in SVL-4.

        ``get_context`` recalls with ``query=""``. In soul-protocol 0.4.0 the
        memory stores are BM25/token-overlap based and only return entries
        whose relevance score is > 0. An empty query produces zero tokens, so
        recall returns NOTHING regardless of memory type — the SEMANTIC-only
        filter was never actually surfacing memories either.

        The SVL-4 ``types`` fix is necessary (PROCEDURAL is no longer excluded)
        but not sufficient on its own: until the empty-query path is addressed
        (a captain-scoped change to the query/retrieval strategy), neither
        SEMANTIC nor PROCEDURAL memories reach the bootstrap from the real
        store. This test locks in that observed behavior so a future fix that
        makes the empty query return results will flip it and prompt a review.
        """
        from soul_protocol import MemoryType, Soul

        soul = await Soul.birth(
            name="EmptyQueryProbe",
            archetype="Test Agent",
            persona="I am a test agent.",
        )
        await soul.remember(
            "The user's company is named Acme Corp.",
            type=MemoryType.SEMANTIC,
            importance=8,
        )
        await soul.remember(
            "Always greet the user by their first name.",
            type=MemoryType.PROCEDURAL,
            importance=7,
        )

        # Empty query == the literal call get_context makes today.
        empty = await soul.recall(
            query="",
            types=[MemoryType.SEMANTIC, MemoryType.PROCEDURAL],
            limit=5,
        )
        assert empty == [], (
            "empty-query recall now returns results — the SVL-4 follow-up "
            "(non-empty bootstrap query) may have landed; revisit get_context"
        )

        # A token-bearing query DOES surface both, confirming the data is there
        # and only the empty query is the blocker.
        hit = await soul.recall(
            query="company greet user",
            types=[MemoryType.SEMANTIC, MemoryType.PROCEDURAL],
            limit=5,
        )
        contents = {m.content for m in hit}
        assert "The user's company is named Acme Corp." in contents
        assert "Always greet the user by their first name." in contents
