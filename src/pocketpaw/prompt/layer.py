"""Prompt layers — the unit the system prompt is assembled from.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).

The system prompt used to be one long string built by appending blocks in
``AgentPool._assemble_system_prompt``. That worked until three backends
independently cached an agent object with the prompt baked in and a cache key
that could not see prompt text (see ``pydantic_ai``'s 2026-08-01 (f) note): a
brand-new chat session was served the previous session's prompt. The per-backend
fix landed in PR #1842; the shape that let three authors make the same mistake
is what this package changes.

The mechanism is :class:`LayerOutput`. A layer returns its text AND a
``cache_key``, and ``cache_key`` has NO DEFAULT — a layer author cannot forget
to answer "does this content belong in a cache key". ``None`` is the explicit
answer for volatile, per-turn content (the soul recall keyed on the user's
message), and excludes the layer from the assembled digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LayerOutput:
    """One layer's contribution to the prompt.

    ``cache_key`` is REQUIRED and deliberately not defaulted:

    * a ``str`` means "this content is stable for as long as this key is" — the
      key, not the text, is what the digest hashes, so a layer whose rendering
      varies without changing what it MEANS (a soul's memory counter, a
      timestamp) keeps one key and does not churn a backend's agent cache;
    * ``None`` means "this content is per-turn" — the layer is excluded from
      the digest entirely, so a retrieval block keyed on the user's message
      cannot make every turn look like a new agent.

    An empty ``text`` still contributes its key: a layer that renders to
    nothing under one identity and to something under another is a change the
    digest must see.
    """

    text: str
    cache_key: str | None


@dataclass(frozen=True)
class PromptContext:
    """The per-run inputs every layer renders against.

    Layers are stateless and registered process-wide (see
    :class:`~pocketpaw.prompt.registry.PromptLayerRegistry`), so everything that
    varies per run arrives here rather than on the layer instance — a layer
    holding a tenant's data between runs is the bug class this package exists
    to close.

    ``instance`` is the pool's ``AgentInstance`` and is typed ``Any`` on
    purpose: this package must not import ``pocketpaw.agents``, which imports
    it. The same opaque pass-through discipline as ``SessionHandle.session_store``.
    """

    instance: Any
    agent_id: str
    message: str
    instructions: str
    knowledge_context: str
    system_message_override: str | None


class PromptLayer(Protocol):
    """One contributor to the assembled system prompt.

    ``priority`` is carried but not yet consumed: the assembler concatenates in
    the order it is given. It is declared now because the order layers are
    DROPPED in when the prompt exceeds its budget is a property of the layer
    itself, not of whoever assembled the list — the budget pass reads it.
    """

    name: str
    priority: int

    async def render(self, ctx: PromptContext) -> LayerOutput: ...
