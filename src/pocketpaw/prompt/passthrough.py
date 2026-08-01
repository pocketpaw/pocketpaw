"""The unkeyed tail — everything the assembler has not layered yet.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).

Steps 3-5 of the legacy ``AgentPool._assemble_system_prompt``, moved verbatim
and behind ONE layer so the assembled text stays byte-identical while the seam
is proven with a single real layer (identity). They come apart into their own
layers next: the authoritative ``instructions`` block, the per-message soul
recall (the volatile one — it renders with ``cache_key=None``), and the
knowledge-base wrapper.

``cache_key`` is ``None`` today because this block contains the per-message soul
recall, which is genuinely per-turn. Keying it would move the digest on every
turn and destroy the very cache the digest exists to make safe.

One byte of the legacy behaviour is deliberately not preserved. The legacy
knowledge-base step appended ``f"{prompt}\\n\\n## Your Knowledge Base..."``
unconditionally, so a prompt with NOTHING before it — no identity, no persona,
no instructions, no recall — came out with a leading blank line. Here the join
is conditional like the two steps above it, so that degenerate case loses the
leading ``\\n\\n``. Every input with any preceding content is byte-identical.
"""

from __future__ import annotations

import logging

from pocketpaw.prompt.layer import LayerOutput, PromptContext

logger = logging.getLogger(__name__)

_MEMORY_HEADER = (
    "## Relevant Past Memories\n"
    "Below are memories from previous conversations that "
    "are relevant to the current question. Use them to "
    "provide continuity and a personalized response.\n\n"
)

_KNOWLEDGE_HEADER = (
    "## Your Knowledge Base\n"
    "Use the following information from your knowledge base to answer questions. "
    "Always reference this data when relevant instead of "
    "making things up or using tools to search.\n\n"
)


def _append(prompt: str, block: str) -> str:
    return f"{prompt}\n\n{block}" if prompt else block


class LegacyTailLayer:
    """Instructions + per-message soul recall + knowledge wrapper, as one block."""

    name = "legacy_tail"
    priority = 0

    async def render(self, ctx: PromptContext) -> LayerOutput:
        instance = ctx.instance
        text = ""

        # Authoritative behavior rules — injected BEFORE the knowledge wrapper
        # so the model reads them as instructions, not reference.
        if ctx.instructions:
            text = _append(text, ctx.instructions)

        # Query-specific soul memory recall — inject relevant past interactions
        # so the agent can reference cross-session memories. This complements
        # the general semantic facts already injected by SoulBootstrapProvider.
        # Skipped on an empty message (e.g. prewarm) — and unkeyed regardless,
        # so it never affects a backend's agent cache.
        if instance.soul_manager and instance.soul_manager.soul and ctx.message.strip():
            try:
                soul_ctx = await instance.soul_manager.soul.context_for(
                    ctx.message,
                    max_memories=5,
                    include_state=False,
                    include_self_model=False,
                )
                if soul_ctx:
                    text = _append(text, f"{_MEMORY_HEADER}{soul_ctx}")
            except Exception:
                logger.debug("Soul context_for() failed for agent %s", ctx.agent_id)

        # Inject knowledge context directly into the system prompt.
        if ctx.knowledge_context:
            text = _append(text, f"{_KNOWLEDGE_HEADER}{ctx.knowledge_context}")

        return LayerOutput(text=text, cache_key=None)
