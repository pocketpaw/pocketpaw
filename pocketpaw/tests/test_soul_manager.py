"""Tests for SoulManager lifecycle."""

import asyncio

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


@pytest.fixture
def soul_settings(tmp_path):
    from pocketpaw.config import Settings

    return Settings(
        soul_enabled=True,
        soul_name="TestSoul",
        soul_archetype="The Test Helper",
        soul_path=str(tmp_path / "test.soul"),
        soul_auto_save_interval=0,
    )


class TestSoulManager:
    async def test_initialize_births_new_soul(self, soul_settings):
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        assert mgr.soul is not None
        assert mgr.soul.name == "TestSoul"
        assert mgr.bridge is not None
        assert mgr.bootstrap_provider is not None

    async def test_save_and_reawaken(self, soul_settings, tmp_path):
        from pocketpaw.soul.manager import SoulManager, _reset_manager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        await mgr.save()
        assert (tmp_path / "test.soul").exists()

        _reset_manager()
        mgr2 = SoulManager(soul_settings)
        await mgr2.initialize()
        assert mgr2.soul.name == "TestSoul"

    async def test_observe_does_not_raise(self, soul_settings):
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        await mgr.observe("Hello", "Hi there!")

    async def test_get_tools_returns_six(self, soul_settings):
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        tools = mgr.get_tools()
        assert len(tools) == 6
        names = {t.name for t in tools}
        assert names == {
            "soul_remember",
            "soul_recall",
            "soul_edit_core",
            "soul_status",
            "soul_evaluate",
            "soul_reload",
        }

    async def test_corrupt_soul_file_falls_back_to_birth(self, soul_settings, tmp_path):
        from pocketpaw.soul.manager import SoulManager

        soul_file = tmp_path / "test.soul"
        soul_file.write_text("this is not a valid soul file")

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        assert mgr.soul is not None
        assert mgr.soul.name == "TestSoul"
        backup = tmp_path / "test.soul.corrupt"
        assert backup.exists()

    async def test_concurrent_observe_is_serialized(self, soul_settings):
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()

        tasks = [mgr.observe(f"msg {i}", f"reply {i}") for i in range(10)]
        await asyncio.gather(*tasks)

    async def test_shutdown_saves_and_stops_autosave(self, tmp_path):
        from pocketpaw.config import Settings
        from pocketpaw.soul.manager import SoulManager

        settings = Settings(
            soul_enabled=True,
            soul_name="ShutdownTest",
            soul_path=str(tmp_path / "shutdown.soul"),
            soul_auto_save_interval=1,
        )
        mgr = SoulManager(settings)
        await mgr.initialize()
        mgr.start_auto_save()

        await mgr.observe("test", "test reply")
        await mgr.shutdown()

        assert (tmp_path / "shutdown.soul").exists()
        assert mgr._auto_save_task is None or mgr._auto_save_task.done()

    async def test_import_from_soul_file(self, soul_settings, tmp_path):
        """Import a .soul file replaces the current soul."""
        # Birth and export a soul to a separate file
        from soul_protocol import Soul

        from pocketpaw.soul.manager import SoulManager

        donor = await Soul.birth(name="Donor", persona="I am the donor soul.")
        donor_path = tmp_path / "donor.soul"
        await donor.export(donor_path)

        # Initialize manager with default soul
        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        assert mgr.soul.name == "TestSoul"

        # Import the donor soul
        name = await mgr.import_from_file(donor_path)
        assert name == "Donor"
        assert mgr.soul.name == "Donor"
        # Should have been saved to the manager's configured path
        assert (tmp_path / "test.soul").exists()

    async def test_import_from_yaml_config(self, soul_settings, tmp_path):
        """Import a YAML config births a new soul from it."""
        from pocketpaw.soul.manager import SoulManager

        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "name: YamlSoul\n"
            "archetype: The Yaml Expert\n"
            "values: [clarity, speed]\n"
            "persona: I was born from YAML.\n"
        )

        mgr = SoulManager(soul_settings)
        await mgr.initialize()

        name = await mgr.import_from_file(yaml_path)
        assert name == "YamlSoul"
        assert mgr.soul.name == "YamlSoul"

    async def test_import_from_json_config(self, soul_settings, tmp_path):
        """Import a JSON config births a new soul from it."""
        import json

        from pocketpaw.soul.manager import SoulManager

        json_path = tmp_path / "config.json"
        json_path.write_text(
            json.dumps(
                {
                    "name": "JsonSoul",
                    "archetype": "The Json Expert",
                    "persona": "I was born from JSON.",
                }
            )
        )

        mgr = SoulManager(soul_settings)
        await mgr.initialize()

        name = await mgr.import_from_file(json_path)
        assert name == "JsonSoul"

    async def test_import_updates_bootstrap_provider_in_place(self, soul_settings, tmp_path):
        """Import must update the existing bootstrap provider, not replace it.

        AgentContextBuilder holds a reference to the original provider,
        so replacing it with a new instance would leave the builder stale.
        """
        from soul_protocol import Soul

        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()

        # Grab the reference that AgentContextBuilder would hold
        original_provider = mgr.bootstrap_provider
        original_bridge = mgr.bridge

        # Import a different soul
        donor = await Soul.birth(name="NewIdentity", persona="I am the new identity.")
        donor_path = tmp_path / "new.soul"
        await donor.export(donor_path)
        await mgr.import_from_file(donor_path)

        # Same object references, but now pointing to the new soul
        assert mgr.bootstrap_provider is original_provider
        assert mgr.bridge is original_bridge

        # The provider should generate context for the NEW soul
        ctx = await mgr.bootstrap_provider.get_context()
        assert ctx.name == "NewIdentity"

    async def test_import_unsupported_format_raises(self, soul_settings, tmp_path):
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()

        bad_file = tmp_path / "config.txt"
        bad_file.write_text("not supported")

        with pytest.raises(ValueError, match="Unsupported file format"):
            await mgr.import_from_file(bad_file)

    async def test_import_missing_file_raises(self, soul_settings, tmp_path):
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()

        with pytest.raises(FileNotFoundError):
            await mgr.import_from_file(tmp_path / "nonexistent.soul")

    async def test_reload_from_disk(self, soul_settings, tmp_path):
        """Reload picks up changes from disk."""
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        await mgr.save()

        # Reload should succeed when file exists
        result = await mgr.reload()
        assert result is True
        assert mgr.soul is not None

    async def test_reload_returns_false_when_no_file(self, soul_settings, tmp_path):
        """Reload returns False when .soul file doesn't exist."""
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        # Don't save, so no file on disk yet -- remove any that initialize may have created
        soul_file = tmp_path / "test.soul"
        soul_file.unlink(missing_ok=True)

        result = await mgr.reload()
        assert result is False

    async def test_evaluate_returns_none_when_unsupported(self, soul_settings):
        """Evaluate returns None when soul doesn't have evaluate method."""
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        # If soul-protocol < 0.2.4, evaluate() won't exist
        result = await mgr.evaluate("hello", "hi there")
        # Result is either None (no method) or a dict (method exists)
        assert result is None or isinstance(result, dict)

    async def test_dirty_tracking(self, soul_settings):
        """Dirty flag is set after observe and cleared after save."""
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        assert mgr._dirty is False

        await mgr.observe("hello", "world")
        assert mgr._dirty is True

        await mgr.save()
        assert mgr._dirty is False

    async def test_tools_are_cached(self, soul_settings):
        """get_tools() returns the same list on repeated calls."""
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        tools1 = mgr.get_tools()
        tools2 = mgr.get_tools()
        assert tools1 is tools2

    async def test_tools_cache_invalidated_on_import(self, soul_settings, tmp_path):
        """Importing a soul invalidates the tools cache."""
        from soul_protocol import Soul

        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        tools1 = mgr.get_tools()

        donor = await Soul.birth(name="CacheTest", persona="Testing cache invalidation.")
        donor_path = tmp_path / "donor.soul"
        await donor.export(donor_path)
        await mgr.import_from_file(donor_path)

        tools2 = mgr.get_tools()
        assert tools1 is not tools2

    async def test_external_file_change_detection(self, soul_settings, tmp_path):
        """_file_changed_externally detects mtime changes."""
        import os
        import time

        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        await mgr.initialize()
        await mgr.save()

        assert mgr._file_changed_externally() is False

        # Simulate external modification by touching the file with a future mtime
        soul_file = tmp_path / "test.soul"
        time.sleep(0.05)
        current = soul_file.stat().st_mtime
        os.utime(soul_file, (current + 1, current + 1))

        assert mgr._file_changed_externally() is True

    async def test_biorhythm_settings_passed(self, tmp_path):
        """Biorhythm config is included in settings."""
        from pocketpaw.config import Settings

        settings = Settings(
            soul_enabled=True,
            soul_name="BioTest",
            soul_path=str(tmp_path / "bio.soul"),
            soul_auto_save_interval=0,
            soul_biorhythm={
                "energy_drain_rate": 0.05,
                "mood_inertia": 0.9,
                "tired_threshold": 0.2,
                "auto_regen": 0.02,
            },
        )
        assert settings.soul_biorhythm["energy_drain_rate"] == 0.05
        assert settings.soul_biorhythm["tired_threshold"] == 0.2
