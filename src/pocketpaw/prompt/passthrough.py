"""The unkeyed tail — everything the assembler has not layered yet.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).
Updated: 2026-08-02 (PA-3) — the ``## Relevant Past Memories`` soul recall left
  for :mod:`pocketpaw.prompt.retrieval`, which declares itself volatile
  (``cache_key=None``) instead of inheriting this block's silence on the
  question. It also RENDERS LAST now, so the recall moved from between
  ``instructions`` and the knowledge wrapper to after both — see that module for
  why, and ``tests/test_prompt_retrieval_layer.py`` for the goldens that pin it.

What is left is steps 3 and 5 of the legacy
``AgentPool._assemble_system_prompt``, still verbatim and still behind ONE
layer: the authoritative ``instructions`` block and the knowledge-base wrapper.
They come apart next (PA-4), and the split is what lets ``instructions`` take a
real key — it is the most stable content in the prompt and currently
contributes nothing to the digest because it shares a layer with a block that
could not be keyed.

``cache_key`` stays ``None`` for now. Not because what remains is volatile — the
authoritative ``instructions`` are as stable as the identity — but because a
key here would claim stability on behalf of the knowledge wrapper too, whose
content is a per-message KB retrieval. Keying it would move the digest on every
turn and destroy the very cache the digest exists to make safe.

One byte of the legacy behaviour is deliberately not preserved. The legacy
knowledge-base step appended ``f"{prompt}\\n\\n## Your Knowledge Base..."``
unconditionally, so a prompt with NOTHING before it — no identity, no persona,
no instructions, no recall — came out with a leading blank line. Here the join
is conditional like the step above it, so that degenerate case loses the
leading ``\\n\\n``. Every input with any preceding content is byte-identical.
"""

from __future__ import annotations

from pocketpaw.prompt.layer import LayerOutput, PromptContext

_KNOWLEDGE_HEADER = (
    "## Your Knowledge Base\n"
    "Use the following information from your knowledge base to answer questions. "
    "Always reference this data when relevant instead of "
    "making things up or using tools to search.\n\n"
)


def _append(prompt: str, block: str) -> str:
    return f"{prompt}\n\n{block}" if prompt else block


class LegacyTailLayer:
    """Instructions + knowledge wrapper, as one block."""

    name = "legacy_tail"
    priority = 0

    async def render(self, ctx: PromptContext) -> LayerOutput:
        text = ""

        # Authoritative behavior rules — injected BEFORE the knowledge wrapper
        # so the model reads them as instructions, not reference.
        if ctx.instructions:
            text = _append(text, ctx.instructions)

        # Inject knowledge context directly into the system prompt.
        if ctx.knowledge_context:
            text = _append(text, f"{_KNOWLEDGE_HEADER}{ctx.knowledge_context}")

        return LayerOutput(text=text, cache_key=None)
