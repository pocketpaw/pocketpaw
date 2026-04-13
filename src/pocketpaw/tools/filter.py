# Tool pre-filtering — scores tools against user message to reduce prompt bloat.
#
# Created: 2026-04-01
#
# Tokenizes the user message and scores each tool's name + description for
# relevance via token overlap. Returns the top-N most relevant tools.
# Essential tools (memory, search, pockets) are always included regardless
# of score.
#
# Integration point: call filter_tools() in agents/loop.py before passing
# tool definitions to the router. See that file for the exact location.

import re
from collections.abc import Sequence

from pocketpaw.tools.protocol import BaseTool

# Tools that should always be available regardless of message content
ALWAYS_INCLUDE = frozenset(
    {
        "web_search",
        "remember",
        "recall",
        "forget",
        "create_pocket",
        "add_widget",
        "remove_widget",
    }
)


def _tokenize(text: str) -> set[str]:
    """Split text into lowercase alphanumeric tokens, stripping punctuation."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def filter_tools(
    message: str,
    tools: Sequence[BaseTool],
    *,
    always_include: frozenset[str] = ALWAYS_INCLUDE,
    max_tools: int = 15,
) -> list[BaseTool]:
    """Score tools against the user message and return the top-N most relevant.

    Tools in ``always_include`` are guaranteed to appear in the result.
    Remaining slots are filled by token-overlap score (message tokens
    intersected with tool name + description tokens).

    When the total number of tools is already at or below ``max_tools``,
    all tools are returned unchanged — no scoring overhead.
    """
    if len(tools) <= max_tools:
        return list(tools)

    message_tokens = _tokenize(message)
    if not message_tokens:
        return list(tools)[:max_tools]

    pinned: list[BaseTool] = []
    scored: list[tuple[BaseTool, int]] = []

    for tool in tools:
        if tool.name in always_include:
            pinned.append(tool)
            continue
        tool_text = f"{tool.name} {tool.description}"
        tool_tokens = _tokenize(tool_text)
        overlap = len(message_tokens & tool_tokens)
        scored.append((tool, overlap))

    scored.sort(key=lambda x: x[1], reverse=True)
    remaining_slots = max(0, max_tools - len(pinned))
    result = pinned + [t for t, _ in scored[:remaining_slots]]
    return result
