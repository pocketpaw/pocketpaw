"""The retrieval layer — memories chosen for THIS question, and nothing else.

Created: 2026-08-02 (PA-3, feat/prompt-assembler-seam).

Lifted verbatim out of ``LegacyTailLayer``: the ``## Relevant Past Memories``
block, a per-message soul recall keyed on what the user just typed. It is the
first layer whose whole reason to exist is the answer it gives to
:class:`~pocketpaw.prompt.layer.LayerOutput`'s one required question —
``cache_key=None``, i.e. "I am per-turn, keep me out of the digest".

WHY THAT MATTERS MORE THAN IT LOOKS. This property is currently RECOVERED
downstream by string surgery: ``ClaudeSDKBackend._behavior_prefix`` takes an
already-concatenated prompt and cuts the volatile tail back off it by searching
for ``## Relevant Past Memories`` and its sibling markers. That works, and it
works by knowing this block's header text from two modules away — rename the
header and the warm-client cache key silently starts hashing per-turn recall,
rebuilding the subprocess every turn. A layer that SAYS it is volatile is how
that inference stops being necessary. The surgery is not removed here (the
backends still receive one flat string), but the declaration it would be
derived from now exists.

ORDER: THIS LAYER RENDERS LAST, AND THE BYTES MOVED.
    old: identity → surface → instructions → RECALL → knowledge
    new: identity → surface → instructions → knowledge → RECALL
Extracting recall from the tail forces the choice, because layer order is the
order the caller lists them and this task deliberately does not also split
``instructions`` out of ``LegacyTailLayer`` (PA-4 does). Three reasons the
volatile-last order won:

* it is the module's ordering principle — stable first, volatile last — and
  preserving the legacy byte order would mean blurring two slices to keep an
  order that was itself incidental;
* both blocks are per-message volatile (the knowledge wrapper is filled by a
  per-message KB retrieval), so their RELATIVE order cannot affect prompt
  caching in either direction. Nothing stable sits between them to be moved
  across the cache boundary;
* on the U-curve the end of the prompt is the best-attended position after the
  start. Memories retrieved for the question being asked earn it over a
  knowledge-base dump.

``_behavior_prefix`` is unaffected, and that is checked rather than assumed
(``tests/test_prompt_retrieval_layer.py``): it cuts at the EARLIEST volatile
marker via ``min()``, and both orders open the volatile region at the same
offset — immediately after the authoritative ``instructions`` — so the retained
prefix is byte-identical.

THE GUARDS ARE THE OLD ONES, ON PURPOSE. Same triple condition before querying,
same call arguments, same bare-``Exception`` swallow at ``logger.debug``. This
layer runs per turn against a soul store doing I/O, and the assembler's own
guard is not a substitute: it would catch a raise, but it would also record a
``DroppedLayer`` and contribute a FAILURE key — the right answer for a keyed
layer whose absence changes the prompt's identity, and noise for a block whose
own code has always treated a miss as routine.
"""

from __future__ import annotations

import logging

from pocketpaw.prompt.layer import LayerOutput, Priority, PromptContext

logger = logging.getLogger(__name__)

_MEMORY_HEADER = (
    "## Relevant Past Memories\n"
    "Below are memories from previous conversations that "
    "are relevant to the current question. Use them to "
    "provide continuity and a personalized response.\n\n"
)

# Nothing to render is the common case (no soul, prewarm's empty message, a soul
# with no hit) and the failure case, and all of them are the same answer: no
# text, no key. Named once so no branch below can drift into claiming a key.
_NOTHING = LayerOutput(text="", cache_key=None)


class RetrievalLayer:
    """Renders the per-message soul recall, and declares it volatile."""

    name = "retrieval"
    # LOW — the rank this comment said it wanted and could not have until PA-4
    # took the authoritative ``instructions`` out of the block it shares a rank
    # with. Five recalled memories are the most expendable thing in the prompt.
    # It ties with ``legacy_tail`` and loses the tiebreak to it (ties fall back
    # to layer order and ``retrieval`` renders last), which is the right way
    # round: of the two per-message blocks, this is the one to lose first.
    #
    # A budget drop here is INVISIBLE TO THE DIGEST, and that follows from
    # ``cache_key=None`` rather than being a hole. An unkeyed layer is outside
    # the digest whether it renders or not; making only its absence visible
    # would move the digest on a per-turn condition, which is exactly the churn
    # this layer declares itself volatile to avoid.
    priority: Priority = Priority.LOW
    # UNCAPPED: ``soul.context_for`` is already bounded by ``max_memories=5``,
    # so the block has a natural ceiling that is not a character count.
    max_chars: int | None = None

    async def render(self, ctx: PromptContext) -> LayerOutput:
        instance = ctx.instance

        # Query-specific soul memory recall — inject relevant past interactions
        # so the agent can reference cross-session memories. This complements
        # the general semantic facts already injected by SoulBootstrapProvider,
        # which is why neither state nor self-model is asked for here: both
        # already reach the prompt through the identity layer, and repeating
        # them under a header that says "recall" would double them.
        #
        # ``strip()`` decides, the raw message queries. An all-whitespace
        # message is the prewarm case (``message=""``) and must not pay a soul
        # round-trip — nor render a block, or the prewarmed client's behavioural
        # prefix would diverge from turn 1's and turn 1 would evict it.
        if not (instance.soul_manager and instance.soul_manager.soul and ctx.message.strip()):
            return _NOTHING

        try:
            soul_ctx = await instance.soul_manager.soul.context_for(
                ctx.message,
                max_memories=5,
                include_state=False,
                include_self_model=False,
            )
        except Exception:
            logger.debug("Soul context_for() failed for agent %s", ctx.agent_id)
            return _NOTHING

        if not soul_ctx:
            return _NOTHING
        return LayerOutput(text=f"{_MEMORY_HEADER}{soul_ctx}", cache_key=None)
