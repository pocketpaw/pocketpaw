"""System-prompt assembly — layers in, one prompt plus a stable digest out.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).

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

__all__ = [
    "AgentIdentityLayer",
    "AssembledPrompt",
    "DroppedLayer",
    "LayerOutput",
    "LegacyTailLayer",
    "PromptContext",
    "PromptLayer",
    "PromptLayerRegistry",
    "assemble",
    "prompt_layer_registry",
]
