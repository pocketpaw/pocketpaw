"""The agent identity layer — who the agent is, before anything is asked of it.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).

Lifted verbatim out of ``AgentPool._assemble_system_prompt`` steps 1-2: the
soul/persona identity block (soul bootstrap ``identity`` plus its
``# Key Knowledge`` lines, falling back to the agent config's ``soul_persona``
+ ``system_prompt``), then the entity-rooms A1 ``system_message_override``,
which SWAPS that base and keeps every downstream layer.

The cache key is the layer's real contribution. It identifies the agent whose
identity this is — ``agent_id``, the agent document's revision, which branch
produced the text, and the override — rather than hashing the rendered text,
because the soul's rendering carries drifting counters (memory count, bond
level, mood) that change what the block SAYS without changing who the agent is.
A text hash would move on nearly every turn and cost a backend its agent cache
for nothing.

The gap that leaves is deliberate and worth naming: a soul that learns a new
semantic fact changes ``# Key Knowledge`` without touching the agent document,
so the key holds still. That is correct for the backends this seam serves today
— ``pydantic_ai`` passes the assembled text per run and uses the digest only to
decide whether to rebuild the agent OBJECT — and is the open question for any
backend that bakes the prompt into a cached object.
"""

from __future__ import annotations

import hashlib
import logging

from pocketpaw.prompt.layer import LayerOutput, PromptContext

logger = logging.getLogger(__name__)


class AgentIdentityLayer:
    """Renders the agent's identity block and keys it on the agent's revision."""

    name = "identity"
    priority = 100

    async def render(self, ctx: PromptContext) -> LayerOutput:
        instance = ctx.instance
        source = "soul"

        # Build the identity via soul bootstrap if available.
        text: str | None = None
        if instance.soul_manager and instance.soul_manager.bootstrap_provider:
            try:
                bootstrap = await instance.soul_manager.bootstrap_provider.get_context()
                text = bootstrap.identity
                # Append soul-level knowledge (semantic memories, bond info, etc.)
                # into the identity block so the agent carries persistent context.
                if bootstrap.knowledge:
                    knowledge_lines = "\n".join(f"- {k}" for k in bootstrap.knowledge)
                    text = f"{text}\n\n# Key Knowledge\n{knowledge_lines}"
            except Exception:
                logger.warning("Failed to build soul prompt for agent %s", ctx.agent_id)

        # Fall back to config system_prompt or persona.
        if not text:
            source = "config"
            persona = instance.config.get("soul_persona", "")
            extra = instance.config.get("system_prompt", "")
            text = f"{persona}\n\n{extra}".strip() if persona or extra else ""

        # Per-entity system-message override (entity-rooms A1): SWAP the base,
        # KEEP the layers. Everything above is the base persona/soul identity —
        # exactly what the override replaces. The layers that follow this one
        # (the authoritative ``instructions`` incl. the ripple LAW, the soul
        # recall, the knowledge wrapper) still append, so they ride on top of
        # the override. ``None`` leaves the base untouched (legacy path).
        override_key = "-"
        if ctx.system_message_override is not None:
            text = ctx.system_message_override
            source = "override"
            override_key = _short_digest(ctx.system_message_override)

        revision = getattr(instance, "created_from_updated_at", None)
        cache_key = f"{ctx.agent_id}:{revision or '-'}:{source}:{override_key}"
        return LayerOutput(text=text, cache_key=cache_key)


def _short_digest(value: str) -> str:
    """Bound the key regardless of how long an entity's override is."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
