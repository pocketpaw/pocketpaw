# Soul bridge — connects soul-protocol into PocketPaw's bootstrap and agent loop.
# Created: 2026-03-02
# SoulBootstrapProvider implements BootstrapProviderProtocol.
# SoulBridge provides high-level observe/recall for the agent loop.
#
# Changelog:
# 2026-06-24 (SVL-4): get_context auto-recall now includes PROCEDURAL memories
#   alongside SEMANTIC. Minted correction-rules (CorrectionSoulBridge stores
#   them as PROCEDURAL) and session-learned procedural how-tos were previously
#   structurally excluded from the bootstrap system-prompt injection — they
#   only surfaced when the agent voluntarily called a recall tool. They now
#   auto-recall into the bootstrap knowledge context.
# 2026-08-02 (PA-3b): get_context also returns ``identity_cache_key`` — a digest
#   over the soul content that moves only on a real soul edit, with the parts
#   that re-render on ordinary interaction left out. Not one rendered byte
#   changed; only the claim about them is new. See _stable_identity_projection.
# 2026-08-03 (PA-6): no code change here — the denylist below was RE-MEASURED
#   against soul-protocol 0.4.0 before the cloud path stopped double-checking it
#   through ``ClaudeSDKBackend._behavior_prefix``. It held in both directions.
#   The comment on _VOLATILE_IDENTITY_SECTIONS carries the numbers.

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from pocketpaw.bootstrap.protocol import BootstrapContext

if TYPE_CHECKING:
    from soul_protocol import Soul

logger = logging.getLogger(__name__)

# The sections of ``soul.to_system_prompt()`` that re-render on ordinary
# interaction. MEASURED on 2026-08-02 over 8 turns of a soul nobody was
# deliberately mutating (scratchpad harness, PocketPaw's own birth path):
#
#   ## Current State        Mood | Energy | Focus. Moved on 1/8 turns with the
#                           biorhythms PocketPaw actually deploys (``focus`` is
#                           density-driven and steps low->medium->high->max as
#                           interactions land inside a 1-hour window), and on
#                           8/8 turns for a companion soul with energy drain on
#                           (2%/turn, rendered to whole percent).
#   ## Self-Understanding   one line per self-image, carrying a confidence
#                           label and an evidence count. Moved on 7/8 turns of
#                           a substantive conversation: ``evidence_count``
#                           climbs whenever an interaction matches the domain.
#
# Everything else in that string — name, archetype, origin, prime directive,
# core values, OCEAN personality, communication style, the biorhythms config
# line, persona memory, human memory, the safety guardrails — held still across
# all 8 turns and moves only when somebody edits the soul.
#
# This is a DENYLIST on purpose, and the direction matters. Anything the list
# does not name stays in the key, so a soul-protocol release that ADDS a section
# is keyed by default: for additions the failure mode is an extra rebuild, not a
# stale prompt. An allowlist would fail the other way, which is the bug this
# whole package exists to close.
#
# The residual hazard is the other case, and it is worth naming rather than
# leaving implied: a section ALREADY on this list whose MEANING changes — if
# soul-protocol ever renders something behavioural under ``## Current State`` —
# is excised from the key and does produce a stale prompt. Nothing here detects
# that; only re-measuring against the shipping version does.
#
# RE-MEASURED 2026-08-03 (PA-6), against soul-protocol 0.4.0 as shipping, before
# the cloud path stopped consulting ``ClaudeSDKBackend._behavior_prefix``. Same
# method: birth a soul PocketPaw's way, 8 substantive turns, split
# ``to_system_prompt()`` into sections and diff each across the 7 boundaries.
# 0.4.0 renders EIGHT sections — preamble, Personality, Communication Style,
# Current State, Biorhythms, Persona Memory, Safety guardrails,
# Self-Understanding. Exactly two moved: ``## Self-Understanding`` on 6/7 and
# ``## Current State`` on 1/7 (the density-driven focus band). The other six held
# on all 7. So: nothing volatile is missing from this list, and nothing on it is
# denylisted for nothing — both directions checked, both clean.
#
# What that measurement does NOT establish, and PA-6 inherits: the list is still
# a claim about the version measured. On the cloud path the warm client now keys
# on the assembler's digest alone, so a FUTURE release that renders something
# behavioural under one of these two headings serves a stale prompt rather than
# paying an extra rebuild. Re-run the measurement when soul-protocol moves — see
# the pin in ``tests/test_prompt_identity_soul_key.py``.
_VOLATILE_IDENTITY_SECTIONS = frozenset({"## Current State", "## Self-Understanding"})


