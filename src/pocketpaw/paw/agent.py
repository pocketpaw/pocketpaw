# Paw agent factory — wires Soul, bootstrap provider, tools, and registry together.
# Created: 2026-03-02
# get_paw_agent() returns a configured agent context ready to run.

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocketpaw.paw.config import PawConfig

logger = logging.getLogger(__name__)


def _require_soul_protocol() -> None:
    """Raise a helpful error if soul-protocol is not installed."""
    try:
        import soul_protocol  # noqa: F401
    except ImportError:
        raise ImportError(
            "soul-protocol is required for paw. Install it with:\n"
            "  pip install pocketpaw[soul]\n"
            "  # or\n"
            "  pip install soul-protocol"
        ) from None


@dataclass
class PawAgent:
    """Container for a wired-up paw agent."""

    soul: Any  # Soul instance
    bridge: Any  # SoulBridge
    bootstrap_provider: Any  # SoulBootstrapProvider
    registry: Any  # ToolRegistry
    config: PawConfig


async def get_paw_agent(project_root: Path | None = None) -> PawAgent:
    """Wire everything together and return a configured paw agent.

    1. Load PawConfig from project directory
    2. Awaken or birth a Soul
    3. Create SoulBootstrapProvider
    4. Create ToolRegistry with soul tools
    5. Return PawAgent ready to run
    """
    _require_soul_protocol()

    from soul_protocol import Soul

    from pocketpaw.paw.soul_bridge import SoulBootstrapProvider, SoulBridge
    from pocketpaw.paw.tools import (
        SoulEditCoreTool,
        SoulRecallTool,
        SoulRememberTool,
        SoulStatusTool,
    )
    from pocketpaw.tools.registry import ToolRegistry

    config = PawConfig.load(project_root)

    # Ensure .paw directory exists
    config.paw_dir.mkdir(parents=True, exist_ok=True)

    # Awaken existing soul or birth a new one
    soul_path = config.soul_path or config.default_soul_path
    if soul_path.exists():
        logger.info("Awakening soul from %s", soul_path)
        soul = await Soul.awaken(soul_path)
    else:
        logger.info("Birthing new soul: %s", config.soul_name)
        soul = await Soul.birth(
            name=config.soul_name,
            archetype="Project Assistant",
            persona=f"I am {config.soul_name}, the resident AI for this project.",
        )

    # Wire up bridge and bootstrap
    bridge = SoulBridge(soul)
    bootstrap_provider = SoulBootstrapProvider(soul)

    # Create tool registry with soul tools
    registry = ToolRegistry()
    registry.register(SoulRememberTool(soul))
    registry.register(SoulRecallTool(soul))
    registry.register(SoulEditCoreTool(soul))
    registry.register(SoulStatusTool(soul))

    return PawAgent(
        soul=soul,
        bridge=bridge,
        bootstrap_provider=bootstrap_provider,
        registry=registry,
        config=config,
    )
