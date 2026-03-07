"""
Tests for the Proactive Daemon module.
"""

import pytest

from pocketpaw.daemon import (
    ContextHub,
    IntentionStore,
    ProactiveDaemon,
    TriggerEngine,
)
from pocketpaw.daemon.triggers import CRON_PRESETS, parse_cron_expression


class TestCronParsing:
    """Test cron expression parsing."""

    def test_parse_standard_cron(self):
        """Test parsing standard 5-field cron expression."""
        result = parse_cron_expression("0 8 * * 1-5")
        assert result["minute"] == "0"
        assert result["hour"] == "8"
        assert result["day"] == "*"
        assert result["month"] == "*"
        assert result["day_of_week"] == "1-5"

    def test_parse_preset(self):
        """Test parsing cron preset name."""
        result = parse_cron_expression("weekday_morning_8am")
        assert result["minute"] == "0"
        assert result["hour"] == "8"
        assert result["day_of_week"] == "1-5"

    def test_available_presets(self):
        """Test that expected presets are available."""
        expected = [
            "every_minute",
            "every_5_minutes",
            "every_hour",
            "every_morning_8am",
            "weekday_morning_9am",
        ]
        for preset in expected:
            assert preset in CRON_PRESETS

    def test_invalid_cron_raises_error(self):
        """Test that invalid cron expression raises ValueError."""
        with pytest.raises(ValueError):
            parse_cron_expression("invalid")

        with pytest.raises(ValueError):
            parse_cron_expression("0 8 *")  # Only 3 fields


