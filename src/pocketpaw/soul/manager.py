"""SoulManager -- lifecycle management for the Soul instance.

Edge cases handled:
- Corrupt/encrypted .soul files: backs up and births fresh soul
- Concurrent observe(): serialized via asyncio.Lock
- Periodic auto-save: background task prevents data loss on crash
- Graceful shutdown: saves state and cancels auto-save task
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketpaw.config import Settings
    from pocketpaw.paw.soul_bridge import SoulBootstrapProvider, SoulBridge
    from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_manager: SoulManager | None = None


def get_soul_manager() -> SoulManager | None:
    """Return the global SoulManager, or None if not initialized."""
    return _manager


def _reset_manager() -> None:
    """Reset singleton (for tests)."""
    global _manager
    _manager = None


class SoulManager:
    """Manages the Soul instance lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.soul: Any = None
        self.bridge: SoulBridge | None = None
        self.bootstrap_provider: SoulBootstrapProvider | None = None
        self._initialized = False
        self._observe_lock = asyncio.Lock()
        self._auto_save_task: asyncio.Task | None = None
        self._observe_count = 0

    @property
    def observe_count(self) -> int:
        """Number of observations since last reflection."""
        return self._observe_count

    @property
    def soul_dir(self) -> Path:
        if self._settings.soul_path:
            p = Path(self._settings.soul_path)
            return p.parent if p.suffix == ".soul" else p
        from pocketpaw.config import get_config_dir

        return get_config_dir() / "soul"

    @property
    def soul_file(self) -> Path:
        if self._settings.soul_path:
            p = Path(self._settings.soul_path)
            if p.suffix == ".soul":
                return p
            return p / f"{self._settings.soul_name.lower()}.soul"
        return self.soul_dir / f"{self._settings.soul_name.lower()}.soul"

    async def initialize(self) -> None:
        """Birth or awaken the soul."""
        if self._initialized:
            return

        try:
            from soul_protocol import Soul
        except ImportError:
            logger.warning("soul-protocol not installed. Install with: pip install pocketpaw[soul]")
            return

        from pocketpaw.paw.soul_bridge import SoulBootstrapProvider, SoulBridge

        self.soul_dir.mkdir(parents=True, exist_ok=True)

        soul_path = self.soul_file
        if soul_path.exists():
            self.soul = await self._try_awaken(Soul, soul_path)
        else:
            self.soul = await self._birth_soul(Soul)

        # Fallback: if awaken returned None (corrupt file), birth fresh
        if self.soul is None:
            self.soul = await self._birth_soul(Soul)

        self.bridge = SoulBridge(self.soul)
        self.bootstrap_provider = SoulBootstrapProvider(self.soul)
        self._initialized = True

        global _manager
        _manager = self

        logger.info("Soul initialized: %s", self.soul.name)

    async def _try_awaken(self, soul_cls: type, soul_path: Path) -> Any | None:
        """Attempt to awaken a soul from file.

        If the file is corrupt or encrypted, back it up and return None
        so the caller can birth a fresh soul.
        """
        try:
            logger.info("Awakening soul from %s", soul_path)
            return await soul_cls.awaken(soul_path)
        except Exception as exc:
            logger.warning(
                "Failed to awaken soul from %s: %s. Backing up and birthing fresh soul.",
                soul_path,
                exc,
            )
            backup_path = soul_path.with_suffix(".soul.corrupt")
            try:
                shutil.copy2(soul_path, backup_path)
                logger.info("Corrupt soul backed up to %s", backup_path)
            except OSError:
                logger.warning("Could not back up corrupt soul file")
            return None

    async def _birth_soul(self, soul_cls: type) -> Any:
        """Birth a new soul from settings."""
        s = self._settings
        persona = s.soul_persona or (
            f"I am {s.soul_name}, a persistent AI companion. I value {', '.join(s.soul_values)}."
        )
        logger.info("Birthing new soul: %s", s.soul_name)
        return await soul_cls.birth(
            name=s.soul_name,
            archetype=s.soul_archetype,
            values=s.soul_values,
            persona=persona,
            ocean=s.soul_ocean if s.soul_ocean else None,
            communication=s.soul_communication if s.soul_communication else None,
        )

    async def observe(self, user_input: str, agent_output: str) -> None:
        """Record a conversation turn (serialized via lock)."""
        if self.bridge is None:
            return
        async with self._observe_lock:
            await self.bridge.observe(user_input, agent_output)
            self._observe_count += 1

    async def save(self) -> None:
        """Persist the soul to disk."""
        if self.soul is None:
            return
        try:
            await self.soul.export(self.soul_file)
            logger.debug("Soul saved to %s", self.soul_file)
        except Exception:
            logger.exception("Failed to save soul")

    def start_auto_save(self) -> None:
        """Start the periodic auto-save background task."""
        interval = self._settings.soul_auto_save_interval
        if interval <= 0 or self._auto_save_task is not None:
            return
        self._auto_save_task = asyncio.create_task(
            self._auto_save_loop(interval), name="soul-auto-save"
        )

    async def _auto_save_loop(self, interval: int) -> None:
        """Periodically save soul state and consolidate memory."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self.save()
                if self.soul is not None and self._observe_count >= 10:
                    try:
                        await self.soul.reflect()
                        self._observe_count = 0
                        logger.debug("Soul memory consolidation complete")
                    except Exception:
                        logger.debug("Soul reflect() failed (non-fatal)", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Soul auto-save failed (non-fatal)", exc_info=True)

    async def shutdown(self) -> None:
        """Save state and stop auto-save task."""
        if self._auto_save_task is not None and not self._auto_save_task.done():
            self._auto_save_task.cancel()
            try:
                await self._auto_save_task
            except asyncio.CancelledError:
                pass
            self._auto_save_task = None
        await self.save()
        logger.info("Soul shut down and saved")

    async def import_from_file(self, file_path: Path) -> str:
        """Import a soul from a .soul file or YAML/JSON config.

        Replaces the current soul, re-wires bridge and bootstrap provider,
        and saves to the configured soul_file location.

        Args:
            file_path: Path to a .soul, .yaml, .yml, or .json file.

        Returns:
            The imported soul's name.

        Raises:
            ImportError: If soul-protocol is not installed.
            ValueError: If the file format is unsupported.
            FileNotFoundError: If the file does not exist.
        """
        try:
            from soul_protocol import Soul
        except ImportError:
            raise ImportError(
                "soul-protocol not installed. Install with: pip install pocketpaw[soul]"
            ) from None

        from pocketpaw.paw.soul_bridge import SoulBootstrapProvider, SoulBridge

        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        suffix = file_path.suffix.lower()
        if suffix == ".soul":
            new_soul = await self._try_awaken(Soul, file_path)
            if new_soul is None:
                raise ValueError(f"Failed to load .soul file: {file_path}")
        elif suffix in (".yaml", ".yml", ".json"):
            new_soul = await Soul.birth_from_config(file_path)
        else:
            raise ValueError(
                f"Unsupported file format: {suffix}. Use .soul, .yaml, .yml, or .json."
            )

        # Replace current soul — update existing bridge/provider in-place so that
        # any external references (e.g. AgentContextBuilder.bootstrap) stay valid.
        self.soul = new_soul
        if self.bridge is not None:
            self.bridge._soul = self.soul
        else:
            self.bridge = SoulBridge(self.soul)
        if self.bootstrap_provider is not None:
            self.bootstrap_provider._soul = self.soul
        else:
            self.bootstrap_provider = SoulBootstrapProvider(self.soul)
        self._initialized = True
        self._observe_count = 0

        # Persist to configured location
        await self.save()

        logger.info("Soul imported from %s: %s", file_path, self.soul.name)
        return self.soul.name

    def get_tools(self) -> list[BaseTool]:
        """Return the four soul tools."""
        if self.soul is None:
            return []
        from pocketpaw.paw.tools import (
            SoulEditCoreTool,
            SoulRecallTool,
            SoulRememberTool,
            SoulStatusTool,
        )

        return [
            SoulRememberTool(self.soul),
            SoulRecallTool(self.soul),
            SoulEditCoreTool(self.soul),
            SoulStatusTool(self.soul),
        ]
