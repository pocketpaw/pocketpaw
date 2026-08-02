"""System-prompt assembly — layers in, one prompt plus a stable digest out.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).
Updated: 2026-08-02 (PA-2) — exports :class:`SurfaceContextLayer`, the surface
  the user is looking at. Its text and its key both arrive from the EE resolver
  as plain data; see the module docstring for why the key cannot be computed
  from the text here.
Updated: 2026-08-02 (PA-3) — exports :class:`RetrievalLayer`, the per-message
  soul recall. It is the first layer that exists to ANSWER ``cache_key`` with
  ``None``, and it renders LAST: stable first, volatile last.

The public surface for anyone writing a prompt layer or consuming an assembled
prompt. Start at :class:`LayerOutput` — its ``cache_key`` field is what makes
the "is this content volatile" question unskippable, and that question is the
one three backends independently got wrong before PR #1842.
"""

from pocketpaw.prompt.assembler import AssembledPrompt, DroppedLayer, assemble
from pocketpaw.prompt.identity import AgentIdentityLayer
from pocketpaw.prompt.layer import LayerOutput, PromptContext, PromptLayer
from pocketpaw.prompt.passthrough import LegacyTailLayer
from pocketpaw.prompt.registry import PromptLayerRegistry, prompt_layer_registry
from pocketpaw.prompt.retrieval import RetrievalLayer
from pocketpaw.prompt.surface import SurfaceContextLayer

__all__ = [
    "AgentIdentityLayer",
    "AssembledPrompt",
    "DroppedLayer",
    "LayerOutput",
    "LegacyTailLayer",
    "PromptContext",
    "PromptLayer",
    "PromptLayerRegistry",
    "RetrievalLayer",
    "SurfaceContextLayer",
    "assemble",
    "prompt_layer_registry",
]
