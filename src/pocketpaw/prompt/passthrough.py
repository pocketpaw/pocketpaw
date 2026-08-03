"""The unkeyed tail — everything the assembler has not layered yet.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).
Updated: 2026-08-02 (PA-3) — the ``## Relevant Past Memories`` soul recall left
  for :mod:`pocketpaw.prompt.retrieval`, which declares itself volatile
  (``cache_key=None``) instead of inheriting this block's silence on the
  question. It also RENDERS LAST now, so the recall moved from between
  ``instructions`` and the knowledge wrapper to after both — see that module for
  why, and ``tests/test_prompt_retrieval_layer.py`` for the goldens that pin it.
Updated: 2026-08-02 (PA-4) — the authoritative ``instructions`` left for
  :mod:`pocketpaw.prompt.instructions`, where they finally take a real key. NO
  BYTES MOVED: this layer rendered instructions-then-knowledge, and the caller
  now orders ``instructions`` immediately before it, which is the same
  concatenation. What is left is step 5 alone — the knowledge-base wrapper.

``cache_key`` stays ``None``, and now for the only reason it ever really had:
the wrapper's content is a per-message KB retrieval. Keying it would move the
digest on every turn and destroy the very cache the digest exists to make safe.
Before PA-4 that silence also covered the ``instructions``, which are as stable
as the identity — one unkeyable block was suppressing the key of the most stable
content in the prompt.

The name stays ``legacy_tail`` rather than becoming ``knowledge``: this block's
future is to be REMOVED, not renamed. PA-8 routes bulk KB out of the system
prompt and onto the tool-result channel, where the gateway compresses it, and
the task doc's eventual layer order has no tail in it at all. Renaming now would
churn the pool's layer tuple, the registry and three test files to describe a
block that is on its way out.

One byte of the legacy behaviour is deliberately not preserved. The legacy
knowledge-base step appended ``f"{prompt}\\n\\n## Your Knowledge Base..."``
unconditionally, so a prompt with NOTHING before it — no identity, no persona,
no instructions, no recall — came out with a leading blank line. Here the join
is conditional like the step above it, so that degenerate case loses the
leading ``\\n\\n``. Every input with any preceding content is byte-identical.
"""

from __future__ import annotations

from pocketpaw.prompt.layer import LayerOutput, Priority, PromptContext

_KNOWLEDGE_HEADER = (
    "## Your Knowledge Base\n"
    "Use the following information from your knowledge base to answer questions. "
    "Always reference this data when relevant instead of "
    "making things up or using tools to search.\n\n"
)


class LegacyTailLayer:
    """The knowledge-base wrapper, still unkeyed."""

    name = "legacy_tail"
    # LOW, and PA-8 is the strongest possible argument for it: the plan is to
    # take this block OUT of the system prompt entirely and serve it as a tool
    # result. A block whose roadmap is "stop sending it" is the block to drop
    # when something has to go. ``_INJECTION_CAPS`` ranks the channel path's
    # ``kb_context`` HIGH; that block is a small per-scope retrieval and this
    # one is the bulk dump PA-8 exists to move, so the ranks part company here.
    priority: Priority = Priority.LOW
    # UNCAPPED for now. A cap belongs on this block — it is the largest thing in
    # a cloud prompt — but sizing it is PA-9's job, and guessing one here would
    # start truncating live traffic in a task whose acceptance is that no byte
    # moves. PA-8 may make the question moot.
    max_chars: int | None = None

    async def render(self, ctx: PromptContext) -> LayerOutput:
        # Inject knowledge context directly into the system prompt. Rendered
        # AFTER the authoritative ``instructions`` (now their own layer, ordered
        # immediately above this one) so the model reads the rules as rules and
        # this as reference — the ordering ``build_behavior_instructions`` exists
        # to get, and the reason the two channels were ever separate.
        #
        # The conditional is what keeps an absent KB from leaving a blank block;
        # the ``_append`` helper it used to share with the instructions branch
        # went with them, since one block cannot join itself to anything.
        if not ctx.knowledge_context:
            return LayerOutput(text="", cache_key=None)
        return LayerOutput(text=f"{_KNOWLEDGE_HEADER}{ctx.knowledge_context}", cache_key=None)
