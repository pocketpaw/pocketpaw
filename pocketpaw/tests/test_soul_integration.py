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
    from pocketpaw.soul.manager import _reset_manager

    _reset_manager()
    yield
    _reset_manager()


class TestSoulIntegration:
    async def test_bootstrap_provider_generates_prompt(self):
        from soul_protocol import Soul

        from pocketpaw.paw.soul_bridge import SoulBootstrapProvider

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

        from pocketpaw.paw.soul_bridge import SoulBridge

        soul = await Soul.birth(name="BridgeTest", persona="Test.")
        bridge = SoulBridge(soul)

        await bridge.observe("What is Python?", "Python is a programming language.")
        results = await bridge.recall("Python")
        assert isinstance(results, list)

    async def test_manager_full_lifecycle(self, tmp_path):
        from pocketpaw.config import Settings
        from pocketpaw.soul.manager import SoulManager, _reset_manager

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
        from pocketpaw.soul.manager import SoulManager, _reset_manager

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
        from pocketpaw.soul.manager import SoulManager, _reset_manager

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