def _stable_identity_projection(system_prompt: str) -> str:
    """Return ``system_prompt`` with the per-turn sections removed.

    Used ONLY to compute a cache key — the text the agent reads is never
    touched. A markdown heading opens a section and the next heading closes it,
    so a volatile section is dropped whole without needing to know how many
    lines it renders (``## Current State`` is one, ``## Self-Understanding``
    grows a line per self-image).

    Prose under a heading the soul's own persona happens to name
    ``## Current State`` would be dropped from the key too. That is accepted
    rather than guarded: the guard would have to distinguish a machine-rendered
    section from user prose by its shape, and the cost of being wrong here is a
    key that holds still across an edit to that one soul's persona — recovered
    on the next rebuild — against a guard that could itself mis-fire and churn
    every turn.
    """
    kept: list[str] = []
    dropping = False
    for line in system_prompt.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            dropping = stripped in _VOLATILE_IDENTITY_SECTIONS
        if not dropping:
            kept.append(line)
    return "\n".join(kept)


def _identity_cache_key(stable_identity: str, stable_knowledge: list[str]) -> str:
    """Digest the soul content that survives ordinary interaction.

    Separators between the two inputs and between knowledge items, so a fact
    ending where the next begins cannot collide by concatenation. Truncated to
    16 hex chars — the same bound the assembler's digest and
    ``claude_sdk._client_cache_key`` use.
    """
    h = hashlib.sha256()
    h.update(stable_identity.encode("utf-8", "replace"))
    h.update(b"\x1e")
    for item in stable_knowledge:
        h.update(item.encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


class SoulBootstrapProvider:
    """Wraps a Soul into PocketPaw's BootstrapProviderProtocol.

    Maps the soul's system prompt, personality, and memories into
    the BootstrapContext fields that AgentContextBuilder consumes.
    Preserves instructions (tool docs) and user profile from the
    default provider so the agent retains all its capabilities.
    """

    def __init__(self, soul: Soul) -> None:
        self._soul = soul
        # Load instructions and user profile from default provider once
        from pocketpaw.bootstrap.default_provider import DefaultBootstrapProvider

        self._default = DefaultBootstrapProvider()

    async def get_context(self) -> BootstrapContext:
        """Build BootstrapContext from the soul's current state.

        Identity, soul, and style come from the Soul instance.
        Instructions and user_profile come from the default provider
        (INSTRUCTIONS.md, USER.md) so tool docs and user context are preserved.
        """
        soul = self._soul

        # Load default context for instructions + user_profile
        default_ctx = await self._default.get_context()

        system_prompt = soul.to_system_prompt()

        # Extract personality, mood, and biorhythm for style hints
        state = soul.state
        mood_hint = f"Current mood: {state.mood}" if hasattr(state, "mood") else ""
        energy_hint = f"Energy: {state.energy}" if hasattr(state, "energy") else ""
        tired_hint = ""
        if hasattr(state, "energy") and hasattr(state, "tired_threshold"):
            if state.energy <= state.tired_threshold:
                tired_hint = "Status: fatigued (low energy)"
        style_parts = [s for s in [mood_hint, energy_hint, tired_hint] if s]

        # ``knowledge`` is what the agent reads. ``stable_knowledge`` is the
        # subset that survives ordinary interaction, and is all the cache key
        # gets to see. Split at the point each item is APPENDED rather than by
        # matching the rendered strings later — this is the only place that
        # knows what each line means, and a downstream ``startswith("Bond
        # level: ")`` would be exactly the two-modules-away inference the
        # prompt package is trying to end.
        knowledge: list[str] = []
        stable_knowledge: list[str] = []

        # Pull active self-images for knowledge context.
        # VOLATILE: confidence is a running average over an evidence count that
        # climbs on any interaction touching the domain. Measured 2026-08-02:
        # moved on 7/8 turns of a substantive conversation.
        if hasattr(soul, "self_model") and soul.self_model:
            try:
                images = soul.self_model.get_active_self_images(limit=5)
                for img in images:
                    knowledge.append(f"[{img.domain}] confidence={img.confidence}")
            except Exception:
                pass

        # v0.2.8+: Include bond level and memory count.
        # VOLATILE, both, and not marginally: ``Soul.observe`` strengthens the
        # bond on EVERY interaction and the memory pipeline stores at least one
        # entry per interaction. Measured 2026-08-02: each moved on 8/8 turns.
        # That is what rules out keying the soul block as a whole — it would
        # rebuild the cached agent every single turn, which is the trade #1842
        # refused.
        if hasattr(soul, "bond") and soul.bond:
            try:
                bond_strength = getattr(soul.bond, "bond_strength", None)
                if bond_strength is not None:
                    knowledge.append(f"Bond level: {bond_strength:.1f}/100")
            except Exception:
                pass
        if hasattr(soul, "memory_count"):
            try:
                knowledge.append(f"Memories: {soul.memory_count}")
            except Exception:
                pass

        # Cross-session soul memory — inject general semantic facts AND
        # procedural rules the soul has learned so the agent carries persistent
        # context across chat sessions. PROCEDURAL covers minted
        # correction-rules (CorrectionSoulBridge) and session-learned how-tos,
        # which were previously excluded from bootstrap injection.
        try:
            from soul_protocol import MemoryType

            recalled_memories = await soul.recall(
                query="",
                types=[MemoryType.SEMANTIC, MemoryType.PROCEDURAL],
                limit=5,
            )
            if recalled_memories:
                for m in recalled_memories:
                    line = f"[{m.type.value}] {m.content}"
                    knowledge.append(line)
                    # STABLE: a recalled fact is content, not a counter. It
                    # changes when the soul learns or forgets something, which
                    # is exactly the change a backend caching the prompt has to
                    # see. Note this branch does not fire today — an empty
                    # ``query`` scores 0.0 against every store, so this recall
                    # returns nothing (measured 2026-08-02, both before and
                    # after remembering a semantic fact at importance 9). The
                    # declaration is written for the content, not the current
                    # hit rate: fix the empty-query recall and the key starts
                    # tracking learned facts with no further change here.
                    stable_knowledge.append(line)
        except Exception:
            logger.debug("Soul memory recall failed for bootstrap context", exc_info=True)

        return BootstrapContext(
            name=soul.name if hasattr(soul, "name") else "Paw",
            identity=system_prompt,
            soul="I am a persistent AI companion powered by soul-protocol.",
            style="; ".join(style_parts) if style_parts else "Helpful and attentive.",
            instructions=default_ctx.instructions,
            knowledge=knowledge,
            user_profile=default_ctx.user_profile,
            identity_cache_key=_identity_cache_key(
                _stable_identity_projection(system_prompt), stable_knowledge
            ),
        )


class SoulBridge:
    """High-level bridge for observe/recall in the agent loop."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    async def observe(self, user_input: str, agent_output: str) -> None:
        """Record an interaction for the soul to learn from."""
        try:
            from soul_protocol import Interaction

            await self._soul.observe(Interaction(user_input=user_input, agent_output=agent_output))
        except Exception:
            pass  # Observation failure should never break the agent loop

    async def recall(self, query: str, limit: int = 5) -> list[str]:
        """Search soul memories and return content strings.

        v0.2.8+: Tries context_for() first for richer, pre-formatted context.
        Falls back to raw recall() if unavailable.
        """
        try:
            # v0.2.8+: context_for() returns formatted block with state + memories
            if hasattr(self._soul, "context_for"):
                try:
                    context = await self._soul.context_for(query, max_memories=limit)
                    if context:
                        return [context]
                except Exception:
                    pass  # Fall through to raw recall
            memories = await self._soul.recall(query, limit=limit)
            return [m.content for m in memories]
        except Exception:
            return []