class TestIntentionStore:
    """Test IntentionStore CRUD operations."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        """Create a fresh IntentionStore with temp storage."""
        # Patch the intentions path
        intentions_file = tmp_path / "intentions.json"
        monkeypatch.setattr(
            "pocketpaw.daemon.intentions.get_intentions_path",
            lambda: intentions_file,
        )
        return IntentionStore()

    def test_create_intention(self, store):
        """Test creating an intention."""
        intention = store.create(
            name="Test Intention",
            prompt="Hello {{datetime.time}}",
            trigger={"type": "cron", "schedule": "0 8 * * *"},
            context_sources=["datetime"],
        )

        assert intention["id"]
        assert intention["name"] == "Test Intention"
        assert intention["prompt"] == "Hello {{datetime.time}}"
        assert intention["trigger"]["type"] == "cron"
        assert intention["enabled"] is True
        assert intention["created_at"]

    def test_get_intention(self, store):
        """Test getting an intention by ID."""
        created = store.create(
            name="Get Test",
            prompt="test",
            trigger={"type": "cron", "schedule": "* * * * *"},
        )

        found = store.get_by_id(created["id"])
        assert found is not None
        assert found["name"] == "Get Test"

    def test_update_intention(self, store):
        """Test updating an intention."""
        created = store.create(
            name="Update Test",
            prompt="original",
            trigger={"type": "cron", "schedule": "0 8 * * *"},
        )

        updated = store.update(created["id"], {"name": "Updated Name", "prompt": "updated"})

        assert updated["name"] == "Updated Name"
        assert updated["prompt"] == "updated"
        assert updated["id"] == created["id"]  # ID should not change

    def test_delete_intention(self, store):
        """Test deleting an intention."""
        created = store.create(
            name="Delete Test",
            prompt="test",
            trigger={"type": "cron", "schedule": "* * * * *"},
        )

        result = store.delete(created["id"])
        assert result is True

        found = store.get_by_id(created["id"])
        assert found is None

    def test_toggle_intention(self, store):
        """Test toggling intention enabled state."""
        created = store.create(
            name="Toggle Test",
            prompt="test",
            trigger={"type": "cron", "schedule": "* * * * *"},
            enabled=True,
        )

        toggled = store.toggle(created["id"])
        assert toggled["enabled"] is False

        toggled_again = store.toggle(created["id"])
        assert toggled_again["enabled"] is True

    def test_get_enabled_intentions(self, store):
        """Test filtering enabled intentions."""
        store.create(
            name="Enabled 1",
            prompt="test",
            trigger={"type": "cron", "schedule": "* * * * *"},
            enabled=True,
        )
        store.create(
            name="Disabled",
            prompt="test",
            trigger={"type": "cron", "schedule": "* * * * *"},
            enabled=False,
        )
        store.create(
            name="Enabled 2",
            prompt="test",
            trigger={"type": "cron", "schedule": "* * * * *"},
            enabled=True,
        )

        enabled = store.get_enabled()
        assert len(enabled) == 2
        names = [i["name"] for i in enabled]
        assert "Enabled 1" in names
        assert "Enabled 2" in names
        assert "Disabled" not in names


class TestContextHub:
    """Test ContextHub context gathering."""

    @pytest.fixture
    def hub(self):
        return ContextHub()

    @pytest.mark.asyncio
    async def test_gather_system_status(self, hub):
        """Test gathering system status context."""
        context = await hub.gather(["system_status"])

        assert "system_status" in context
        status = context["system_status"]
        assert "cpu_percent" in status
        assert "memory_percent" in status
        assert "disk_percent" in status

    @pytest.mark.asyncio
    async def test_gather_datetime(self, hub):
        """Test gathering datetime context."""
        context = await hub.gather(["datetime"])

        assert "datetime" in context
        dt = context["datetime"]
        assert "date" in dt
        assert "time" in dt
        assert "day_of_week" in dt

    @pytest.mark.asyncio
    async def test_apply_template(self, hub):
        """Test applying context to template."""
        context = await hub.gather(["datetime"])

        template = "Today is {{datetime.day_of_week}}"
        result = hub.apply_template(template, context)

        assert "Today is" in result
        assert "{{datetime.day_of_week}}" not in result

    @pytest.mark.asyncio
    async def test_format_context_string(self, hub):
        """Test formatting context as string."""
        context = await hub.gather(["system_status", "datetime"])

        formatted = hub.format_context_string(context)

        assert "[System Status]" in formatted
        assert "[Current Time]" in formatted


class TestTriggerEngine:
    """Test TriggerEngine scheduling."""

    @pytest.fixture
    async def engine(self):
        """Create engine in async context for event loop."""
        engine = TriggerEngine()
        yield engine
        engine.stop()

    @pytest.mark.asyncio
    async def test_add_cron_trigger(self, engine):
        """Test adding a cron trigger."""

        # Start engine with async no-op callback
        async def noop(x):
            pass

        engine.start(callback=noop)

        intention = {
            "id": "test-1",
            "name": "Test Intention",
            "trigger": {"type": "cron", "schedule": "0 8 * * *"},
            "enabled": True,
        }

        result = engine.add_intention(intention)
        assert result is True
        assert "test-1" in engine.get_scheduled_intentions()

    @pytest.mark.asyncio
    async def test_remove_trigger(self, engine):
        """Test removing a trigger."""

        async def noop(x):
            pass

        engine.start(callback=noop)

        intention = {
            "id": "test-2",
            "name": "Test Intention",
            "trigger": {"type": "cron", "schedule": "0 8 * * *"},
            "enabled": True,
        }

        engine.add_intention(intention)
        result = engine.remove_intention("test-2")

        assert result is True
        assert "test-2" not in engine.get_scheduled_intentions()

    @pytest.mark.asyncio
    async def test_disabled_intention_not_scheduled(self, engine):
        """Test that disabled intentions are not scheduled."""

        async def noop(x):
            pass

        engine.start(callback=noop)

        intention = {
            "id": "test-3",
            "name": "Disabled Intention",
            "trigger": {"type": "cron", "schedule": "0 8 * * *"},
            "enabled": False,
        }

        result = engine.add_intention(intention)
        assert result is False
        assert "test-3" not in engine.get_scheduled_intentions()


class TestProactiveDaemon:
    """Test ProactiveDaemon integration."""

    @pytest.fixture
    async def daemon(self, tmp_path, monkeypatch):
        """Create daemon with temp storage in async context."""
        intentions_file = tmp_path / "intentions.json"
        monkeypatch.setattr(
            "pocketpaw.daemon.intentions.get_intentions_path",
            lambda: intentions_file,
        )

        # Reset singletons for fresh test
        import pocketpaw.daemon.intentions as intentions_mod
        import pocketpaw.daemon.proactive as proactive_mod

        intentions_mod._intention_store = None
        proactive_mod._daemon = None

        daemon = ProactiveDaemon()
        yield daemon
        daemon.stop()

    @pytest.mark.asyncio
    async def test_daemon_lifecycle(self, daemon):
        """Test daemon start/stop lifecycle."""
        assert daemon.is_running is False

        daemon.start()
        assert daemon.is_running is True

        daemon.stop()
        assert daemon.is_running is False

    @pytest.mark.asyncio
    async def test_create_intention_via_daemon(self, daemon):
        """Test creating intention through daemon API."""
        daemon.start()

        intention = daemon.create_intention(
            name="Daemon Test",
            prompt="Hello world",
            trigger={"type": "cron", "schedule": "0 9 * * *"},
        )

        assert intention["name"] == "Daemon Test"

        # Verify it was scheduled
        intentions = daemon.get_intentions()
        assert len(intentions) == 1

    @pytest.mark.asyncio
    async def test_toggle_intention_via_daemon(self, daemon):
        """Test toggling intention through daemon API."""
        daemon.start()

        intention = daemon.create_intention(
            name="Toggle Test",
            prompt="test",
            trigger={"type": "cron", "schedule": "0 9 * * *"},
            enabled=True,
        )

        # Toggle off
        toggled = daemon.toggle_intention(intention["id"])
        assert toggled["enabled"] is False

        # Toggle back on
        toggled = daemon.toggle_intention(intention["id"])
        assert toggled["enabled"] is True

    @pytest.mark.asyncio
    async def test_delete_intention_via_daemon(self, daemon):
        """Test deleting intention through daemon API."""
        daemon.start()

        intention = daemon.create_intention(
            name="Delete Test",
            prompt="test",
            trigger={"type": "cron", "schedule": "0 9 * * *"},
        )

        result = daemon.delete_intention(intention["id"])
        assert result is True

        intentions = daemon.get_intentions()
        assert len(intentions) == 0
