"""Central registry for :class:`~pocketpaw.prompt.layer.PromptLayer` implementations.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).

Follows the runtime's registry convention (workspace engineering charter,
Pillar 3): ``register`` / ``get`` / ``list`` over a name-keyed dict, plus one
module-level singleton. Layers are stateless — every per-run input arrives via
:class:`~pocketpaw.prompt.layer.PromptContext` — so a process-wide instance is
safe to share across tenants.
"""

from __future__ import annotations

from pocketpaw.prompt.identity import AgentIdentityLayer
from pocketpaw.prompt.layer import PromptLayer
from pocketpaw.prompt.passthrough import LegacyTailLayer


class PromptLayerRegistry:
    """Central registry for ``PromptLayer`` implementations."""

    def __init__(self) -> None:
        self._items: dict[str, PromptLayer] = {}

    def register(self, name: str, item: PromptLayer) -> None:
        self._items[name] = item

    def get(self, name: str) -> PromptLayer:
        return self._items[name]

    def list(self) -> list[str]:
        return list(self._items)


prompt_layer_registry = PromptLayerRegistry()

# The built-in layers, declared where the registry is — the same shape as
# ``agents/registry.py``'s backend table. A caller owns the ORDER it assembles
# them in (the cloud path's order lives in ``AgentPool``); the registry owns
# WHICH implementation answers to a name.
prompt_layer_registry.register(AgentIdentityLayer.name, AgentIdentityLayer())
prompt_layer_registry.register(LegacyTailLayer.name, LegacyTailLayer())
