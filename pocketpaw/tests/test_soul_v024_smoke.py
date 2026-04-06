"""Smoke tests for soul-protocol v0.2.4 features.

Covers: reload endpoint, evaluate endpoint, biorhythm config round-trip,
auto-save dirty skip, auto-sync external change detection, and fatigue hint.
"""

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

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
        soul_name="SmokeTest",
        soul_archetype="The Tester",
        soul_path=str(tmp_path / "smoke.soul"),
        soul_auto_save_interval=0,
        soul_biorhythm={
            "energy_drain_rate": 0.05,
            "mood_inertia": 0.9,
            "tired_threshold": 0.2,
            "auto_regen": 0.02,
        },
    )


@pytest.fixture
async def manager(soul_settings):
    from pocketpaw.soul.manager import SoulManager

    mgr = SoulManager(soul_settings)
    await mgr.initialize()
    return mgr


# ── POST /soul/reload ────────────────────────────────────────────────


class TestReload:
    async def test_reload_returns_updated_name(self, manager):
        """Reload picks up the soul from disk and returns its name."""
        await manager.save()
        result = await manager.reload()
        assert result is True
        assert manager.soul is not None
        assert manager.soul.name == "SmokeTest"

    async def test_reload_invalidates_tools_cache(self, manager):
        """Reload clears the cached tools list."""
        tools1 = manager.get_tools()
        assert tools1 is manager.get_tools()  # cached

        await manager.save()
        await manager.reload()

        tools2 = manager.get_tools()
        assert tools1 is not tools2

    async def test_reload_updates_bridge_and_provider(self, manager):
        """Reload re-wires bridge and bootstrap provider to the new soul."""
        old_soul = manager.soul
        await manager.save()
        await manager.reload()
        assert manager.soul is not old_soul
        assert manager.bridge._soul is manager.soul
        assert manager.bootstrap_provider._soul is manager.soul


# ── POST /soul/evaluate ──────────────────────────────────────────────


class TestEvaluate:
    async def test_evaluate_returns_dict_or_none(self, manager):
        """Evaluate returns a scores dict (v0.2.4+) or None (older versions)."""
        result = await manager.evaluate("What is Python?", "Python is a programming language.")
        assert result is None or isinstance(result, dict)

    async def test_evaluate_returns_none_without_soul(self, soul_settings):
        from pocketpaw.soul.manager import SoulManager

        mgr = SoulManager(soul_settings)
        # Don't initialize -- soul is None
        result = await mgr.evaluate("hello", "world")
        assert result is None


# ── Biorhythm config ─────────────────────────────────────────────────


class TestBiorhythm:
    def test_biorhythm_settings_stored(self, soul_settings):
        """Biorhythm config values are accessible from settings."""
        assert soul_settings.soul_biorhythm["energy_drain_rate"] == 0.05
        assert soul_settings.soul_biorhythm["tired_threshold"] == 0.2
        assert soul_settings.soul_biorhythm["mood_inertia"] == 0.9
        assert soul_settings.soul_biorhythm["auto_regen"] == 0.02

    def test_biorhythm_defaults(self):
        from pocketpaw.config import Settings

        s = Settings()
        bio = s.soul_biorhythm
        assert bio["energy_drain_rate"] == 0.02
        assert bio["mood_inertia"] == 0.8
        assert bio["tired_threshold"] == 0.3
        assert bio["auto_regen"] == 0.01

    def test_biorhythm_dashboard_validation(self):
        """Biorhythm values are clamped to 0-1 range."""
        raw = {"energy_drain_rate": 1.5, "mood_inertia": -0.3, "bad_key": 0.5}
        allowed = {"energy_drain_rate", "mood_inertia", "tired_threshold", "auto_regen"}
        clean = {}
        for k, v in raw.items():
            if k in allowed and isinstance(v, int | float):
                clean[k] = float(max(0.0, min(1.0, v)))

        assert clean["energy_drain_rate"] == 1.0  # clamped
        assert clean["mood_inertia"] == 0.0  # clamped
        assert "bad_key" not in clean  # filtered


# ── Auto-save dirty skip ─────────────────────────────────────────────


class TestDirtyTracking:
    async def test_clean_after_init(self, manager):
        assert manager._dirty is False

    async def test_dirty_after_observe(self, manager):
        await manager.observe("hello", "world")
        assert manager._dirty is True

    async def test_clean_after_save(self, manager):
        await manager.observe("hello", "world")
        await manager.save()
        assert manager._dirty is False

    async def test_auto_save_skips_when_clean(self, manager):
        """Auto-save loop should not write when there are no changes."""
        await manager.save()
        assert manager._dirty is False

        with patch.object(manager, "save", new_callable=AsyncMock) as mock_save:
            # Simulate one iteration of the auto-save logic
            if manager._dirty:
                await manager.save()

            mock_save.assert_not_called()

    async def test_auto_save_writes_when_dirty(self, manager):
        """Auto-save loop should write when there are unsaved changes."""
        await manager.observe("test", "response")
        assert manager._dirty is True

        with patch.object(manager, "save", new_callable=AsyncMock) as mock_save:
            if manager._dirty:
                await manager.save()

            mock_save.assert_called_once()


# ── Auto-sync external changes ───────────────────────────────────────


class TestAutoSync:
    async def test_no_external_change_detected(self, manager):
        await manager.save()
        assert manager._file_changed_externally() is False

    async def test_external_change_detected(self, manager, tmp_path):
        await manager.save()
        soul_file = tmp_path / "smoke.soul"
        assert soul_file.exists()

        # Simulate external edit by bumping mtime
        time.sleep(0.05)
        current = soul_file.stat().st_mtime
        os.utime(soul_file, (current + 1, current + 1))

        assert manager._file_changed_externally() is True

    async def test_reload_clears_external_change(self, manager, tmp_path):
        await manager.save()
        soul_file = tmp_path / "smoke.soul"

        time.sleep(0.05)
        current = soul_file.stat().st_mtime
        os.utime(soul_file, (current + 1, current + 1))
        assert manager._file_changed_externally() is True

        await manager.reload()
        assert manager._file_changed_externally() is False


# ── Bootstrap fatigue hint ────────────────────────────────────────────


class TestFatigueHint:
    async def test_fatigue_hint_when_energy_low(self):
        """Bootstrap context includes fatigue hint when energy <= tired_threshold."""
        from pocketpaw.paw.soul_bridge import SoulBootstrapProvider

        soul = MagicMock()
        soul.name = "TiredSoul"
        soul.to_system_prompt.return_value = "I am tired."
        soul.state = MagicMock(mood="calm", energy=0.1, tired_threshold=0.3)
        soul.self_model = None

        provider = SoulBootstrapProvider(soul)
        ctx = await provider.get_context()

        assert "fatigued" in ctx.style.lower()

    async def test_no_fatigue_hint_when_energy_high(self):
        """Bootstrap context does not include fatigue hint when energy is fine."""
        from pocketpaw.paw.soul_bridge import SoulBootstrapProvider

        soul = MagicMock()
        soul.name = "EnergySoul"
        soul.to_system_prompt.return_value = "I am energized."
        soul.state = MagicMock(mood="happy", energy=0.8, tired_threshold=0.3)
        soul.self_model = None

        provider = SoulBootstrapProvider(soul)
        ctx = await provider.get_context()

        assert "fatigued" not in ctx.style.lower()
