# pocketpaw/ripple/__init__.py — System prompts for Ripple UI generation.
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
#     first. Contains literal ``__POCKET_ID__`` (POCKET_ID_TOKEN) and
#     ``__BACKEND_SUMMARY__`` (BACKEND_SUMMARY_TOKEN) tokens — fill BOTH
#     via ``fill_current_pocket`` before injection.
#
# Use ``get_pocket_prompts(backend_name=...)`` to pick the right pair.
#
#   * HOME_POCKET_PROMPT — the home-surface analogue of the interaction
#     prompt, injected when the chat is scoped to the per-user
#     ``type="home"`` pocket. Tells the agent to call ``add_widget`` for
#     an explicit widget request and otherwise answer directly.
#
# Changes: 2026-05-22 — re-export ``HOME_POCKET_PROMPT``.
# Changes: 2026-05-22 (RFC 04 alpha follow-up 2) — re-export the new
# ``fill_current_pocket`` helper + ``BACKEND_SUMMARY_TOKEN``.
#
# Model selection, sampling parameters, and prompt caching are decided by
# the agent backend — this module exports prompt strings only.

from __future__ import annotations

from pocketpaw.ripple._design import RIPPLE_DESIGN_RULES
from pocketpaw.ripple._inline import INLINE_RIPPLE_SYSTEM_PROMPT
from pocketpaw.ripple._pockets import (
    BACKEND_SUMMARY_TOKEN,
    HOME_POCKET_PROMPT,
    POCKET_CREATION_PROMPT,
    POCKET_CREATION_PROMPT_CLI,
    POCKET_CREATION_PROMPT_MCP,
    POCKET_DELEGATION_RULE,
    POCKET_EDIT_SPECIALIST_PROMPT_CLI,
    POCKET_EDIT_SPECIALIST_PROMPT_MCP,
    POCKET_ID_TOKEN,
    POCKET_INTERACTION_PROMPT,
    POCKET_INTERACTION_PROMPT_CLI,
    POCKET_INTERACTION_PROMPT_MCP,
    POCKET_SPECIALIST_PROMPT,
    fill_current_pocket,
    get_pocket_prompts,
)

__all__ = [
    "BACKEND_SUMMARY_TOKEN",
    "HOME_POCKET_PROMPT",
    "INLINE_RIPPLE_SYSTEM_PROMPT",
    "POCKET_CREATION_PROMPT",
    "POCKET_CREATION_PROMPT_CLI",
    "POCKET_CREATION_PROMPT_MCP",
    "POCKET_DELEGATION_RULE",
    "POCKET_EDIT_SPECIALIST_PROMPT_CLI",
    "POCKET_EDIT_SPECIALIST_PROMPT_MCP",
    "POCKET_ID_TOKEN",
    "POCKET_INTERACTION_PROMPT",
    "POCKET_INTERACTION_PROMPT_CLI",
    "POCKET_INTERACTION_PROMPT_MCP",
    "POCKET_SPECIALIST_PROMPT",
    "RIPPLE_DESIGN_RULES",
    "fill_current_pocket",
    "get_pocket_prompts",
]
