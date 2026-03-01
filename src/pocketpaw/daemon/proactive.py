"""
ProactiveDaemon - Main orchestrator for proactive agent behavior.

Coordinates:
- IntentionStore (CRUD for intentions)
- TriggerEngine (scheduling)
- IntentionExecutor (agent invocation)
- ContextHub (system info gathering)

The daemon is lightweight - it only monitors triggers and invokes
the agent when needed, not constantly running LLM queries.
"""

import logging
from collections.abc import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import Settings, get_settings
from .context import get_context_hub
from .executor import IntentionExecutor
from .intentions import get_intention_store
from .triggers import TriggerEngine

logger = logging.getLogger(__name__)


class ProactiveDaemon:
    """
    Main daemon class that orchestrates proactive agent behavior.

    Lifecycle:
    1. start() - Load intentions, schedule triggers, begin monitoring
    2. (triggers fire) - Execute intentions, stream results
    3. stop() - Clean up triggers and scheduler

    The daemon integrates with the existing WebSocket/Telegram infrastructure
    to deliver results to users.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        scheduler: AsyncIOScheduler | None = None,
    ):
        """
        Initialize the daemon.

        Args:
            settings: Settings instance (uses singleton if not provided)
            scheduler: Optional shared scheduler (creates own if not provided)
        """
        self.settings = settings or get_settings()

        # Core components
        self.intention_store = get_intention_store()
        self.context_hub = get_context_hub()
        self.trigger_engine = TriggerEngine(scheduler)
        self.executor = IntentionExecutor(
            settings=self.settings,
            intention_store=self.intention_store,
            context_hub=self.context_hub,
        )

        # Callbacks
        self._stream_callback: Callable | None = None
        self._started = False

    @property
    def is_running(self) -> bool:
        """Check if daemon is running."""
        return self._started

    def start(self, stream_callback: Callable | None = None) -> None:
        """
        Start the daemon.

        Args:
            stream_callback: Async function to receive intention execution results.
                            Signature: async def callback(intention_id: str, chunk: dict)
        """
        if self._started:
            logger.warning("Daemon already started")
            return

        logger.info("Starting ProactiveDaemon...")

        # Set up stream callback
        self._stream_callback = stream_callback
        self.executor.set_stream_callback(stream_callback)

        # Start trigger engine
        self.trigger_engine.start(callback=self._on_trigger)

        # Load and schedule all enabled intentions
        self._schedule_all_intentions()

        self._started = True
        logger.info(
            f"ProactiveDaemon started with {len(self.intention_store.get_enabled())} enabled intentions"
        )

    def stop(self) -> None:
        """Stop the daemon."""
        if not self._started:
            return

        logger.info("Stopping ProactiveDaemon...")

        self.trigger_engine.stop()
        self._started = False

        logger.info("ProactiveDaemon stopped")

    def _schedule_all_intentions(self) -> None:
        """Schedule triggers for all enabled intentions."""
        for intention in self.intention_store.get_enabled():
            self.trigger_engine.add_intention(intention)

    async def _on_trigger(self, intention: dict) -> None:
        """
        Called when an intention trigger fires.

        Args:
            intention: The intention that was triggered
        """
        logger.info(f"Intention triggered: {intention['name']}")

        # Execute and stream results
        await self.executor.execute_and_stream(intention)

    # ==================== Intention Management API ====================

    def get_intentions(self) -> list[dict]:
        """Get all intentions."""
        intentions = self.intention_store.get_all()

        # Enrich with next run time
        for intention in intentions:
            next_run = self.trigger_engine.get_next_run_time(intention["id"])
            intention["next_run"] = next_run.isoformat() if next_run else None

        return intentions

    def get_intention(self, intention_id: str) -> dict | None:
        """Get a single intention by ID."""
        intention = self.intention_store.get_by_id(intention_id)
        if intention:
            next_run = self.trigger_engine.get_next_run_time(intention_id)
            intention["next_run"] = next_run.isoformat() if next_run else None
        return intention

    def create_intention(
        self,
        name: str,
        prompt: str,
        trigger: dict,
        context_sources: list[str] | None = None,
        enabled: bool = True,
    ) -> dict:
        """
        Create a new intention.

        Args:
            name: Human-readable name
            prompt: Prompt template (can include {{variables}})
            trigger: Trigger config (e.g., {"type": "cron", "schedule": "0 8 * * *"})
            context_sources: Context sources to gather (e.g., ["system_status"])
            enabled: Whether to enable immediately

        Returns:
            Created intention dict
        """
        intention = self.intention_store.create(
            name=name,
            prompt=prompt,
            trigger=trigger,
            context_sources=context_sources,
            enabled=enabled,
        )

        # Schedule if enabled and daemon is running
        if enabled and self._started:
            self.trigger_engine.add_intention(intention)

        return intention

    def update_intention(self, intention_id: str, updates: dict) -> dict | None:
        """
        Update an intention.

        Args:
            intention_id: ID of intention to update
            updates: Fields to update

        Returns:
            Updated intention or None if not found
        """
        intention = self.intention_store.update(intention_id, updates)

        if intention and self._started:
            # Re-schedule if trigger or enabled changed
            if "trigger" in updates or "enabled" in updates:
                self.trigger_engine.update_intention(intention)

        return intention

    def delete_intention(self, intention_id: str) -> bool:
        """
        Delete an intention.

        Args:
            intention_id: ID of intention to delete

        Returns:
            True if deleted
        """
        # Remove trigger first
        if self._started:
            self.trigger_engine.remove_intention(intention_id)

        return self.intention_store.delete(intention_id)

    def toggle_intention(self, intention_id: str) -> dict | None:
        """
        Toggle enabled state of an intention.

        Args:
            intention_id: ID of intention to toggle

        Returns:
            Updated intention or None if not found
        """
        intention = self.intention_store.toggle(intention_id)

        if intention and self._started:
            if intention["enabled"]:
                self.trigger_engine.add_intention(intention)
            else:
                self.trigger_engine.remove_intention(intention_id)

        return intention

    async def run_intention_now(self, intention_id: str) -> None:
        """
        Manually run an intention immediately.

        Args:
            intention_id: ID of intention to run
        """
        intention = self.intention_store.get_by_id(intention_id)
        if intention:
            await self.executor.execute_and_stream(intention)
        else:
            logger.error(f"Intention not found: {intention_id}")

    def reload_intentions(self) -> None:
        """Reload intentions from disk and reschedule."""
        self.intention_store.reload()

        if self._started:
            # Remove all existing triggers
            self.trigger_engine.remove_all_jobs()
            # Re-schedule
            self._schedule_all_intentions()

        logger.info("Intentions reloaded")


# Singleton pattern
_daemon: ProactiveDaemon | None = None


def get_daemon(
    settings: Settings | None = None,
    scheduler: AsyncIOScheduler | None = None,
) -> ProactiveDaemon:
    """
    Get singleton ProactiveDaemon instance.

    Args:
        settings: Optional settings (only used on first call)
        scheduler: Optional shared scheduler (only used on first call)

    Returns:
        ProactiveDaemon instance
    """
    global _daemon
    if _daemon is None:
        _daemon = ProactiveDaemon(settings=settings, scheduler=scheduler)
    return _daemon
