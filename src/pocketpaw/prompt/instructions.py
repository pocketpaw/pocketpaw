"""The authoritative instructions layer — the rules, not the reference material.

Created: 2026-08-02 (PA-4, feat/prompt-assembler-seam).

Lifted verbatim out of ``LegacyTailLayer``: the ``instructions`` channel the EE
cloud path fills from ``build_behavior_instructions`` — the runtime-identity
rule, the artifact-delivery rule, the ripple LAW and pocket-delegation rule, the
per-backend pocket prompts, the ``<pocket-summary>`` anchor, the
``<about-member>`` block, and — when the entity set one — the
``system_message_override`` that REPLACES the pocket-shaped deliverable stack.

NOT ONE BYTE MOVED. ``LegacyTailLayer`` rendered ``instructions`` and then the
knowledge wrapper, in that order; splitting the first out and ordering the layers
``identity, surface, instructions, legacy_tail, retrieval`` reproduces the same
concatenation. Unlike PA-3, which had a reason to move bytes and said so, this
task has none: a moved byte here invalidates every warm Claude SDK client that is
live at deploy (``_behavior_prefix`` hashes this text) and buys nothing.

WHY IT NEEDED A LAYER: it was the most stable content in the prompt and it
contributed NOTHING to the digest, because it shared ``LegacyTailLayer`` with the
knowledge wrapper — whose content is a per-message KB retrieval. One block could
not be keyed, so neither was. A backend that caches an agent with the prompt
baked in (``deep_agents`` and ``langchain_react``, once PA-6 points them at
``stable_digest``) could therefore be handed a cached agent carrying the previous
surface's LAW.

THE KEY IS A DIGEST OF THE TEXT, AND THIS IS THE ONE LAYER WHERE THAT IS RIGHT.
:mod:`~pocketpaw.prompt.surface` refuses exactly this and :mod:`.identity` refuses
it too, so the difference matters. Both refuse because their text is a LOSSY view
of a larger object: the surface preamble renders the first 12 of N widgets under a
1500-char cap, and the soul's rendering carries counters that drift without the
agent changing. A hash of a lossy view under-reports in the unsafe direction.
This text is not a view of anything — ``build_behavior_instructions`` returns the
complete instruction set, assembled from module constants and already-resolved
values. It changes if and only if the instructions change, which is precisely
what a layer key is supposed to mean.

WHY NOT ``surface + entity override``, which is what PA-4 was filed as. Because
it discriminates NOTHING the digest does not already have, and misses things it
should catch:

* the surface key is contributed to the same digest by the ``surface`` layer, and
  the override by ``identity``'s ``override`` slot — so repeating either here
  cannot move a digest that was not already moving;
* meanwhile the instructions text also moves on the agent's BACKEND (the
  MCP-vs-CLI pocket prompts differ), on the entity's ``ripple_mode`` (which is a
  different profile field from ``system_message_override``), on
  ``pocket_type == "home"``, on ``intent``, and on whether Composio is enabled.
  None of those move the surface key or the override, and the agent-document
  revision that might have caught the first is dead — beanie never registers
  ``TimestampedDocument``'s ``_``-prefixed hooks, so ``updatedAt`` holds its
  construction-time value forever. Keyed on ``surface + override`` this layer
  would say "unchanged" across a backend switch, which is the #1842 failure with
  a key attached.

WHAT THE ENTITY OVERRIDE KEYS, HERE AND IN ``identity``. It reaches this layer's
key through the text it is PART of: ``build_behavior_instructions`` appends the
override INSTEAD of the ripple/delegation/pocket stack, so two entities on one
agent produce different instruction bytes and different keys. It reaches
:mod:`.identity`'s key separately, in that layer's own ``override`` slot, because
there it does something else entirely — it SWAPS the base persona, and PA-3b's
soul-key drop hangs off that swap (an override replaced the soul text, so the
soul's claim describes bytes no longer in the prompt). Both layers see it, and
that is the intended arrangement rather than double-counting: they key different
consequences of the same input. Neither can drop it — identity's swap and this
layer's text would both go unkeyed.

ORDER: THIS LAYER MUST STAY ABOVE THE VOLATILE REGION. ``_behavior_prefix`` cuts
the warm-client key at the EARLIEST ``_VOLATILE_PROMPT_MARKERS`` match, so a
keyed layer ordered below one of them is silently cut out of that key and its
cache contribution is lost — the layer would look keyed and behave unkeyed. That
is a property of position, not of this file, so it is pinned in
``tests/test_prompt_instructions_layer.py`` over every keyed layer at once rather
than asserted here.
"""

from __future__ import annotations

import hashlib

from pocketpaw.prompt.layer import LayerOutput, Priority, PromptContext


class InstructionsLayer:
    """Renders the authoritative behavioural rules and keys them on their bytes."""

    name = "instructions"
    # CRITICAL, tied with ``identity``, which is what PA-4 predicted in this
    # comment and PA-5 now says in the type. Dropping the ripple LAW produces an
    # agent that does the wrong KIND of work, exactly as dropping the persona
    # does; a rank between them would claim one is droppable first, which
    # nothing has measured. Ties fall back to layer order, so ``identity``
    # simply takes the budget first.
    priority: Priority = Priority.CRITICAL
    # UNCAPPED, matching ``_INJECTION_CAPS["instructions"] = None``. This is the
    # stack that says what the agent may and may not do; a cut lands mid-rule
    # and the agent obeys half of it, which is worse than a longer prompt.
    max_chars: int | None = None

    async def render(self, ctx: PromptContext) -> LayerOutput:
        # Authoritative behavior rules — rendered BEFORE the knowledge wrapper so
        # the model reads them as instructions, not as reference data. That
        # ordering is the whole reason this channel exists separately from
        # ``knowledge_context`` (see ``build_behavior_instructions``).
        #
        # An EMPTY channel still contributes its key. A run with no instructions
        # and a run with some are different prompts, and the one thing the digest
        # must never do is call them one identity — the assembler skips empty
        # text but keeps the key, and ``sha256("")`` is a perfectly good name for
        # "this layer had nothing to say".
        return LayerOutput(text=ctx.instructions, cache_key=_short_digest(ctx.instructions))


def _short_digest(value: str) -> str:
    """Bound the key regardless of how long the instruction stack grows.

    Deliberately a local copy of ``identity.py``'s helper rather than a shared
    import: layer modules are meant to be readable one at a time, and two lines
    of sha256 are cheaper than a dependency between two layers that otherwise
    share nothing. 16 hex chars is the bound ``claude_sdk._client_cache_key``
    and the assembler's own digest already use.
    """
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
