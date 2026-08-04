"""Central registry for :class:`~pocketpaw.prompt.layer.PromptLayer` implementations.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).
Updated: 2026-08-02 (PA-2) — registers ``surface``. It holds no surface state
  itself (every per-run input still arrives on ``PromptContext``), so the
  process-wide instance stays safe to share across tenants.
Updated: 2026-08-02 (PA-3) — registers ``retrieval``, the per-message soul
  recall extracted from ``legacy_tail``. Stateless for the same reason: it
  reaches the soul through ``ctx.instance``, never through a field of its own.
Updated: 2026-08-02 (PA-4) — registers ``instructions``, the authoritative
  behaviour rules, also extracted from ``legacy_tail``. Stateless in the
  strongest sense of the four: it renders ``ctx.instructions`` and keys on it,
  reaching for nothing else at all.
Updated: 2026-08-03 (PA-5) — registers ``atlas`` and ``user``, the first two
  layers with caps that can bite. Both are stateless in ``instructions``' sense:
  they render a ``PromptContext`` field and key on it plus the id/scope beside
  it, so a tenant's primer and a member's block can never outlive the run they
  arrived on.

Follows the runtime's registry convention (workspace engineering charter,
Pillar 3): ``register`` / ``get`` / ``list`` over a name-keyed dict, plus one
module-level singleton. Layers are stateless — every per-run input arrives via
:class:`~pocketpaw.prompt.layer.PromptContext` — so a process-wide instance is
safe to share across tenants.
"""

from __future__ import annotations

from pocketpaw.prompt.atlas import AtlasPrimerLayer
from pocketpaw.prompt.identity import AgentIdentityLayer
from pocketpaw.prompt.instructions import InstructionsLayer
from pocketpaw.prompt.layer import PromptLayer
from pocketpaw.prompt.passthrough import LegacyTailLayer
from pocketpaw.prompt.retrieval import RetrievalLayer
from pocketpaw.prompt.surface import SurfaceContextLayer
from pocketpaw.prompt.user import UserInfoLayer


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
prompt_layer_registry.register(AtlasPrimerLayer.name, AtlasPrimerLayer())
prompt_layer_registry.register(UserInfoLayer.name, UserInfoLayer())
prompt_layer_registry.register(SurfaceContextLayer.name, SurfaceContextLayer())
prompt_layer_registry.register(InstructionsLayer.name, InstructionsLayer())
prompt_layer_registry.register(LegacyTailLayer.name, LegacyTailLayer())
prompt_layer_registry.register(RetrievalLayer.name, RetrievalLayer())
