"""The agent identity layer — who the agent is, before anything is asked of it.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).
Updated: 2026-08-02 (PA-3b) — the cache key now carries the soul's own claim
  about its content (``BootstrapContext.identity_cache_key``), closing the gap
  PA-1 named below. Not one rendered byte moved; see THE SPLIT IS IN THE KEY.

Lifted verbatim out of ``AgentPool._assemble_system_prompt`` steps 1-2: the
soul/persona identity block (soul bootstrap ``identity`` plus its
``# Key Knowledge`` lines, falling back to the agent config's ``soul_persona``
+ ``system_prompt``), then the entity-rooms A1 ``system_message_override``,
which SWAPS that base and keeps every downstream layer.

The cache key is the layer's real contribution. It identifies the agent whose
identity this is — ``agent_id``, the agent document's revision, which branch
produced the text, the override, and (PA-3b) what the soul says its own content
is worth — rather than hashing the rendered text, because the soul's rendering
carries drifting counters (memory count, bond level, mood, self-image
confidence) that change what the block SAYS without changing who the agent is.
A text hash would move on nearly every turn and cost a backend its agent cache
for nothing. That is not a guess: measured over 8 ordinary turns on 2026-08-02,
the bond level and memory count moved on 8/8, and the self-image lines on 7/8.

THE SPLIT IS IN THE KEY, NOT IN THE TEXT. PA-3b was filed as "split the drifting
soul-state block out of identity", and the natural reading — give the volatile
state its own layer — is the one thing this must not do. ``## Current State``
and ``## Self-Understanding`` sit in the MIDDLE of ``soul.to_system_prompt()``,
between the personality and the persona memory. Moving them into a layer of
their own relocates them to the far end of the assembled prompt, which changes
``ClaudeSDKBackend._behavior_prefix``'s retained bytes and so invalidates every
warm client that is live at deploy. The prompt is byte-identical after this
change; only the claim about it is new. So the decomposition is:
``SoulBootstrapProvider`` renders the same string it always did and separately
says which of it is stable, and this layer folds that answer into its key.

WHY THE CLAIM COMES FROM THE PROVIDER. Same reason ``surface_cache_key`` arrives
on ``PromptContext`` instead of being derived here (PA-2): the producer is the
only party that knows which of the bytes it just wrote are meaningful. Deriving
it here would mean this module matching on ``"Bond level: "`` and
``"## Current State"`` from two packages away — the failure mode
``retrieval.py`` documents, where renaming a header silently changes what a
cache key hashes.

WHAT STILL DOES NOT MOVE THE KEY, deliberately. A new semantic memory. Not
because it is unimportant but because it does not move the PROMPT either: the
bridge's auto-recall passes an empty query, which scores 0.0 against every
memory store, so it returns nothing and no learned fact ever reaches
``# Key Knowledge``. That is filed as a separate bug; the bridge already marks
recalled memories as stable, so fixing it starts moving this key with no change
here. The digest tracks what the prompt says, which is the only thing a
prompt cache can honestly key on.
"""

from __future__ import annotations

import hashlib
import logging

from pocketpaw.prompt.layer import LayerOutput, Priority, PromptContext

logger = logging.getLogger(__name__)

# The soul-key slot when nobody claimed one: no soul, a provider that does not
# answer the question (the config-persona path, ``DefaultBootstrapProvider``),
# a provider whose ``get_context`` raised, or an override that replaced the
# soul text outright. A literal rather than an empty string so the slot is
# always occupied and "no claim" cannot be read as "claimed nothing".
_NO_CLAIM = "-"


class AgentIdentityLayer:
    """Renders the agent's identity block and keys it on the agent's revision."""

    name = "identity"
    # CRITICAL: an agent without its persona is a different product, so the
    # budget may never drop it. PA-5 flipped the convention — this was ``100``
    # under "bigger is more important" and nothing read it.
    priority: Priority = Priority.CRITICAL
    # UNCAPPED, and not merely unmeasured. This layer's key deliberately
    # UNDER-reports its text: it identifies the agent and lets the soul's
    # counters (bond level, memory count, self-image confidence) drift beneath
    # one key, which is what keeps a backend's agent cache alive across ordinary
    # turns. A cap on a key like that is unsound in a way a cap on ``atlas`` is
    # not — a growing counter would push stable persona bytes off the end, so
    # one key would name two prompts. Bounding this block means fixing the key
    # first. See ``assembler._apply_cap``.
    max_chars: int | None = None

    async def render(self, ctx: PromptContext) -> LayerOutput:
        instance = ctx.instance
        source = "soul"

        # Build the identity via soul bootstrap if available.
        text: str | None = None
        soul_key = _NO_CLAIM
        if instance.soul_manager and instance.soul_manager.bootstrap_provider:
            try:
                bootstrap = await instance.soul_manager.bootstrap_provider.get_context()
                text = bootstrap.identity
                # Append soul-level knowledge (semantic memories, bond info, etc.)
                # into the identity block so the agent carries persistent context.
                if bootstrap.knowledge:
                    knowledge_lines = "\n".join(f"- {k}" for k in bootstrap.knowledge)
                    text = f"{text}\n\n# Key Knowledge\n{knowledge_lines}"
                soul_key = getattr(bootstrap, "identity_cache_key", None) or _NO_CLAIM
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
            # The override REPLACED the soul text, so the soul's claim now
            # describes bytes that are not in the prompt. Keeping it would
            # rebuild an overridden agent's cache every time its soul was
            # edited, for a change the model can no longer read.
            soul_key = _NO_CLAIM

        revision = getattr(instance, "created_from_updated_at", None)
        cache_key = f"{ctx.agent_id}:{revision or '-'}:{source}:{override_key}:{soul_key}"
        return LayerOutput(text=text, cache_key=cache_key)


def _short_digest(value: str) -> str:
    """Bound the key regardless of how long an entity's override is."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
