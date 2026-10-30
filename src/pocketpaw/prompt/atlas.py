"""The atlas layer — the OS the agent is running inside, before anything else.

Created: 2026-08-03 (PA-5, feat/prompt-assembler-seam).

The "Paw OS Primer" block: a one-paragraph OS identity, one line per atlas
primitive, and the standing instruction to call ``atlas_search`` before guessing
whether the OS can do a thing. It exists because the domain's vocabulary is
heavily LLM-biased — a Pocket is a workspace app, not clothing — and a model
that guesses sends users to routes that do not exist.

It is the FIRST layer with a cap that can actually bite, which is why PA-5
lands it: ``max_chars = 2000``, lifted from
``context_builder._INJECTION_CAPS["atlas_primer"]``, where it has enforced a
~500-token ceiling on this exact block since 2026-04-01.

THE TEXT ARRIVES AS PLAIN DATA, and this layer does not build it. That is the
``surface`` layer's shape rather than the ``instructions`` layer's, and the
reason is not the OSS/EE boundary this time — ``pocketpaw.atlas.store`` is core
and the layer could call it. It is that BUILDING the primer here would put it
into every cloud prompt that renders this layer, and the cloud path does not
carry the primer today; only the channel path does. Adding ~1.5k chars to every
cloud turn is a product decision with a token bill attached, not a side effect
of introducing a budget. So the channel path hands its existing block over in
PA-7, and until then this renders nothing and costs nothing.

THE KEY IS THE TENANT SCOPE **AND** A DIGEST OF THE BYTES, where PA-5 filed it
as the scope alone. Both halves earn their place and neither is sufficient:

* the scope alone discriminates nothing a live cache can see. Cloud agents are
  per-workspace, so the scope is fixed for the whole lifetime of any cache it
  guards — the same reduction PA-3b recorded for ``created_from_updated_at``,
  which collapsed the identity key to the override alone against a live cache.
* the digest is exact, for the reason ``instructions`` gives: this block is a
  COMPLETE artifact rather than a lossy view of a larger object. The primer is
  every primitive the store holds, not the first twelve. So hashing it cannot
  under-report the way hashing a surface preamble does.
* the scope is still carried because it is the half that survives the primer
  becoming per-tenant. ``atlas/overlay.py`` already re-ranks entries by which
  connectors a tenant actually has; the day the primer reads the overlay
  instead of the unfiltered store, the scope is what says two tenants' primers
  are different things rather than one string that happens to differ.

ORDER: SECOND, DIRECTLY BELOW ``identity``. Who the agent is, then what world it
is running in — orientation about the environment only means something once the
reader knows who is being oriented. And it is the most stable block in the
prompt after the persona (packaged seed data, a module-level singleton, constant
for the process's life), so it belongs as high in the prefix as it can get:
everything above the first byte that varies is what a prompt cache reuses.
Pinned with a reason in ``tests/test_prompt_instructions_layer.py``'s
``_ORDER_RULES`` rather than asserted here, since position is a property of the
caller's list.
"""

from __future__ import annotations

import hashlib

from pocketpaw.prompt.layer import LayerOutput, Priority, PromptContext


class AtlasPrimerLayer:
    """Renders the Paw OS primer and keys it on the tenant scope plus its bytes."""

    name = "atlas"
    # MEDIUM: losing it costs the agent accuracy about the OS — it will answer
    # from the LLM's prior instead of from the atlas, which is wrong in a way
    # users notice — but not its persona and not its rules. It outranks the two
    # per-message retrieval blocks and yields to who is talking and where they
    # are, both of which the agent needs to answer at all.
    priority: Priority = Priority.MEDIUM
    # ~500 tokens. Inherited from ``_INJECTION_CAPS["atlas_primer"]`` rather
    # than re-derived: it has bounded this block on the channel path for months,
    # and PA-9 is the task with the measurements to change it.
    #
    # Measured 2026-08-03: the shipped primer renders 1779 chars, so the cap has
    # 221 chars of headroom — about two more primitives at the seed's current
    # one-line-per-primitive rate. The atlas grows by seed edit, so this cap is
    # closer than it looks and the first thing it will do is silently drop the
    # LAST primitives in the list. Whoever adds the third primitive after that
    # gets a truncated primer and a ``dropped`` entry saying so.
    max_chars: int | None = 2000

    async def render(self, ctx: PromptContext) -> LayerOutput:
        # An empty primer still contributes its key, exactly as the empty
        # ``instructions`` channel does: a run carrying the OS primer and a run
        # without it are different prompts, and the one thing a digest must not
        # do is call them one identity.
        return LayerOutput(
            text=ctx.atlas_primer,
            cache_key=f"{ctx.tenant_scope or '-'}:{_short_digest(ctx.atlas_primer)}",
        )


def _short_digest(value: str) -> str:
    """Bound the key regardless of how many primitives the atlas grows.

    A local copy for the reason ``instructions._short_digest`` gives: layer
    modules are meant to read one at a time, and two lines of sha256 are cheaper
    than a dependency between layers that share nothing else.
    """
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
