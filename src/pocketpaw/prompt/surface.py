"""The surface layer — what the user is looking at while they type.

Created: 2026-08-02 (PA-2, feat/prompt-assembler-seam).

The block itself is not new: the cloud chat path has resolved a per-surface
preamble (route, pocket snapshot, pinned widgets, live lists) since 2026-05-24.
What is new is that it arrives as a LAYER with a key of its own, instead of
riding inside ``knowledge_context`` where the digest could not see it and a
backend caching an agent object could not know it had changed.

This layer renders nothing itself. It carries what the EE resolver produced,
because the OSS core cannot import ``pocketpaw_ee`` (import-linter) and would
not know how to build a preamble anyway. Both halves cross as plain data on
:class:`~pocketpaw.prompt.layer.PromptContext` — the same shape
``deny_mcp_tool_ids`` uses.

THE KEY IS THE PRODUCER'S ANSWER, NOT OURS. It would be easy to key here on a
hash of ``surface_preamble``, and for most surfaces that is what the producer
ends up doing. It is wrong as a RULE, in the direction that fails silently: the
pocket preamble renders the first 12 of N widgets and truncates at 1500 chars,
so an edit to widget 13 changes the pocket and changes nothing we can see. The
handler that read the pocket keys on its ``updatedAt`` and catches that; a hash
taken here would report the surface unchanged and hand a backend a cached agent
describing a pocket that no longer looks like that. So the key is threaded from
the handler, and this layer's only judgement is what to do when there isn't one.
"""

from __future__ import annotations

from pocketpaw.prompt.layer import LayerOutput, PromptContext


class SurfaceContextLayer:
    """Renders the resolved surface preamble under the key its producer gave it."""

    name = "surface"
    # Below ``identity`` (100) and above the tail: who the agent is outranks
    # where the user is, and both outrank the per-turn material.
    priority = 90

    async def render(self, ctx: PromptContext) -> LayerOutput:
        # ``None`` means no producer claimed a key. That is the honest answer on
        # every path that has no surface at all (OSS local runs, the channel
        # adapters, ``prewarm`` before the client has stamped one) AND on a
        # cloud run whose handler declined to claim stability. Both are treated
        # the same on purpose: the layer keeps whatever text it was given, and
        # a caller who cannot say what the text is keyed on does not get to
        # claim it is stable. Empty text with a key is still a real answer and
        # is passed through — a surface that renders to nothing under one
        # identity and to something under another is a change the digest must
        # see (``LayerOutput`` documents this; ``assemble`` skips empty text but
        # keeps the key).
        return LayerOutput(text=ctx.surface_preamble, cache_key=ctx.surface_cache_key)
