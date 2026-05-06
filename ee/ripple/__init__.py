# ee/ripple/__init__.py — System prompts for Ripple UI generation (Enterprise).
# Licensed under FSL 1.1 — see ee/LICENSE.
#
# Three system-prompt surfaces, one source of truth per surface:
#
#   * INLINE_RIPPLE_SYSTEM_PROMPT — chat-message-level UI. The agent emits a
#     short text reply plus an optional <ripple-spec> block; clicks on the
#     rendered UI round-trip back as user messages via `chat.send`.
#
#   * POCKET_CREATION_PROMPT_{MCP,CLI} — when the user asks us to build a
#     NEW pocket. Covers the create_pocket tool surface, the
#     <list-before-create> gate, and the <interactive-by-default> rule.
#
#   * POCKET_INTERACTION_PROMPT_{MCP,CLI} — when the conversation is anchored
#     to an existing pocket. Read/write/chat intent flow with `get_pocket`
#     first. Contains a literal ``__POCKET_ID__`` token (POCKET_ID_TOKEN) the
#     caller replaces before injection.
#
# Use ``get_pocket_prompts(backend_name=...)`` to pick the right pair.
#
# Model selection, sampling parameters, and prompt caching are decided by
# the agent backend — this module exports prompt strings only.

from __future__ import annotations

from ee.ripple._design import RIPPLE_DESIGN_RULES
from ee.ripple._inline import INLINE_RIPPLE_SYSTEM_PROMPT
from ee.ripple._pockets import (
    POCKET_CREATION_PROMPT,
    POCKET_CREATION_PROMPT_CLI,
    POCKET_CREATION_PROMPT_MCP,
    POCKET_ID_TOKEN,
    POCKET_INTERACTION_PROMPT,
    POCKET_INTERACTION_PROMPT_CLI,
    POCKET_INTERACTION_PROMPT_MCP,
    get_pocket_prompts,
)

__all__ = [
    "INLINE_RIPPLE_SYSTEM_PROMPT",
    "POCKET_CREATION_PROMPT",
    "POCKET_CREATION_PROMPT_CLI",
    "POCKET_CREATION_PROMPT_MCP",
    "POCKET_ID_TOKEN",
    "POCKET_INTERACTION_PROMPT",
    "POCKET_INTERACTION_PROMPT_CLI",
    "POCKET_INTERACTION_PROMPT_MCP",
    "RIPPLE_DESIGN_RULES",
    "get_pocket_prompts",
]
