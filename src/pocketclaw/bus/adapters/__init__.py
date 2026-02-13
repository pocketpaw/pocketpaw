"""
Channel adapter protocol for pluggable communication channels.
Created: 2026-02-02
"""

import importlib
import logging
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Protocol

from pocketclaw.bus.events import Channel, InboundMessage, OutboundMessage
from pocketclaw.bus.queue import MessageBus

_log = logging.getLogger(__name__)

# Maps a pip extra name → the pip package(s) it installs
_EXTRA_PACKAGES: dict[str, str] = {
    "discord": "pocketpaw[discord]",
    "slack": "pocketpaw[slack]",
    "whatsapp-personal": "pocketpaw[whatsapp-personal]",
    "matrix": "pocketpaw[matrix]",
    "teams": "pocketpaw[teams]",
    "gchat": "pocketpaw[gchat]",
}


def auto_install(extra: str, verify_import: str) -> None:
    """Auto-install an optional dependency if it is missing.

    Args:
        extra: The pocketpaw extra name (e.g. "discord").
        verify_import: A top-level module to try importing after install (e.g. "discord").

    Raises:
        RuntimeError: If the install fails or the module still can't be imported.
    """
    pip_spec = _EXTRA_PACKAGES.get(extra, f"pocketpaw[{extra}]")
    _log.info("Auto-installing missing dependency: %s", pip_spec)

    # Prefer uv (fast), fall back to pip
    import sys

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

    # Verify the module is now importable
    try:
        importlib.import_module(verify_import)
    except ImportError:
        raise RuntimeError(
            f"Installed {pip_spec} but still cannot import '{verify_import}'. "
            "You may need to restart the application."
        )


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
