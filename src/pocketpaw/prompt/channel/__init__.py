"""The channel path's fifteen layers, and the order they are emitted in.

Created: 2026-08-03 (PA-7a, feat/prompt-assembler-channel).

Telegram, Discord, Slack and the CLI used to assemble their system prompt with
``AgentContextBuilder._assemble_with_budget`` while the cloud path used
``pocketpaw.prompt.assemble``. Two assemblers, two sets of rules for caps and
budgets, one of them with a digest and one without. This package is the channel
half moved onto the shared one; the builder now fills a
:class:`~pocketpaw.prompt.channel.inputs.ChannelInputs`, resolves the layers by
name and calls ``assemble``.

THE ORDER IS THE PART THAT WILL BITE YOU, so it is derived here in full.

The old assembler took blocks in the order the builder APPENDED them and
``sorted`` them by priority — a stable sort, so ties kept their append order —
then emitted that. The new assembler emits in the order the caller lists layers
and NEVER reorders (see ``_Rendered``: "the budget decides in priority order but
never reorders what is emitted"). Those two produce the same prompt only if this
list is written in the OLD PATH'S SORTED ORDER, which is not the order the
blocks are appended in and does not look like it.

``_CHANNEL_BLOCK_SOURCE_ORDER`` below is the append order, straight down
``build_system_prompt`` as it stood before the cutover. ``CHANNEL_PROMPT_LAYERS``
is that list stably sorted by priority. The gap between them is not subtle:
``channel_hints`` is appended FIFTH and emitted SECOND TO LAST, because it is
the only LOW-priority block near the top of the function. Get this wrong and
every channel prompt reorders while every test that checks for a block's
PRESENCE still passes.

``tests/test_channel_prompt_layers.py::test_the_emission_order_is_the_old_priority_sort``
re-derives the sort from ``_CHANNEL_BLOCK_SOURCE_ORDER`` and the layers' own
declared priorities and asserts it equals ``CHANNEL_PROMPT_LAYERS``, so the rule
is checked rather than the answer transcribed.

NAMES ARE PREFIXED ``channel.``. The registry is one flat name-keyed dict shared
with the cloud layers, and three names would otherwise collide outright —
``identity`` is already ``AgentIdentityLayer``. A silent overwrite there would
swap the cloud path's persona layer for a channel one that renders nothing
outside a channel turn.
"""

from __future__ import annotations

from pocketpaw.prompt.channel.environment import (
    ChannelAgentsMdLayer,
    ChannelAtlasPrimerLayer,
    ChannelGwsLayer,
    ChannelHealthLayer,
    ChannelSkillsLayer,
)
from pocketpaw.prompt.channel.inputs import ChannelInputs
from pocketpaw.prompt.channel.request import (
    ChannelCurrentPocketLayer,
    ChannelFileContextLayer,
    ChannelFormatHintLayer,
    ChannelIdentityLayer,
    ChannelInstructionsLayer,
    ChannelKnowledgeBaseLayer,
    ChannelMemoryLayer,
    ChannelPocketContextLayer,
    ChannelSenderLayer,
    ChannelSessionKeyLayer,
)
from pocketpaw.prompt.layer import PromptLayer

# Every channel layer, keyed by name. The registry registers from this, so a new
# layer is added in exactly one place and cannot be half-wired.
CHANNEL_LAYER_TYPES: tuple[type, ...] = (
    ChannelIdentityLayer,
    ChannelMemoryLayer,
    ChannelKnowledgeBaseLayer,
    ChannelSenderLayer,
    ChannelFormatHintLayer,
    ChannelInstructionsLayer,
    ChannelPocketContextLayer,
    ChannelCurrentPocketLayer,
    ChannelSessionKeyLayer,
    ChannelFileContextLayer,
    ChannelHealthLayer,
    ChannelSkillsLayer,
    ChannelAtlasPrimerLayer,
    ChannelAgentsMdLayer,
    ChannelGwsLayer,
)

# The order ``build_system_prompt`` APPENDED these blocks, before PA-7a. Kept
# because it is the input to the sort, not because anything emits in it. If you
# add a block, add it here in its append position and let the test re-derive the
# emission order rather than editing ``CHANNEL_PROMPT_LAYERS`` by hand.
_CHANNEL_BLOCK_SOURCE_ORDER: tuple[str, ...] = (
    "channel.identity",  # 1.  CRITICAL
    "channel.memory_context",  # 2.  HIGH
    "channel.kb_context",  # 2b. HIGH
    "channel.sender_block",  # 3.  HIGH
    "channel.channel_hints",  # 4.  LOW   <- appended 5th, emitted 14th
    "channel.channel_instructions",  # 4b. MEDIUM
    "channel.pocket_context",  # 4c. HIGH
    "channel.current_pocket",  # 4d. HIGH
    "channel.session_key",  # 5.  MEDIUM
    "channel.file_context",  # 6.  MEDIUM
    "channel.health_state",  # 7.  LOW
    "channel.skills_list",  # 8.  MEDIUM
    "channel.atlas_primer",  # 8b. MEDIUM
    "channel.agents_md",  # 9.  MEDIUM
    "channel.gws_instructions",  # 10. MEDIUM
)

# What ``assemble`` emits, in order: ``_CHANNEL_BLOCK_SOURCE_ORDER`` stably
# sorted by priority. CRITICAL, then the five HIGH blocks in append order, then
# the seven MEDIUM, then the two LOW.
CHANNEL_PROMPT_LAYERS: tuple[str, ...] = (
    # CRITICAL
    "channel.identity",
    # HIGH
    "channel.memory_context",
    "channel.kb_context",
    "channel.sender_block",
    "channel.pocket_context",
    "channel.current_pocket",
    # MEDIUM
    "channel.channel_instructions",
    "channel.session_key",
    "channel.file_context",
    "channel.skills_list",
    "channel.atlas_primer",
    "channel.agents_md",
    "channel.gws_instructions",
    # LOW
    "channel.channel_hints",
    "channel.health_state",
)

__all__ = [
    "CHANNEL_LAYER_TYPES",
    "CHANNEL_PROMPT_LAYERS",
    "ChannelAgentsMdLayer",
    "ChannelAtlasPrimerLayer",
    "ChannelCurrentPocketLayer",
    "ChannelFileContextLayer",
    "ChannelFormatHintLayer",
    "ChannelGwsLayer",
    "ChannelHealthLayer",
    "ChannelIdentityLayer",
    "ChannelInputs",
    "ChannelInstructionsLayer",
    "ChannelKnowledgeBaseLayer",
    "ChannelMemoryLayer",
    "ChannelPocketContextLayer",
    "ChannelSenderLayer",
    "ChannelSessionKeyLayer",
    "ChannelSkillsLayer",
    "PromptLayer",
]
