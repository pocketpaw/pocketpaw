"""
Channel adapter protocol for pluggable communication channels.
Created: 2026-02-02
"""

import importlib
import logging
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from typing import Protocol

from pocketpaw.bus.events import Channel, InboundMessage, OutboundMessage
from pocketpaw.bus.queue import MessageBus

_log = logging.getLogger(__name__)


def auto_install(extra: str, verify_import: str) -> None:
    """Auto-install an optional dependency if it is missing.

    Uses ``pocketpaw[<extra>]`` so version constraints stay in pyproject.toml
    (single source of truth).

    Args:
        extra: The pocketpaw extra name (e.g. "discord").
        verify_import: A top-level module to try importing after install (e.g. "discord").

    Raises:
        RuntimeError: If the install fails or the module still can't be imported.
    """
    pip_spec = f"pocketpaw[{extra}]"
    _log.info("Auto-installing missing dependency: %s", pip_spec)

    # Prefer uv (fast), fall back to pip
    in_venv = hasattr(sys, "real_prefix") or sys.prefix != sys.base_prefix
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install"]
        if not in_venv:
            cmd.append("--system")
        cmd.append(pip_spec)
    else:
        cmd = ["pip", "install"]
        if not in_venv:
            cmd.append("--user")
        cmd.append(pip_spec)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to install {pip_spec}:\n{result.stderr.strip()}")
        _log.info("Successfully installed %s", pip_spec)
    except FileNotFoundError:
        raise RuntimeError(f"Cannot auto-install {pip_spec}: neither uv nor pip found on PATH")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Timed out installing {pip_spec}")

    # Clear cached import failures so Python retries the import
    importlib.invalidate_caches()

    # Remove stale sys.modules entries for the target module and its parent
    # so importlib.import_module() performs a fresh import.
    for key in list(sys.modules):
        if key == verify_import or key.startswith(verify_import + "."):
            del sys.modules[key]
    # Also clear the top-level package (e.g. "telegram" for "telegram.ext")
    top_pkg = verify_import.split(".")[0]
    for key in list(sys.modules):
        if key == top_pkg or key.startswith(top_pkg + "."):
            del sys.modules[key]

    # Verify the module is now importable
    try:
        importlib.import_module(verify_import)
    except ImportError:
        raise RuntimeError(
            f"Installed {pip_spec} but still cannot import '{verify_import}'. "
            "You may need to restart the application."
        )


_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus", ".wma"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}


def guess_media_type(path: str) -> str:
    """Return ``'audio'``, ``'image'``, or ``'document'`` based on file extension."""
    import os

    ext = os.path.splitext(path)[1].lower()
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _IMAGE_EXTS:
        return "image"
    return "document"


class ChannelAdapter(Protocol):
    """Protocol for channel adapters (Telegram, WebSocket, etc.)."""

    @property
    def channel(self) -> Channel:
        """The channel type this adapter handles."""
        ...

    async def start(self, bus: MessageBus) -> None:
        """Start the adapter, subscribing to the bus."""
        ...

    async def stop(self) -> None:
        """Stop the adapter gracefully."""
        ...

    async def send(self, message: OutboundMessage) -> None:
        """Send a message through this channel."""
        ...


class BaseChannelAdapter(ABC):
    """Base class for channel adapters with common functionality."""

    def __init__(self):
        self._bus: MessageBus | None = None
        self._running = False

    @property
    @abstractmethod
    def channel(self) -> Channel:
        """The channel type."""
        ...

    async def start(self, bus: MessageBus) -> None:
        """Start and subscribe to the bus."""
        self._bus = bus
        self._running = True
        bus.subscribe_outbound(self.channel, self.send)
        try:
            await self._on_start()
        except Exception:
            # Rollback: unsubscribe and mark stopped on init failure
            self._running = False
            bus.unsubscribe_outbound(self.channel, self.send)
            raise

    async def stop(self) -> None:
        """Stop the adapter."""
        self._running = False
        if self._bus:
            self._bus.unsubscribe_outbound(self.channel, self.send)
        await self._on_stop()

    async def _on_start(self) -> None:
        """Override for custom start logic."""
        pass

    async def _on_stop(self) -> None:
        """Override for custom stop logic."""
        pass

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """Send a message through this channel."""
        ...

    async def _publish_inbound(self, message: InboundMessage) -> None:
        """Helper to publish inbound messages."""
        if self._bus:
            await self._bus.publish_inbound(message)
