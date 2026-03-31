"""SoulManager -- lifecycle management for the Soul instance.

Edge cases handled:
- Corrupt/encrypted .soul files: backs up and births fresh soul
- Concurrent observe(): serialized via asyncio.Lock
- Periodic auto-save: background task prevents data loss on crash
- Graceful shutdown: saves state and cancels auto-save task
- Auto-sync: detects external .soul file changes (v0.2.4+)
- Rubric self-evaluation: heuristic scoring after interactions (v0.2.4+)
- Configurable biorhythms: energy/mood dynamics via DNA (v0.2.4+)
- CognitiveEngine wiring: passes PocketPawCognitiveEngine to Soul so the
  active agent backend powers fact extraction, significance scoring,
  and reflection instead of heuristic fallbacks (feat/pocketpaw-cognitive-engine)
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketpaw.agents.backend import AgentBackend
    from pocketpaw.config import Settings
    from pocketpaw.paw.soul_bridge import SoulBootstrapProvider, SoulBridge
    from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

# Soul config formats supported on hot-reload
_SOUL_CONFIG_FORMATS: frozenset[str] = frozenset({".yaml", ".yml", ".json"})

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

    # Cache for Interaction class (avoid repeated import per observe call)
    _interaction_cls: type | None = None

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.soul: Any = None
        self.bridge: SoulBridge | None = None
        self.bootstrap_provider: SoulBootstrapProvider | None = None
        self._initialized = False
        self._observe_lock = asyncio.Lock()
        self._auto_save_task: asyncio.Task | None = None
        self._observe_count = 0
        self._dirty = False  # Track whether soul has unsaved changes
        self._tools_cache: list[BaseTool] | None = None
        self._soul_file_mtime: float = 0.0  # Last known mtime for auto-sync

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

    async def initialize(
        self,
        engine: Any | None = None,
    ) -> None:
        """Birth or awaken the soul.

        Args:
            engine: Optional CognitiveEngine to wire into the soul for
                LLM-enhanced cognition (significance, fact extraction,
                reflection).  When None the soul runs in heuristic mode.
                Typically a PocketPawCognitiveEngine wrapping the active
                agent backend is passed by AgentLoop.start().
        """
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
            self.soul = await self._try_awaken(Soul, soul_path, engine=engine)
        else:
            self.soul = await self._birth_soul(Soul, engine=engine)

        # Fallback: if awaken returned None (corrupt file), birth fresh
        if self.soul is None:
            self.soul = await self._birth_soul(Soul, engine=engine)

        self.bridge = SoulBridge(self.soul)
        self.bootstrap_provider = SoulBootstrapProvider(self.soul)
        self._initialized = True
        self._dirty = False
        self._record_file_mtime()

        global _manager
        _manager = self

        engine_label = type(engine).__name__ if engine is not None else "HeuristicEngine"
        logger.info("Soul initialized: %s (cognitive engine: %s)", self.soul.name, engine_label)

    async def _try_awaken(
        self,
        soul_cls: type,
        soul_path: Path,
        engine: Any | None = None,
    ) -> Any | None:
        """Attempt to awaken a soul from file.

        If the file is corrupt or encrypted, back it up and return None
        so the caller can birth a fresh soul.

        Args:
            soul_cls: The Soul class to call awaken() on.
            soul_path: Path to the .soul file.
            engine: Optional CognitiveEngine forwarded to Soul.awaken().
        """
        try:
            logger.info("Awakening soul from %s", soul_path)
            return await soul_cls.awaken(soul_path, engine=engine)
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

    async def _birth_soul(self, soul_cls: type, engine: Any | None = None) -> Any:
        """Birth a new soul from settings.

        Args:
            soul_cls: The Soul class to call birth() on.
            engine: Optional CognitiveEngine forwarded to Soul.birth().
        """
        s = self._settings
        persona = s.soul_persona or (
            f"I am {s.soul_name}, a persistent AI companion. I value {', '.join(s.soul_values)}."
        )
        logger.info("Birthing new soul: %s", s.soul_name)

        kwargs: dict[str, Any] = {
            "name": s.soul_name,
            "archetype": s.soul_archetype,
            "values": s.soul_values,
            "persona": persona,
        }
        if engine is not None:
            kwargs["engine"] = engine
        if s.soul_ocean:
            kwargs["ocean"] = s.soul_ocean
        if s.soul_communication:
            kwargs["communication"] = s.soul_communication

        # v0.2.4+: Pass biorhythm config via DNA if the library supports it
        if s.soul_biorhythm:
            try:
                import inspect

                sig = inspect.signature(soul_cls.birth)
                if "biorhythm" in sig.parameters:
                    kwargs["biorhythm"] = s.soul_biorhythm
                elif "dna" in sig.parameters:
                    kwargs["dna"] = {"biorhythm": s.soul_biorhythm}
            except Exception:
                logger.debug("Could not pass biorhythm config to Soul.birth()")

        return await soul_cls.birth(**kwargs)

    async def observe(self, user_input: str, agent_output: str) -> None:
        """Record a conversation turn (serialized via lock)."""
        if self.bridge is None:
            return
        async with self._observe_lock:
            await self.bridge.observe(user_input, agent_output)
            self._observe_count += 1
            self._dirty = True

    async def evaluate(self, user_input: str, agent_output: str) -> dict[str, Any] | None:
        """Run rubric-based self-evaluation on a response (v0.2.4+).

        Returns a dict with scores and feedback, or None if unsupported.
        """
        if self.soul is None:
            return None
        try:
            if not hasattr(self.soul, "evaluate"):
                return None
            result = await self.soul.evaluate(user_input=user_input, agent_output=agent_output)
            # Result can be a dict or an object with a .dict()/.model_dump() method
            if hasattr(result, "model_dump"):
                return result.model_dump()
            if hasattr(result, "dict"):
                return result.dict()
            if isinstance(result, dict):
                return result
            return {"raw": str(result)}
        except Exception:
            logger.debug("Soul evaluate() failed (non-fatal)", exc_info=True)
            return None

    async def reload(self) -> bool:
        """Reload the soul from its .soul file (v0.2.4+).

        Useful when the file was modified externally (e.g. by another client).
        Returns True if reload succeeded, False otherwise.
        """
        if self.soul is None:
            return False
        try:
            from soul_protocol import Soul

            soul_path = self.soul_file
            if not soul_path.exists():
                return False

            new_soul = await self._try_awaken(Soul, soul_path)
            if new_soul is None:
                return False

            self.soul = new_soul
            if self.bridge is not None:
                self.bridge._soul = self.soul
            if self.bootstrap_provider is not None:
                self.bootstrap_provider._soul = self.soul
            self._tools_cache = None  # Invalidate cached tools
            self._dirty = False
            self._record_file_mtime()
            logger.info("Soul reloaded from %s", soul_path)
            return True
        except Exception:
            logger.exception("Failed to reload soul")
            return False

    async def forget(self, query: str) -> dict[str, Any]:
        """Forget memories matching query (v0.2.8+)."""
        if self.soul is None:
            return {"error": "Soul not available"}
        if not hasattr(self.soul, "forget"):
            return {"error": "Requires soul-protocol >= 0.2.8."}
        try:
            result = await self.soul.forget(query)
            self._dirty = True
            return result if isinstance(result, dict) else {"result": str(result)}
        except Exception:
            logger.debug("Soul forget() failed", exc_info=True)
            return {"error": "forget() failed"}

    async def save(self) -> None:
        """Persist the soul to disk."""
        if self.soul is None:
            return
        try:
            await self.soul.export(self.soul_file)
            self._dirty = False
            self._record_file_mtime()
            logger.debug("Soul saved to %s", self.soul_file)
        except Exception:
            logger.exception("Failed to save soul")

    def _record_file_mtime(self) -> None:
        """Record the current .soul file mtime for sync detection."""
        try:
            self._soul_file_mtime = self.soul_file.stat().st_mtime
        except OSError:
            self._soul_file_mtime = 0.0

    def _file_changed_externally(self) -> bool:
        """Check if the .soul file was modified by another process."""
        try:
            current_mtime = self.soul_file.stat().st_mtime
            return current_mtime > self._soul_file_mtime
        except OSError:
            return False

    def start_auto_save(self) -> None:
        """Start the periodic auto-save background task."""
        interval = self._settings.soul_auto_save_interval
        if interval <= 0 or self._auto_save_task is not None:
            return
        self._auto_save_task = asyncio.create_task(
            self._auto_save_loop(interval), name="soul-auto-save"
        )

    async def _auto_save_loop(self, interval: int) -> None:
        """Periodically save soul state, sync external changes, and consolidate memory."""
        while True:
            await asyncio.sleep(interval)
            try:
                # Auto-sync: detect external .soul file changes
                if self._file_changed_externally():
                    logger.info("External .soul file change detected, reloading")
                    await self.reload()

                # Only save if there are unsaved changes
                if self._dirty:
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
        elif suffix in _SOUL_CONFIG_FORMATS:
            new_soul = await Soul.birth_from_config(file_path)
        else:
            raise ValueError(
                f"Unsupported file format: {suffix}. Use .soul, .yaml, .yml, or .json."
            )

        # Replace current soul -- update existing bridge/provider in-place so that
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
        self._tools_cache = None  # Invalidate cached tools

        # Persist to configured location
        await self.save()

        logger.info("Soul imported from %s: %s", file_path, self.soul.name)
        return self.soul.name

    def get_tools(self) -> list[BaseTool]:
        """Return soul tools (cached per soul instance)."""
        if self.soul is None:
            return []
        # Return cached tools if soul reference hasn't changed
        if self._tools_cache is not None:
            return self._tools_cache
        from pocketpaw.paw.tools import (
            SoulContextTool,
            SoulCoreMemoryTool,
            SoulEditCoreTool,
            SoulEvaluateTool,
            SoulForgetTool,
            SoulRecallTool,
            SoulReloadTool,
            SoulRememberTool,
            SoulStatusTool,
        )

        self._tools_cache = [
            SoulRememberTool(self.soul),
            SoulRecallTool(self.soul),
            SoulEditCoreTool(self.soul),
            SoulStatusTool(self.soul),
            SoulEvaluateTool(self.soul, self),
            SoulReloadTool(self),
            # v0.2.8 tools
            SoulForgetTool(self.soul),
            SoulCoreMemoryTool(self.soul),
            SoulContextTool(self.soul),
        ]
        return self._tools_cache
