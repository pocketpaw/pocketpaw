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


class TestStaleSessionTrigger:
    """Tests for the stale-session trigger type."""

    @pytest.fixture
    async def engine(self):
        engine = TriggerEngine()

        async def noop(x):
            pass

        engine.start(callback=noop)
        yield engine
        engine.stop()

    @pytest.mark.asyncio
    async def test_add_stale_trigger_registers_job(self, engine):
        """Adding a stale trigger should register a job in the scheduler."""
        intention = {
            "id": "stale-1",
            "name": "Stale Test",
            "enabled": True,
            "trigger": {
                "type": "stale",
                "threshold_hours": 12,
                "check_interval_minutes": 60,
            },
            "prompt": "Hey {{session.title}} is stale",
        }
        result = engine.add_intention(intention)
        assert result is True
        assert "stale-1" in engine.get_scheduled_intentions()

    @pytest.mark.asyncio
    async def test_stale_trigger_calls_callback_for_stale_sessions(self, engine, monkeypatch):
        """_fire_stale_trigger should call the callback for sessions past the threshold."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch

        from pocketpaw.daemon.triggers import _DEFAULT_STALE_THRESHOLD_HOURS

        now_utc = datetime.now(tz=UTC)
        stale_ts = (now_utc - timedelta(hours=_DEFAULT_STALE_THRESHOLD_HOURS + 1)).isoformat()
        fresh_ts = now_utc.isoformat()

        fake_index = {
            "chan_stale_user": {
                "title": "Old Project",
                "last_activity": stale_ts,
                "preview": "Let's discuss...",
            },
            "chan_fresh_user": {
                "title": "New Chat",
                "last_activity": fresh_ts,
                "preview": "",
            },
        }

        class FakeManager:
            def list_sessions_with_metadata(self):
                return fake_index

        fired: list[dict] = []

        async def capture(intention: dict) -> None:
            fired.append(intention)

        engine.callback = capture

        intention = {
            "id": "stale-2",
            "name": "Stale Watcher",
            "enabled": True,
            "trigger": {
                "type": "stale",
                "threshold_hours": _DEFAULT_STALE_THRESHOLD_HOURS,
                "check_interval_minutes": 60,
            },
            "prompt": "I noticed '{{session.title}}' has been idle {{session.idle_hours}} hours.",
        }

        with patch("pocketpaw.daemon.triggers.get_memory_manager", return_value=FakeManager()):
            await engine._fire_stale_trigger(intention)

        # Only the stale session should have triggered a callback
        assert len(fired) == 1
        stale_meta = fired[0]["_stale_session"]
        assert stale_meta["session_key"] == "chan_stale_user"
        assert stale_meta["title"] == "Old Project"
        assert stale_meta["idle_hours"] > _DEFAULT_STALE_THRESHOLD_HOURS

    @pytest.mark.asyncio
    async def test_stale_trigger_rate_limits_nudges(self, engine):
        """The same stale session should not be nudged twice within the cooldown window."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch

        from pocketpaw.daemon.triggers import _DEFAULT_STALE_THRESHOLD_HOURS

        stale_ts = (
            datetime.now(tz=UTC) - timedelta(hours=_DEFAULT_STALE_THRESHOLD_HOURS + 1)
        ).isoformat()

        fake_index = {
            "chan_rate_user": {
                "title": "Rate Limited Chat",
                "last_activity": stale_ts,
                "preview": "",
            },
        }

        class FakeManager:
            def list_sessions_with_metadata(self):
                return fake_index

        fired: list[dict] = []

        async def capture(intention: dict) -> None:
            fired.append(intention)

        engine.callback = capture

        intention = {
            "id": "stale-rate",
            "name": "Rate Limit Test",
            "enabled": True,
            "trigger": {
                "type": "stale",
                "threshold_hours": _DEFAULT_STALE_THRESHOLD_HOURS,
                "check_interval_minutes": 60,
            },
            "prompt": "nudge",
        }

        with patch("pocketpaw.daemon.triggers.get_memory_manager", return_value=FakeManager()):
            await engine._fire_stale_trigger(intention)
            await engine._fire_stale_trigger(intention)  # second call — should be suppressed

        assert len(fired) == 1, "Session should only be nudged once within the cooldown window"

    @pytest.mark.asyncio
    async def test_executor_injects_session_variables(self):
        """execute() should replace {{session.*}} placeholders when session_meta is provided."""
        from unittest.mock import AsyncMock, patch

        from pocketpaw.daemon.executor import IntentionExecutor

        async def fake_run(prompt, **_):
            yield {"type": "text", "content": prompt, "metadata": {}}
            yield {"type": "done", "content": "", "metadata": {}}

        with (
            patch("pocketpaw.daemon.executor.AgentRouter") as MockRouter,
            patch("pocketpaw.daemon.executor.get_settings"),
            patch("pocketpaw.daemon.executor.get_context_hub"),
            patch("pocketpaw.daemon.executor.get_intention_store"),
        ):
            mock_router_instance = MockRouter.return_value
            mock_router_instance.run = fake_run

            executor = IntentionExecutor()
            # Bypass context gathering
            executor.context_hub = AsyncMock()
            executor.context_hub.gather = AsyncMock(return_value={})
            executor.context_hub.apply_template = lambda prompt, _ctx: prompt
            executor.intention_store = AsyncMock()
            executor.intention_store.mark_run = AsyncMock()

            intention = {
                "id": "exec-stale",
                "name": "Stale Exec Test",
                "prompt": "Hi! '{{session.title}}' has been idle {{session.idle_hours}} hours.",
                "context_sources": [],
            }
            session_meta = {
                "session_key": "ws_user1",
                "title": "Project X",
                "idle_hours": 14.5,
                "preview": "Let's continue",
            }

            chunks = [c async for c in executor.execute(intention, session_meta=session_meta)]
            text_chunks = [c for c in chunks if c.get("type") == "text"]
            assert text_chunks, "Expected at least one text chunk"
            text = text_chunks[0]["content"]
            assert "Project X" in text
            assert "14.5" in text

    @pytest.mark.asyncio
    async def test_eviction_removes_expired_entries(self, engine):
        """Expired entries in _nudged_sessions should be evicted on the next fire."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch

        from pocketpaw.daemon.triggers import _DEFAULT_STALE_THRESHOLD_HOURS

        threshold_hours = _DEFAULT_STALE_THRESHOLD_HOURS
        cooldown = timedelta(hours=threshold_hours * 2)

        # Pre-populate _nudged_sessions with an entry that has expired
        expired_key = "chan_expired_user"
        engine._nudged_sessions[expired_key] = datetime.now(tz=UTC) - cooldown - timedelta(hours=1)
        # Also add a fresh entry that should survive eviction
        fresh_key = "chan_fresh_nudge"
        engine._nudged_sessions[fresh_key] = datetime.now(tz=UTC)

        # Return an empty index so no new sessions fire — we only care about eviction
        class FakeManager:
            def list_sessions_with_metadata(self):
                return {}

        intention = {
            "id": "stale-evict",
            "name": "Eviction Test",
            "enabled": True,
            "trigger": {
                "type": "stale",
                "threshold_hours": threshold_hours,
                "check_interval_minutes": 60,
            },
            "prompt": "nudge",
        }

        with patch("pocketpaw.daemon.triggers.get_memory_manager", return_value=FakeManager()):
            await engine._fire_stale_trigger(intention)

        assert expired_key not in engine._nudged_sessions, "Expired entry should have been evicted"
        assert fresh_key in engine._nudged_sessions, "Fresh entry should survive eviction"
